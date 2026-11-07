"""Lab-only puffle host: the server is the paired device for allowlisted players.

Replaces the downloadable companion. The browser only holds an HttpOnly session
cookie; it never sees the broker device token or any Spotify token. Pairing keeps
the broker's two confirmations: the code typed in the owner's own igloo, and the
«Sí, es mi pingüino» button in the session that requested the code.

Device tokens and Spotify credentials are stored AES-256-GCM encrypted in SQLite
(associated data binds each row to its session or player id). The key comes from
a file mounted only into this service. This is the single-operator lab mode
described in docs/puffle.md, not the production design for third parties.
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from experiments.puffle_sim.spotify import MESSAGES, SpotifyAgent, SpotifyWebAPI, parse_command
from experiments.puffle_sim.spotify_auth import PENDING_TTL, SpotifyAuth, SpotifyAuthError
from .http import ServiceStatus, request

PAIR_TTL = 300                 # the broker's pending-code lifetime
SESSION_TTL = 30 * 24 * 3600   # paired browser session (cookie Max-Age)
MAX_PENDING = 16
MAX_LAB_PLAYERS = 5            # Spotify development mode allowlist size
COOKIE = 'puffle_session'
CALLBACK_PATH = '/puffle/spotify/callback'


class Rejected(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Vault:
    """AES-256-GCM with a 96-bit random nonce per record."""
    def __init__(self, key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(key) != 32:
            raise ValueError('host key must be 32 bytes')
        self.aead = AESGCM(key)

    def seal(self, aad, value):
        nonce = secrets.token_bytes(12)
        return nonce + self.aead.encrypt(nonce, json.dumps(value).encode(), aad.encode())

    def open(self, aad, blob):
        return json.loads(self.aead.decrypt(blob[:12], blob[12:], aad.encode()))


def _locked(method):
    def wrapper(self, *args):
        with self.lock:
            return method(self, *args)
    return wrapper


class Store:
    """Spotify calls run in worker threads (to_thread), so one connection behind a lock."""
    def __init__(self, database, vault, clock=time.time):
        self.vault, self.clock = vault, clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.executescript(
            'CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, device BLOB NOT NULL,'
            ' player INTEGER, nickname TEXT NOT NULL DEFAULT "", paired INTEGER NOT NULL DEFAULT 0,'
            ' expires REAL NOT NULL);'
            'CREATE TABLE IF NOT EXISTS spotify (player INTEGER PRIMARY KEY, cred BLOB NOT NULL);')
        self.db.commit()

    @_locked
    def prune(self):
        self.db.execute('DELETE FROM sessions WHERE expires <= ?', (self.clock(),))
        self.db.commit()

    @_locked
    def pending_count(self):
        return self.db.execute('SELECT count(*) FROM sessions WHERE paired=0').fetchone()[0]

    @_locked
    def create(self, sid, device):
        self.db.execute('INSERT OR REPLACE INTO sessions (sid, device, expires) VALUES (?,?,?)',
                        (sid, self.vault.seal('session:' + sid, device), self.clock() + PAIR_TTL))
        self.db.commit()

    @_locked
    def session(self, sid):
        row = self.db.execute('SELECT device, player, nickname, paired, expires FROM sessions WHERE sid=?',
                              (sid,)).fetchone()
        if not row or row[4] <= self.clock():
            return None
        return {'sid': sid, 'device': self.vault.open('session:' + sid, row[0]), 'player': row[1],
                'nickname': row[2], 'paired': bool(row[3])}

    @_locked
    def identify(self, sid, player, nickname):
        self.db.execute('UPDATE sessions SET player=?, nickname=? WHERE sid=? AND paired=0',
                        (player, nickname, sid))
        self.db.commit()

    @_locked
    def pair(self, sid, player):
        # One device per player at the broker: older sessions for this player are dead now.
        self.db.execute('DELETE FROM sessions WHERE player=? AND sid<>?', (player, sid))
        self.db.execute('UPDATE sessions SET paired=1, expires=? WHERE sid=?', (self.clock() + SESSION_TTL, sid))
        self.db.commit()

    @_locked
    def delete(self, sid):
        self.db.execute('DELETE FROM sessions WHERE sid=?', (sid,))
        self.db.commit()

    @_locked
    def drop_player(self, player):
        self.db.execute('DELETE FROM sessions WHERE player=? AND paired=1', (player,))
        self.db.commit()

    @_locked
    def paired(self):
        rows = self.db.execute('SELECT sid, player FROM sessions WHERE paired=1 AND expires>?',
                               (self.clock(),)).fetchall()
        return {player: sid for sid, player in rows}

    @_locked
    def device_for(self, player):
        row = self.db.execute('SELECT sid FROM sessions WHERE player=? AND paired=1 AND expires>?',
                              (player, self.clock())).fetchone()
        return self.session(row[0])['device'] if row else None

    @_locked
    def spotify_get(self, player):
        row = self.db.execute('SELECT cred FROM spotify WHERE player=?', (player,)).fetchone()
        return self.vault.open('spotify:%d' % player, row[0]) if row else None

    @_locked
    def spotify_put(self, player, cred):
        self.db.execute('INSERT OR REPLACE INTO spotify VALUES (?,?)',
                        (player, self.vault.seal('spotify:%d' % player, cred)))
        self.db.commit()

    @_locked
    def spotify_delete(self, player):
        self.db.execute('DELETE FROM spotify WHERE player=?', (player,))
        self.db.commit()


class PlayerSpotifyAuth(SpotifyAuth):
    """SpotifyAuth for one server-validated player id, with encrypted rows instead of files."""
    def __init__(self, store, player, **kwargs):
        super().__init__(config_dir='/nonexistent', **kwargs)
        self.store, self.player = store, player

    def _load(self):
        return self.store.spotify_get(self.player)

    def _store(self, data):
        self.store.spotify_put(self.player, data)

    def _erase(self):
        self.store.spotify_delete(self.player)


class Silent:
    async def evaluate(self, context):
        return False, ''


class _PlayerEvaluator:
    def __init__(self, agent, player):
        self.agent, self.player = agent, player

    async def evaluate(self, context):
        return await self.agent.evaluate_for(self.player, context)


class Host:
    def __init__(self, store, broker_url, allowed, origin, client_id, *, conversation=None,
                 http=None, clock=time.time, sleep=asyncio.sleep, spotify_sleep=time.sleep, redirect=None,
                 bots_secret=None):
        if not allowed or len(allowed) > MAX_LAB_PLAYERS:
            raise ValueError('lab mode needs 1 to %d allowlisted players' % MAX_LAB_PLAYERS)
        self.store, self.broker, self.allowed = store, broker_url, frozenset(allowed)
        self.origin, self.client_id = origin.rstrip('/'), client_id
        self.http, self.clock, self.sleep, self.spotify_sleep = http, clock, sleep, spotify_sleep
        # Default: same origin. A registered loopback URI (e.g. :1455) may bounce here instead.
        self.redirect = redirect or self.origin + CALLBACK_PATH
        self.auths, self.apis, self.oauth = {}, {}, {}
        self.workers, self.last_poll = {}, {}
        # Shared secret of the bots service (Cadence, Juanito, Viterbos...), separate from the broker's.
        self.bots_secret = bots_secret if bots_secret and len(bots_secret) >= 32 else None
        # One agent for every player: per-player rate limits and locks persist across jobs,
        # whether the order came through the puffle or through a bot.
        self.agent = SpotifyAgent(conversation or Silent(), self.backend)

    # -- Spotify, per server-validated player id --
    def auth(self, player):
        if player not in self.auths:
            kwargs = {'client_id': self.client_id, 'redirect': self.redirect, 'clock': self.clock}
            if self.http:
                kwargs['http'] = self.http
            self.auths[player] = PlayerSpotifyAuth(self.store, player, **kwargs)
        return self.auths[player]

    def backend(self, player):
        if player not in self.allowed or self.store.spotify_get(player) is None:
            return None
        if player not in self.apis:
            self.apis[player] = SpotifyWebAPI(self.auth(player), self.http, sleep=self.spotify_sleep)
        return self.apis[player]

    def spotify_status(self, player):
        if not self.client_id:
            return 'sin_app'
        return 'conectado' if self.store.spotify_get(player) else 'desconectado'

    # -- browser session API (sid is the cookie value; only its digest is stored) --
    def _session(self, cookie):
        self.store.prune()
        return self.store.session(digest(cookie)) if cookie else None

    def _paired(self, cookie):
        session = self._session(cookie)
        if not session or not session['paired'] or session['player'] not in self.allowed:
            raise Rejected(403, 'no_session')
        return session

    async def pair(self, cookie):
        old = self._session(cookie)
        if old and old['paired']:
            raise Rejected(409, 'already_paired')
        if old:
            self.store.delete(old['sid'])
        if self.store.pending_count() >= MAX_PENDING:
            raise Rejected(429, 'busy')
        result = await request(self.broker, '/puffle/api/pair')
        cookie = secrets.token_urlsafe(32)
        self.store.create(digest(cookie), result['token'])
        return cookie, {'code': result['code'], 'expires_in': min(PAIR_TTL, result['expires_in'])}

    async def status(self, cookie):
        session = self._session(cookie)
        if not session:
            return {'state': 'none', 'spotify': 'sin_app' if not self.client_id else 'desconectado'}
        if not session['paired']:
            if session['player'] is None:
                try:
                    found = await request(self.broker, '/puffle/api/pair-status', session['device'])
                except ServiceStatus:
                    self.store.delete(session['sid'])
                    return {'state': 'none', 'spotify': 'desconectado'}
                if found['player'] is not None:
                    self.store.identify(session['sid'], found['player'], found['nickname'])
                    session.update(player=found['player'], nickname=found['nickname'])
            if session['player'] is None:
                return {'state': 'pending', 'spotify': 'desconectado'}
            state = 'confirm' if session['player'] in self.allowed else 'denied'
            return {'state': state, 'nickname': session['nickname'], 'spotify': 'desconectado'}
        player = session['player']
        online = self.clock() - self.last_poll.get(player, 0) < 5
        return {'state': 'paired', 'nickname': session['nickname'], 'online': online,
                'spotify': self.spotify_status(player)}

    async def confirm(self, cookie):
        session = self._session(cookie)
        if not session or session['paired'] or session['player'] is None:
            raise Rejected(409, 'not_ready')
        player = session['player']
        if player not in self.allowed:
            raise Rejected(403, 'not_allowed')
        await request(self.broker, '/puffle/api/confirm', session['device'], {'player': player})
        self.store.pair(session['sid'], player)
        self.start(player)
        return {'state': 'paired', 'nickname': session['nickname']}

    async def disconnect(self, cookie):
        session = self._session(cookie)
        if not session:
            return {'ok': True}
        if session['paired']:
            with contextlib.suppress(ConnectionError, OSError, TimeoutError, ValueError):
                await request(self.broker, '/puffle/api/disconnect', session['device'])
            self.stop(session['player'])
        self.store.delete(session['sid'])
        return {'ok': True}

    def spotify_connect(self, cookie, popup=False):
        session = self._paired(cookie)
        if not self.client_id:
            raise Rejected(503, 'sin_app')
        now = self.clock()
        self.oauth = {k: v for k, v in self.oauth.items() if v[2] > now - PENDING_TTL}
        if len(self.oauth) >= 2 * MAX_LAB_PLAYERS:
            raise Rejected(429, 'busy')
        # «p.» marks the in-game popup flow; the state itself is still checked server-side.
        state = ('p.' if popup else '') + secrets.token_urlsafe(24)
        # The state is bound to the player AND to this browser session, so a link
        # started by someone else cannot attach the victim's Spotify to their penguin.
        self.oauth[state] = (session['player'], session['sid'], now)
        try:
            return {'url': self.auth(session['player']).start(state=state)}
        except SpotifyAuthError:
            self.oauth.pop(state, None)
            raise Rejected(503, 'sin_app') from None

    def spotify_callback(self, cookie, query):
        state = query.get('state', '')
        match = next((k for k in self.oauth if hmac.compare_digest(k, state)), None) if state else None
        bound = self.oauth.pop(match, None) if match else None
        session = self._session(cookie)
        if (not bound or bound[2] <= self.clock() - PENDING_TTL or not session or not session['paired']
                or not hmac.compare_digest(bound[1], session['sid']) or session['player'] != bound[0]):
            return 'sp_error'
        try:
            self.auth(bound[0]).complete(query)
        except SpotifyAuthError as error:
            return 'sp_denied' if 'cancelaste' in str(error) else 'sp_error'
        self.apis.pop(bound[0], None)
        return 'sp_ok'

    def spotify_disconnect(self, cookie):
        player = self._paired(cookie)['player']
        self.auth(player).disconnect()
        self.apis.pop(player, None)
        self.agent.forget(player)
        return {'ok': True}

    # -- bots: a player asks a bot («Cobra, pon despacito») for HIS OWN music --
    def bot_authorized(self, token):
        return bool(self.bots_secret and token and hmac.compare_digest(token, self.bots_secret))

    async def bot_music(self, player, text):
        """Run the speaker's own explicit command on the speaker's own Spotify.

        `player` is the chat sender id the world assigned (bots never act on their own
        initiative and never for someone else). Non-music text returns handled=False so
        the bot answers normally; song names never reach the bot's language model.
        """
        if type(player) is not int or not 0 < player < 2**31 or not isinstance(text, str) or len(text) > 300:
            raise Rejected(400, 'invalid')
        if parse_command(text) is None:
            return {'handled': False}
        if player not in self.allowed:
            return {'handled': True, 'text': MESSAGES['not_connected']}
        _, reply = await asyncio.wait_for(self.agent.evaluate_for(player, (('player', text),)), 25)
        return {'handled': True, 'text': reply}

    # -- device worker: one poll loop per paired, allowlisted player --
    def start(self, player):
        self.stop(player)
        self.workers[player] = asyncio.create_task(self._worker(player))

    def stop(self, player):
        task = self.workers.pop(player, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self.last_poll.pop(player, None)

    def resume(self):
        for player in self.store.paired():
            if player in self.allowed:
                self.start(player)

    async def _worker(self, player):
        from .companion import run_job
        evaluator = _PlayerEvaluator(self.agent, player)
        while True:
            token = self.store.device_for(player)
            if token is None:
                break
            try:
                job = await request(self.broker, '/puffle/api/poll', token)
                self.last_poll[player] = self.clock()
                if job['job']:
                    await run_job({'url': self.broker, 'token': token}, job, evaluator)
                await self.sleep(0.5)
            except ServiceStatus as error:
                if error.status == 403:  # «puffle desconectar» in game, or a newer pairing
                    if self.store.device_for(player) == token:
                        self.store.drop_player(player)
                    break
                await self.sleep(1)  # 410: the world cancelled that job
            except (OSError, ConnectionError, TimeoutError, ValueError, KeyError):
                await self.sleep(3)
        if self.workers.get(player) is asyncio.current_task():
            self.workers.pop(player, None)
            self.last_poll.pop(player, None)

    async def close(self):
        tasks = list(self.workers.values())
        self.workers.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


POPUP_TEXT = {
    'sp_ok': '¡Listo! Abriendo el reproductor de Spotify…',
    'sp_denied': 'Conexión con Spotify cancelada. Puedes cerrar esta ventana.',
    'sp_error': 'No se pudo conectar Spotify. Vuelve al juego e inténtalo otra vez.',
}
POPUP_PAGE = '''<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Spotify · Glaciar</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:#0b7ab9;
color:#fff;font:18px/1.5 system-ui,sans-serif;text-align:center}</style><p>%s</p>
<script>
try { localStorage.setItem('puffle-spotify', JSON.stringify({m: '%s', t: Date.now()})); } catch (e) {}
if (%s) location.replace('https://open.spotify.com/');
else setTimeout(function () { window.close(); }, 4000);
</script></html>'''


def make_app(host):
    from aiohttp import web
    secure = host.origin.startswith('https://')
    messages = {'sp_ok', 'sp_denied', 'sp_error'}

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers={'Cache-Control': 'no-store'})

    @web.middleware
    async def guard(request, handler):
        if request.path.startswith('/internal/'):
            # Service-to-service only (nginx never routes /internal/); bearer = bots secret.
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            if not host.bot_authorized(token):
                return reply({'error': 'forbidden'}, 403)
        elif request.method == 'POST':
            origin = request.headers.get('Origin')
            # A custom header cannot be sent cross-site without a CORS preflight we never allow.
            if request.headers.get('X-Puffle') != '1' or (origin and origin != host.origin):
                return reply({'error': 'forbidden'}, 403)
        try:
            return await handler(request)
        except Rejected as error:
            return reply({'error': error.code}, error.status)
        except (ConnectionError, OSError, TimeoutError, ValueError, KeyError):
            return reply({'error': 'unavailable'}, 503)

    def cookie(request):
        value = request.cookies.get(COOKIE, '')
        return value if 20 <= len(value) <= 64 else ''

    async def pair(request):
        value, payload = await host.pair(cookie(request))
        response = reply(payload)
        response.set_cookie(COOKIE, value, max_age=SESSION_TTL, path='/puffle/', httponly=True,
                            samesite='Lax', secure=secure)
        return response

    async def status(request):
        return reply(await host.status(cookie(request)))

    async def confirm(request):
        return reply(await host.confirm(cookie(request)))

    async def disconnect(request):
        payload = await host.disconnect(cookie(request))
        response = reply(payload)
        response.del_cookie(COOKIE, path='/puffle/')
        return response

    async def spotify_connect(request):
        return reply(host.spotify_connect(cookie(request), popup=request.query.get('popup') == '1'))

    async def spotify_disconnect(request):
        return reply(host.spotify_disconnect(cookie(request)))

    async def spotify_callback(request):
        query = {k: v for k, v in request.query.items() if k in ('state', 'code', 'error')}
        code = await asyncio.to_thread(host.spotify_callback, cookie(request), query)
        code = code if code in messages else 'sp_error'
        if query.get('state', '').startswith('p.'):
            # Popup opened from the game bubble: tell the game tab (same-origin storage
            # event, no opener needed) and, on success, become the Spotify web player.
            return web.Response(text=POPUP_PAGE % (POPUP_TEXT[code], code, 'true' if code == 'sp_ok' else 'false'),
                                content_type='text/html', headers={
                                    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                    'X-Frame-Options': 'DENY',
                                    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
                                                               "script-src 'unsafe-inline'"})
        raise web.HTTPSeeOther('/puffle/?m=' + code, headers={'Cache-Control': 'no-store',
                                                              'Referrer-Policy': 'no-referrer'})

    async def music(request):
        try:
            data = await request.json()
        except ValueError:
            raise Rejected(400, 'invalid') from None
        if not isinstance(data, dict):
            raise Rejected(400, 'invalid')
        return reply(await host.bot_music(data.get('player'), data.get('text')))

    async def healthz(request):
        return reply({'ok': True, 'players': len(host.workers)})

    app = web.Application(middlewares=[guard], client_max_size=4096)
    app.add_routes([
        web.post('/puffle/api/session/pair', pair),
        web.post('/puffle/api/session/status', status),
        web.post('/puffle/api/session/confirm', confirm),
        web.post('/puffle/api/session/disconnect', disconnect),
        web.post('/puffle/spotify/connect', spotify_connect),
        web.post('/puffle/spotify/disconnect', spotify_disconnect),
        web.get(CALLBACK_PATH, spotify_callback),
        web.get('/healthz', healthz),
        web.post('/internal/music', music),
    ])
    return app


def spotify_redirect(value):
    """Optional override: a 127.0.0.1 loopback URI (Spotify allows http only there) or https."""
    import urllib.parse
    if not value:
        return None
    url = urllib.parse.urlsplit(value)
    if (url.query or url.fragment or url.username or not url.path.startswith('/')
            or not (url.scheme == 'https' or (url.scheme == 'http' and url.hostname == '127.0.0.1'))):
        raise ValueError('PUFFLE_SPOTIFY_REDIRECT must be http://127.0.0.1:PORT/... or https')
    return value


def _read_secret(path):
    return Path(path).read_text().strip() if path else None


def from_environment():
    if os.environ.get('PUFFLE_HOST_MODE') != 'lab':
        raise ValueError('only PUFFLE_HOST_MODE=lab is implemented')
    allowed = {int(x) for x in os.environ.get('PUFFLE_ALLOWED_PLAYERS', '').split(',') if x.strip()}
    key = bytes.fromhex(Path(os.environ['PUFFLE_HOST_KEY_FILE']).read_text().strip())
    database = Path(os.environ.get('PUFFLE_HOST_DATABASE', '/var/lib/puffle/host.sqlite'))
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client_id = os.environ.get('PUFFLE_SPOTIFY_CLIENT_ID', '').strip() or None
    if client_id and not (client_id.isalnum() and 16 <= len(client_id) <= 64):
        raise ValueError('invalid Spotify client ID')
    conversation = None
    if os.environ.get('PUFFLE_HOST_CONVERSATION', 'simulated') == 'simulated':
        from experiments.puffle_sim.provider import SimulatedProcess
        conversation = SimulatedProcess()
    return Host(Store(str(database), Vault(key)), os.environ.get('PUFFLE_HOST_BROKER', 'http://127.0.0.1:9103'),
                allowed, os.environ.get('PUFFLE_PUBLIC_ORIGIN', 'http://127.0.0.1:8080'), client_id,
                conversation=conversation, redirect=spotify_redirect(os.environ.get('PUFFLE_SPOTIFY_REDIRECT', '')),
                bots_secret=_read_secret(os.environ.get('PUFFLE_BOTS_SECRET_FILE')))


async def serve(host, address='0.0.0.0', port=9104):
    from aiohttp import web
    runner = web.AppRunner(make_app(host), access_log=None)  # callback URLs carry codes
    await runner.setup()
    await web.TCPSite(runner, address, port).start()
    host.resume()
    try:
        await asyncio.Event().wait()
    finally:
        await host.close()
        await runner.cleanup()

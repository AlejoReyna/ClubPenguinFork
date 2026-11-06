import asyncio
import json
from pathlib import Path
import socket
import tempfile
import unittest
import urllib.parse

from production.puffle.broker import Broker, serve
from production.puffle.host import Host, Rejected, Store, Vault, make_app
from production.puffle.http import request

SECRET = 's' * 48
CID = 'a' * 32
OWNER, VISITOR = 7, 9
TRACK = {'uri': 'spotify:track:abc', 'name': 'Despacito'}


class FakeSpotify:
    """Token endpoint and Web API in one stdlib-shaped http(method, url, headers, body)."""
    def __init__(self):
        self.calls, self.playing = [], None

    def __call__(self, method, url, headers=None, body=None, timeout=30):
        self.calls.append((method, url))
        if url == 'https://accounts.spotify.com/api/token':
            return 200, json.dumps({'access_token': 'ACCESS-TOKEN-X', 'refresh_token': 'REFRESH-TOKEN-Y',
                                    'expires_in': 3600,
                                    'scope': 'user-modify-playback-state user-read-playback-state'}).encode()
        assert headers['Authorization'] == 'Bearer ACCESS-TOKEN-X'
        path = url.split('/v1', 1)[1]
        if path.startswith('/me/player/devices'):
            return 200, json.dumps({'devices': [{'id': 'd1', 'is_active': True}]}).encode()
        if path.startswith('/search'):
            return 200, json.dumps({'tracks': {'items': [TRACK]}}).encode()
        if method == 'PUT' and path.startswith('/me/player/play'):
            self.playing = TRACK
            return 204, b''
        if path == '/me/player':
            return 200, json.dumps({'item': self.playing, 'is_playing': bool(self.playing)}).encode()
        return 500, b''


class Talk:
    def __init__(self):
        self.seen = []

    async def evaluate(self, context):
        self.seen.append(tuple(context))
        return True, 'Hola, dueño'


def query(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = Broker(SECRET)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.task = asyncio.create_task(serve(self.broker, '127.0.0.1', port))
        self.url = f'http://127.0.0.1:{port}'
        for _ in range(50):
            try:
                await request(self.url, '/healthz')
                break
            except OSError:
                await asyncio.sleep(0.01)
        self.db = Path(self.tmp.name) / 'host.sqlite'
        self.spotify, self.talk = FakeSpotify(), Talk()
        self.host = Host(Store(str(self.db), Vault(b'k' * 32)), self.url, {OWNER}, 'http://127.0.0.1:8080', CID,
                         conversation=self.talk, http=self.spotify, sleep=lambda _: asyncio.sleep(0.01),
                         spotify_sleep=lambda _: None)

    async def asyncTearDown(self):
        await self.host.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.broker.db.close()
        self.host.store.db.close()
        self.tmp.cleanup()

    def igloo(self, player, code, nickname='Alexis'):
        """What the world does when the owner types «puffle conectar CODE» in his igloo."""
        self.broker.dispatch('/internal/pair', SECRET, {'player': player, 'code': code, 'nickname': nickname})

    async def online(self, player, expected=True):
        for _ in range(200):
            status = self.broker.dispatch('/internal/status', SECRET, {'player': player})
            if status['online'] is expected:
                return status
            await asyncio.sleep(0.01)
        self.fail('worker lease did not reach %r' % expected)

    async def paired(self, player=OWNER):
        cookie, pending = await self.host.pair('')
        self.igloo(player, pending['code'])
        self.assertEqual((await self.host.status(cookie))['state'], 'confirm')
        await self.host.confirm(cookie)
        await self.online(player)
        return cookie

    async def connected(self, cookie):
        state = query(self.host.spotify_connect(cookie)['url'])['state']
        self.assertEqual(await asyncio.to_thread(self.host.spotify_callback, cookie,
                                                 {'state': state, 'code': 'C'}), 'sp_ok')

    async def ask(self, player, text):
        job = self.broker.dispatch('/internal/submit', SECRET, {'player': player, 'context': [['player', text]]})['job']
        for _ in range(300):
            result = self.broker.dispatch('/internal/result', SECRET, {'player': player, 'job': job})['result']
            if result is not None:
                return result
            await asyncio.sleep(0.01)
        self.fail('no result from the server-side device')

    async def test_pairing_needs_igloo_code_and_browser_confirmation(self):
        cookie, pending = await self.host.pair('')
        self.assertRegex(pending['code'], r'^[0-9A-F]{10}$')
        self.assertEqual((await self.host.status(cookie))['state'], 'pending')
        with self.assertRaises(Rejected):  # nobody typed the code yet
            await self.host.confirm(cookie)
        self.igloo(OWNER, pending['code'])
        status = await self.host.status(cookie)
        self.assertEqual((status['state'], status['nickname']), ('confirm', 'Alexis'))
        self.assertFalse(self.broker.dispatch('/internal/status', SECRET, {'player': OWNER})['paired'])
        await self.host.confirm(cookie)
        await self.online(OWNER)
        status = await self.host.status(cookie)
        self.assertEqual((status['state'], status['online'], status['spotify']), ('paired', True, 'desconectado'))
        self.assertEqual((await self.host.status('x' * 43))['state'], 'none')  # other browsers see nothing

    async def test_player_outside_lab_allowlist_cannot_confirm(self):
        cookie, pending = await self.host.pair('')
        self.igloo(VISITOR, pending['code'])
        self.assertEqual((await self.host.status(cookie))['state'], 'denied')
        with self.assertRaises(Rejected) as caught:
            await self.host.confirm(cookie)
        self.assertEqual(caught.exception.code, 'not_allowed')
        self.assertFalse(self.broker.dispatch('/internal/status', SECRET, {'player': VISITOR})['paired'])

    async def test_spotify_oauth_is_same_origin_bound_to_session_and_encrypted(self):
        cookie = await self.paired()
        url = self.host.spotify_connect(cookie)['url']
        params = query(url)
        self.assertEqual(params['redirect_uri'], 'http://127.0.0.1:8080/puffle/spotify/callback')
        self.assertEqual(params['code_challenge_method'], 'S256')
        # A callback in another browser (attacker link) fails, and burns the state.
        self.assertEqual(self.host.spotify_callback('', {'state': params['state'], 'code': 'C'}), 'sp_error')
        self.assertEqual(self.host.spotify_callback(cookie, {'state': params['state'], 'code': 'C'}), 'sp_error')
        self.assertEqual(self.host.spotify_status(OWNER), 'desconectado')
        await self.connected(cookie)
        self.assertEqual((await self.host.status(cookie))['spotify'], 'conectado')
        raw = self.db.read_bytes()
        self.assertNotIn(b'ACCESS-TOKEN-X', raw)
        self.assertNotIn(b'REFRESH-TOKEN-Y', raw)
        self.host.spotify_disconnect(cookie)
        self.assertEqual(self.host.spotify_status(OWNER), 'desconectado')

    async def test_owner_music_and_talk_run_on_the_server_device(self):
        cookie = await self.paired()
        result = await self.ask(OWNER, 'pon despacito')
        self.assertEqual(result, {'reply': True, 'text': 'Conecta tu Spotify en la página del puffle.'})
        await self.connected(cookie)
        await asyncio.sleep(2.1)  # SpotifyAgent minimum interval between music actions
        self.assertEqual(await self.ask(OWNER, 'pon despacito'), {'reply': True, 'text': 'Puse «Despacito».'})
        self.assertIn(('PUT', 'https://api.spotify.com/v1/me/player/play'), self.spotify.calls)
        await asyncio.sleep(2.1)  # the broker's own 2 s spacing per player
        self.assertEqual(await self.ask(OWNER, '¿cómo estás?'), {'reply': True, 'text': 'Hola, dueño'})
        self.assertTrue(all('despacito' not in text for ctx in self.talk.seen for _, text in ctx))

    async def test_in_game_revoke_stops_the_server_device(self):
        cookie = await self.paired()
        self.broker.dispatch('/internal/revoke', SECRET, {'player': OWNER})
        for _ in range(200):
            if OWNER not in self.host.workers:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn(OWNER, self.host.workers)
        self.assertEqual((await self.host.status(cookie))['state'], 'none')

    async def test_new_pairing_replaces_old_browser(self):
        old = await self.paired()
        new = await self.paired()
        self.assertEqual((await self.host.status(old))['state'], 'none')
        self.assertEqual((await self.host.status(new))['state'], 'paired')
        await self.host.disconnect(new)
        await self.online(OWNER, False)
        self.assertFalse(self.broker.dispatch('/internal/status', SECRET, {'player': OWNER})['paired'])

    async def test_resume_after_restart(self):
        await self.paired()
        await self.host.close()
        self.assertEqual(self.host.workers, {})
        self.host.resume()  # paired sessions persist (encrypted) and their devices restart
        self.assertIn(OWNER, self.host.workers)
        await self.online(OWNER)

    async def test_http_guard_and_cookie(self):
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(make_app(self.host))) as client:
            response = await client.post('/puffle/api/session/pair')
            self.assertEqual(response.status, 403)  # no custom header: cross-site form or fetch
            response = await client.post('/puffle/api/session/pair',
                                         headers={'X-Puffle': '1', 'Origin': 'https://evil.test'})
            self.assertEqual(response.status, 403)
            response = await client.post('/puffle/api/session/pair',
                                         headers={'X-Puffle': '1', 'Origin': 'http://127.0.0.1:8080'})
            self.assertEqual(response.status, 200)
            header = response.headers['Set-Cookie']
            for part in ('HttpOnly', 'SameSite=Lax', 'Path=/puffle/'):
                self.assertIn(part, header)
            self.assertNotIn('token', await response.json())
            response = await client.post('/puffle/spotify/connect', headers={'X-Puffle': '1'})
            self.assertEqual(response.status, 403)  # not paired yet
            response = await client.get('/puffle/spotify/callback?state=x&code=y', allow_redirects=False)
            self.assertEqual((response.status, response.headers['Location']), (303, '/puffle/?m=sp_error'))
            # Popup flow from the game bubble: never leaves the game tab; no code echoed back.
            response = await client.get('/puffle/spotify/callback?state=p.x&code=SECRETCODE', allow_redirects=False)
            body = await response.text()
            self.assertEqual(response.status, 200)
            self.assertIn("m: 'sp_error'", body)
            self.assertNotIn('SECRETCODE', body)
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')

    async def test_popup_success_becomes_the_web_player(self):
        from aiohttp.test_utils import TestClient, TestServer
        cookie = await self.paired()
        state = query(self.host.spotify_connect(cookie, popup=True)['url'])['state']
        self.assertTrue(state.startswith('p.'))
        async with TestClient(TestServer(make_app(self.host))) as client:
            client.session.cookie_jar.update_cookies({'puffle_session': cookie})
            response = await client.get(f'/puffle/spotify/callback?state={state}&code=C', allow_redirects=False)
            body = await response.text()
        self.assertIn("m: 'sp_ok'", body)
        self.assertIn("if (true) location.replace('https://open.spotify.com/')", body)
        self.assertEqual(self.host.spotify_status(OWNER), 'conectado')

    async def test_registered_loopback_redirect_can_be_reused(self):
        from production.puffle.host import spotify_redirect
        self.host.redirect = spotify_redirect('http://127.0.0.1:1455/spotify/callback')
        self.host.auths.clear()
        cookie = await self.paired()
        params = query(self.host.spotify_connect(cookie)['url'])
        self.assertEqual(params['redirect_uri'], 'http://127.0.0.1:1455/spotify/callback')
        self.assertEqual(self.host.spotify_callback(cookie, {'state': params['state'], 'code': 'C'}), 'sp_ok')
        for bad in ('http://localhost:1455/spotify/callback', 'http://evil.test/cb', 'http://127.0.0.1:1455/cb?x=1'):
            with self.assertRaises(ValueError):
                spotify_redirect(bad)

    async def test_bots_change_only_the_speakers_own_music(self):
        from aiohttp.test_utils import TestClient, TestServer
        self.host.bots_secret = 'b' * 40
        cookie = await self.paired()
        auth = {'Authorization': 'Bearer ' + 'b' * 40}
        async with TestClient(TestServer(make_app(self.host))) as client:
            async def music(player, text, headers=auth):
                response = await client.post('/internal/music', json={'player': player, 'text': text}, headers=headers)
                return response.status, await response.json()
            self.assertEqual((await music(OWNER, 'pon despacito', {'Authorization': 'Bearer ' + 'x' * 40}))[0], 403)
            self.assertEqual((await music(OWNER, 'pon despacito', {}))[0], 403)
            # Small talk goes back to the bot (and to its model); music never does.
            self.assertEqual(await music(OWNER, 'hola, ¿qué tal la fiesta?'), (200, {'handled': False}))
            self.assertEqual(await music(OWNER, 'pon despacito'),
                             (200, {'handled': True, 'text': 'Conecta tu Spotify en la página del puffle.'}))
            await self.connected(cookie)
            # A visitor asking a bot cannot reach the owner's Spotify: only his own (none).
            self.assertEqual(await music(VISITOR, 'pon despacito'),
                             (200, {'handled': True, 'text': 'Conecta tu Spotify en la página del puffle.'}))
            self.assertEqual(await music(OWNER, 'pon despacito'),
                             (200, {'handled': True, 'text': 'Puse «Despacito».'}))
            # Puffle and bots share the per-player pacing.
            self.assertEqual(await music(OWNER, 'siguiente'),
                             (200, {'handled': True, 'text': 'Espera un momento entre órdenes.'}))
            self.assertEqual((await music('7', 'pausa'))[0], 400)

    def test_lab_mode_requires_small_allowlist(self):
        store = Store(':memory:', Vault(b'k' * 32))
        for allowed in (set(), set(range(1, 7))):
            with self.assertRaises(ValueError):
                Host(store, self.url, allowed, 'http://127.0.0.1:8080', CID)


if __name__ == '__main__':
    unittest.main()

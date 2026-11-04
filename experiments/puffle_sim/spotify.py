"""Owner-only Spotify control for the conversational puffle (experimental).

Design (see docs/puffle-spotify.md):
- The command comes ONLY from the latest chat line of the igloo owner, after the
  bridge/authority layer accepted it (visitors, bots and other rooms never reach
  the engine). This module never trusts the model for actions: a deterministic,
  bounded parser turns the owner's own words into one of five intents.
- validate_intent() is the gate for any model-proposed intent: it must agree
  with what the parser reads from the owner's latest message.
- Spotify metadata and the owner's command are not sent to the language model:
  confirmations are replaced by a neutral marker in the context passed on.
- Each player maps to HIS OWN backend (Web API with his token, or simulator).
  Tokens never leave the host/server process; the client only gets the bubble.
"""
import asyncio
import json
import re
import time
import unicodedata
import urllib.parse

ACTIONS = ('play', 'pause', 'resume', 'next', 'previous')
MAX_QUERY = 80
BUBBLE = 48
MARKER = '(acción de música)'

MESSAGES = {
    'not_connected': 'Conecta tu Spotify en la página del puffle.',
    'premium': 'Tu Spotify necesita Premium para esto.',
    'no_device': 'Abre Spotify en tu compu o el reproductor web.',
    'rate': 'Spotify pide esperar un poco.',
    'expired': 'Reconecta tu Spotify, por favor.',
    'not_found': 'No encontré esa canción.',
    'unverified': 'Se lo pedí a Spotify, sin confirmar.',
    'error': 'Spotify no respondió. Intenta otra vez.',
    'busy': 'Espera un momento entre órdenes.',
    'pause': 'Listo, pausé la música.',
    'resume': 'Listo, la música sigue.',
    'next': 'Listo, siguiente canción.',
    'previous': 'Listo, canción anterior.',
}


class SpotifyError(Exception):
    """args[0] is a MESSAGES key; never carries tokens or response bodies."""


def _norm(text):
    text = unicodedata.normalize('NFKD', text.casefold())
    return ''.join(c for c in text if not unicodedata.combining(c))


def bubble(text):
    text = ' '.join(''.join(' ' if c in '%|<>&' or unicodedata.category(c).startswith('C') else c
                            for c in str(text)).split())
    return text if len(text) <= BUBBLE else text[:BUBBLE - 1].rstrip() + '…'


_MUSIC = r'(?:spotify|musica|cancion|canciones|rola|tema|track)'
_NEG = re.compile(r'\b(?:no|nunca|jamas)\s+(?:me\s+)?(?:pongas|pon|reproduzcas|toques|pases|pauses|quites|cambies)\b')
_PLAY = re.compile(r'^(?:puffle[\s,:]+)?(?:por\s+favor\s+)?(?:ponme|pon|pone|reproduce|reproduceme|toca|tocame|play)\s+'
                   r'(?:en\s+spotify\s+)?(?:la\s+cancion\s+|la\s+rola\s+|el\s+tema\s+|la\s+de\s+)?(?P<q>.+?)'
                   r'(?:\s+(?:en|por)\s+spotify)?[\s.!¡?¿]*$')
_SIMPLE = (
    ('next', re.compile(r'\b(?:siguiente|la\s+que\s+sigue|salta(?:la)?|skip|cambia(?:la)?\s+de\s+(?:cancion|rola)|otra\s+(?:cancion|rola))\b')),
    ('previous', re.compile(r'\b(?:anterior|la\s+de\s+antes|regresa(?:la)?|vuelve\s+a\s+la\s+(?:otra|anterior))\b')),
    ('pause', re.compile(r'\b(?:pausa(?:la)?|pausar|deten(?:la)?|para\s+la\s+(?:musica|cancion|rola)|quita\s+la\s+musica|stop)\b')),
    ('resume', re.compile(r'\b(?:reanuda(?:la)?|continua(?:la)?|sigue\s+con\s+la\s+(?:musica|cancion)|dale\s+play|quita\s+(?:la\s+)?pausa)\b')),
)


def parse_command(text):
    """Owner's line -> {'action', 'query'} or None. Never raises on input."""
    if not isinstance(text, str):
        return None
    raw = ' '.join(text.split())[:200]
    phrase = _norm(raw).strip()
    if not phrase or _NEG.search(phrase) or phrase.startswith('!'):
        return None
    if re.fullmatch(r'(?:puffle[\s,:]+)?(?:sigue|reproduce)[\s.!¡?¿]*', phrase):
        return {'action': 'resume', 'query': ''}
    words = phrase.split()
    musical = re.search(r'\b' + _MUSIC + r'\b', phrase) is not None
    match = _PLAY.match(phrase)
    if match:
        query = match.group('q').strip(' .!?¡¿"\'«»')
        if query in ('musica', 'la musica', 'algo', 'play') or not query:
            return {'action': 'resume', 'query': ''} if musical or query == 'play' else None
        # Take the query from the owner's original text (accents preserved).
        original = _strip_query(raw, query)
        original = ' '.join(''.join(' ' if c in '%|<>&\\' or unicodedata.category(c).startswith('C') else c
                                    for c in original).split())
        return {'action': 'play', 'query': original[:MAX_QUERY]} if original else None
    # Short imperative or explicit music context, to avoid "para mí..." chatter.
    if len(words) > 7 and not musical:
        return None
    for action, pattern in _SIMPLE:
        if pattern.search(phrase) and (musical or len(words) <= 5):
            return {'action': action, 'query': ''}
    return None


def _strip_query(raw, normalized_query):
    """Find the query span in the original text by normalized length matching."""
    norm_chars = [(_norm(c), i) for i, c in enumerate(raw)]
    flat = ''.join(n for n, _ in norm_chars)
    pos = flat.rfind(normalized_query)
    if pos < 0:
        return normalized_query
    # Map normalized offsets back to original indices.
    offset, start, end = 0, None, None
    for n, i in norm_chars:
        if start is None and offset >= pos:
            start = i
        offset += len(n)
        if offset >= pos + len(normalized_query):
            end = i + 1
            break
    return raw[start or 0:end].strip()


def validate_intent(intent, context):
    """Accept a model-proposed intent only if the owner's latest line says the same."""
    if not isinstance(intent, dict) or set(intent) - {'action', 'query'} or 'action' not in intent:
        return None
    action, query = intent.get('action'), intent.get('query', '')
    if action not in ACTIONS or not isinstance(query, str) or len(query) > MAX_QUERY:
        return None
    if not context or context[-1][0] != 'player':
        return None
    parsed = parse_command(context[-1][1])
    if not parsed or parsed['action'] != action:
        return None
    if action == 'play' and _norm(query).strip() != _norm(parsed['query']).strip():
        return None
    return parsed


class SimulatedSpotify:
    """Per-player fake account for tests and the disposable bench. No network."""
    CATALOG = ('Despacito', 'Bohemian Rhapsody', 'La Bamba', 'Club Penguin Theme', 'Oye Como Va')

    def __init__(self, premium=True, device=True):
        self.premium, self.device = premium, device
        self.track, self.playing, self.index, self.calls = None, False, 0, []
        self.fail = None

    async def run(self, intent):
        self.calls.append(dict(intent))
        if self.fail:
            raise SpotifyError(self.fail)
        if not self.premium:
            raise SpotifyError('premium')
        if not self.device:
            raise SpotifyError('no_device')
        action = intent['action']
        if action == 'play':
            q = _norm(intent['query'])
            hit = next((t for t in self.CATALOG if q in _norm(t) or _norm(t) in q), None)
            if not hit:
                raise SpotifyError('not_found')
            self.track, self.playing = hit, True
            return 'Puse «%s» (simulado).' % hit
        if action in ('next', 'previous'):
            self.index = (self.index + (1 if action == 'next' else -1)) % len(self.CATALOG)
            self.track, self.playing = self.CATALOG[self.index], True
        else:
            self.playing = action == 'resume'
        return MESSAGES[action]


class SpotifyWebAPI:
    """Real backend: one player's own token (auth.access_token()). Stdlib HTTP."""
    API = 'https://api.spotify.com/v1'

    def __init__(self, auth, http=None, sleep=time.sleep, verify_tries=4):
        from .spotify_auth import default_http
        self.auth, self.http, self.sleep, self.verify_tries = auth, http or default_http, sleep, verify_tries

    async def run(self, intent):
        return await asyncio.to_thread(self._run, intent)

    def _call(self, method, path, body=None, retry=True):
        token = self.auth.access_token()
        if not token:
            raise SpotifyError('not_connected')
        headers = {'Authorization': 'Bearer ' + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        status, raw = self.http(method, self.API + path, headers, data)
        if status == 401 and retry:
            if not self.auth.access_token(force_refresh=True):
                raise SpotifyError('expired')
            return self._call(method, path, body, retry=False)
        if status == 401:
            raise SpotifyError('expired')
        if status == 429:
            raise SpotifyError('rate')
        reason = _reason(raw)
        if status == 403:
            raise SpotifyError('premium' if reason == 'PREMIUM_REQUIRED' else 'error')
        if status == 404 and reason in (None, 'NO_ACTIVE_DEVICE'):
            raise SpotifyError('no_device')
        if status >= 400:
            raise SpotifyError('error')
        if status == 204 or not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise SpotifyError('error')

    def _device(self):
        devices = (self._call('GET', '/me/player/devices') or {}).get('devices') or []
        usable = [d for d in devices if isinstance(d, dict) and not d.get('is_restricted') and d.get('id')]
        if any(d.get('is_active') for d in usable):
            return None  # Spotify targets the active device by default.
        if len(usable) == 1:
            return usable[0]['id']
        raise SpotifyError('no_device')

    def _current(self):
        state = self._call('GET', '/me/player') or {}
        item = state.get('item') or {}
        return item.get('uri'), bool(state.get('is_playing')), item.get('name')

    def _verify(self, check):
        for _ in range(self.verify_tries):
            self.sleep(0.5)
            if check(*self._current()):
                return True
        return False

    def _run(self, intent):
        action = intent['action']
        device = self._device()
        suffix = '?' + urllib.parse.urlencode({'device_id': device}) if device else ''
        before = self._current()[0]
        if action == 'play':
            q = urllib.parse.urlencode({'q': intent['query'], 'type': 'track', 'limit': 1})
            items = ((self._call('GET', '/search?' + q) or {}).get('tracks') or {}).get('items') or []
            if not items or not items[0].get('uri', '').startswith('spotify:track:'):
                raise SpotifyError('not_found')
            uri, name = items[0]['uri'], str(items[0].get('name') or '')
            self._call('PUT', '/me/player/play' + suffix, {'uris': [uri]})
            ok = self._verify(lambda cur, playing, _n: cur == uri and playing)
            album = (items[0].get('album') or {}).get('uri')
            if not ok and isinstance(album, str) and album.startswith('spotify:album:'):
                self._call('PUT', '/me/player/play' + suffix,
                           {'context_uri': album, 'offset': {'uri': uri}})
                ok = self._verify(lambda cur, playing, _n: cur == uri and playing)
            return ('Puse «%s».' % name) if ok else MESSAGES['unverified']
        if action in ('next', 'previous'):
            self._call('POST', '/me/player/' + action + suffix)
            ok = self._verify(lambda cur, playing, _n: cur is not None and cur != before)
        elif action == 'pause':
            self._call('PUT', '/me/player/pause' + suffix)
            ok = self._verify(lambda cur, playing, _n: cur is not None and not playing)
        else:
            self._call('PUT', '/me/player/play' + suffix)
            ok = self._verify(lambda cur, playing, _n: playing)
        return MESSAGES[action] if ok else MESSAGES['unverified']


def _reason(raw):
    try:
        err = json.loads(raw or b'{}').get('error')
    except (ValueError, AttributeError):
        return None
    return err.get('reason') if isinstance(err, dict) else None


class SpotifyAgent:
    """Wraps the conversation provider. Spotify commands never reach the model.

    accounts(player_key) -> backend or None (the player's own connection).
    Called by Engine.evaluate_for with the server-validated binding; in the
    single-owner local relay the key is 'local'.
    """
    def __init__(self, inner, accounts, *, min_interval=2.0, per_minute=8, clock=time.monotonic):
        self.inner, self.accounts = inner, accounts
        self.min_interval, self.per_minute, self.clock = min_interval, per_minute, clock
        self.history = {}  # player key -> recent action times (bounded)
        self.locks = {}

    def forget(self, key):
        self.history.pop(key, None)
        self.locks.pop(key, None)

    async def evaluate(self, context):
        return await self.evaluate_for('local', context)

    async def evaluate_for(self, key, context, active=lambda: True):
        context = tuple(context)
        intent = parse_command(context[-1][1]) if context and context[-1][0] == 'player' else None
        if intent is None:
            return await self.inner.evaluate(_scrub(context))
        backend = self.accounts(key)
        if backend is None:
            return True, MESSAGES['not_connected']
        now = self.clock()
        recent = [t for t in self.history.get(key, ()) if now - t < 60]
        if recent and (now - recent[-1] < self.min_interval or len(recent) >= self.per_minute):
            return True, MESSAGES['busy']
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return True, MESSAGES['busy']
        async with lock:
            if not active():
                return False, ''
            recent.append(now)
            self.history[key] = recent[-self.per_minute:]
            try:
                text = await backend.run(intent)
            except SpotifyError as error:
                text = MESSAGES.get(error.args[0] if error.args else 'error', MESSAGES['error'])
            except Exception:
                text = MESSAGES['error']
        return True, bubble(text)


def _scrub(context):
    """Never forward music commands or Spotify metadata to the language model."""
    out = []
    for role, text in context:
        if role == 'player' and parse_command(text):
            out.append((role, MARKER))
        elif role == 'puffle' and (text in MESSAGES.values() or text.startswith('Puse «')
                                   or text == bubble(MESSAGES['unverified'])):
            out.append((role, MARKER))
        else:
            out.append((role, text))
    return tuple(out)

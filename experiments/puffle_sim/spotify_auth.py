"""Spotify Authorization Code + PKCE for ONE local owner (no client secret).

The client ID is not secret: it is read from $PUFFLE_SPOTIFY_CLIENT_ID or from
~/.config/puffle-lab/spotify-client-id (a file Alexis edits). Tokens live in
~/.config/puffle-lab/spotify-credentials.json (0600, atomic), never in the repo,
logs, the game client or the browser. Python 3.9 compatible (connect_server).
Production needs per-player encrypted storage instead (docs/puffle-spotify.md).
"""
import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.parse

from .chatgpt_auth import DEFAULT_DIR, atomic_write_json, default_http, pkce_pair, read_json

AUTHORIZE_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
SCOPE = 'user-modify-playback-state user-read-playback-state'
PENDING_TTL = 600


class SpotifyAuthError(Exception):
    """Fixed, safe message (no tokens)."""


def redirect_uri(port):
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError('invalid port')
    return 'http://127.0.0.1:%d/spotify/callback' % port


class SpotifyAuth:
    def __init__(self, config_dir=None, http=None, clock=time.time, client_id=None, redirect=None):
        self.dir = Path(config_dir) if config_dir else DEFAULT_DIR
        self.http = http or default_http
        self.clock = clock
        self._client_id = client_id
        # A fixed same-origin redirect (puffle-host) instead of the :PORT loopback page.
        self._redirect = redirect
        self._pending = {}
        self._lock = threading.Lock()

    @property
    def cred_path(self):
        return self.dir / 'spotify-credentials.json'

    def client_id(self):
        value = self._client_id or os.environ.get('PUFFLE_SPOTIFY_CLIENT_ID')
        if not value:
            with contextlib.suppress(FileNotFoundError):
                value = (self.dir / 'spotify-client-id').read_text().strip()
        if value and value.isalnum() and 16 <= len(value) <= 64:
            return value
        return None

    def start(self, port=None, state=None):
        cid = self.client_id()
        if not cid:
            raise SpotifyAuthError('falta el Client ID de Spotify')
        uri = self._redirect or redirect_uri(port)
        verifier, challenge = pkce_pair()
        state = state or secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v['at'] > now - PENDING_TTL}
            self._pending[state] = {'verifier': verifier, 'uri': uri, 'at': now}
        return AUTHORIZE_URL + '?' + urllib.parse.urlencode({
            'response_type': 'code', 'client_id': cid, 'scope': SCOPE,
            'redirect_uri': uri, 'state': state,
            'code_challenge_method': 'S256', 'code_challenge': challenge})

    def complete(self, query):
        state = query.get('state', '')
        with self._lock:
            pending = None
            for key in list(self._pending):
                if hmac.compare_digest(key, state):
                    pending = self._pending.pop(key)
        if not pending or pending['at'] < self.clock() - PENDING_TTL:
            raise SpotifyAuthError('state inválido o expirado')
        if query.get('error'):
            raise SpotifyAuthError('cancelaste la conexión')
        code = query.get('code')
        if not code:
            raise SpotifyAuthError('falta el código')
        tok = self._token({'grant_type': 'authorization_code', 'code': code,
                           'redirect_uri': pending['uri'],
                           'code_verifier': pending['verifier']})
        granted = set((tok.get('scope') or '').split())
        if 'user-modify-playback-state' not in granted:
            raise SpotifyAuthError('no se concedió el permiso de reproducción')
        self._save(tok)

    def _token(self, form):
        form = dict(form, client_id=self.client_id())
        status, raw = self.http('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'},
                                urllib.parse.urlencode(form).encode())
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if status != 200 or not data.get('access_token'):
            err = data.get('error') if isinstance(data, dict) else None
            raise SpotifyAuthError('falló el canje (HTTP %d%s)' % (status, ', ' + err if isinstance(err, str) and err.isidentifier() else ''))
        return data

    # Credential storage hooks: a 0600 file locally; puffle-host stores encrypted rows.
    def _load(self):
        return read_json(self.cred_path)

    def _store(self, data):
        atomic_write_json(self.cred_path, data)

    def _erase(self):
        with contextlib.suppress(FileNotFoundError):
            self.cred_path.unlink()

    def _save(self, tok, previous=None):
        previous = previous or {}
        self._store({
            'access_token': tok['access_token'],
            'refresh_token': tok.get('refresh_token') or previous.get('refresh_token'),
            'expires_at': self.clock() + int(tok.get('expires_in', 3600)) - 60,
            'scope': tok.get('scope') or previous.get('scope', '')})

    def access_token(self, force_refresh=False):
        with self._lock:
            cred = self._load()
            if not cred:
                return None
            if not force_refresh and cred.get('expires_at', 0) > self.clock():
                return cred['access_token']
            if not cred.get('refresh_token'):
                return None
            try:
                tok = self._token({'grant_type': 'refresh_token', 'refresh_token': cred['refresh_token']})
            except SpotifyAuthError:
                return None
            self._save(tok, cred)
            return tok['access_token']

    def disconnect(self):
        # Spotify has no token-revocation endpoint: delete locally and tell the
        # owner to remove the app at spotify.com/account/apps.
        with self._lock:
            self._erase()

    def status(self):
        if not self.client_id():
            return 'sin_app'
        return 'conectado' if self._load() else 'desconectado'

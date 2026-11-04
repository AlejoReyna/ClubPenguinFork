"""Sign in with ChatGPT (open-source token sharing) for Puffle Lab, stdlib only.

Credentials live in ~/.config/puffle-lab/ (0600, atomic writes), never in the
repo, logs or the browser. Every network call goes through ``self.http`` so tests
run without network. Nothing here starts a login by itself.
"""
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ISSUER = 'https://auth.openai.com'
AUTHORIZE_URL = ISSUER + '/api/accounts/authorize'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
DISCOVERY_URL = ISSUER + '/.well-known/openid-configuration'
RESOURCE = 'https://api.openai.com/v1'
SCOPE = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
DYNAMIC_CLIENT = 'dynamic_agent_client'
AGENT_NAME = 'Puffle Lab'
PENDING_TTL = 600
DEFAULT_DIR = Path.home() / '.config' / 'puffle-lab'


class AuthError(Exception):
    """Message is safe to show; it never contains tokens."""


class PlanNotGranted(AuthError):
    pass


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def pkce_pair():
    verifier = b64url(secrets.token_bytes(32))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def redirect_uri(port):
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError('invalid port')
    return f'http://127.0.0.1:{port}/auth/callback'


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def default_http(method, url, headers=None, body=None, timeout=30):
    """Returns (status, body_bytes). Never logs anything."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- RS256 ID-token validation with stdlib (RSASSA-PKCS1-v1_5 + SHA-256) ---
_SHA256_PREFIX = bytes.fromhex('3031300d060960864801650304020105000420')


def rsa_verify_sha256(message, signature, n_b64, e_b64):
    n = int.from_bytes(b64url_decode(n_b64), 'big')
    e = int.from_bytes(b64url_decode(e_b64), 'big')
    k = (n.bit_length() + 7) // 8
    if k < 256 or len(signature) != k:  # require >= 2048-bit keys
        return False
    em = pow(int.from_bytes(signature, 'big'), e, n).to_bytes(k, 'big')
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    return hmac.compare_digest(em, expected)


def verify_id_token(token, jwks, *, issuer, audience, nonce, now=None, leeway=60):
    try:
        h64, p64, s64 = token.split('.')
        header = json.loads(b64url_decode(h64))
        claims = json.loads(b64url_decode(p64))
        sig = b64url_decode(s64)
    except Exception:
        raise AuthError('ID token mal formado')
    if header.get('alg') != 'RS256':
        raise AuthError('algoritmo de ID token no permitido')
    keys = [k for k in jwks.get('keys', []) if k.get('kty') == 'RSA'
            and k.get('kid') == header.get('kid') and k.get('use', 'sig') == 'sig']
    if not keys or not rsa_verify_sha256(f'{h64}.{p64}'.encode(), sig, keys[0]['n'], keys[0]['e']):
        raise AuthError('firma de ID token inválida')
    now = time.time() if now is None else now
    aud = claims.get('aud')
    auds = aud if isinstance(aud, list) else [aud]
    if claims.get('iss') != issuer:
        raise AuthError('emisor de ID token inválido')
    if audience not in auds:
        raise AuthError('audiencia de ID token inválida')
    if not isinstance(claims.get('exp'), (int, float)) or claims['exp'] + leeway < now:
        raise AuthError('ID token expirado')
    if not isinstance(claims.get('nonce'), str) or not hmac.compare_digest(claims['nonce'], nonce):
        raise AuthError('nonce de ID token inválido')
    if not isinstance(claims.get('sub'), str) or not claims['sub']:
        raise AuthError('ID token sin sujeto')
    return claims


class ChatGPTAuth:
    def __init__(self, config_dir=None, http=None, clock=time.time):
        self.dir = Path(config_dir) if config_dir else DEFAULT_DIR
        self.http = http or default_http
        self.clock = clock
        self.pending = {}
        self._mutex = threading.Lock()
        self._discovery = None
        # An invalid_grant retry may reuse an issued ID within this process,
        # but an unverified registration must never replace a saved account.
        self._retry_registration = None

    @property
    def cred_path(self):
        return self.dir / 'credentials.json'

    @property
    def state_path(self):
        return self.dir / 'state.json'

    # -- host id, persisted before the first login --
    def host_id(self):
        path = self.dir / 'host.json'
        data = read_json(path)
        if not data or not str(data.get('ext_agent_host_id', '')).startswith('urn:uuid:'):
            data = {'ext_agent_host_id': f'urn:uuid:{uuid.uuid4()}'}
            atomic_write_json(path, data)
        return data['ext_agent_host_id']

    def _json(self, method, url, form=None, headers=None):
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        hdrs = {'Accept': 'application/json', **(headers or {})}
        if body is not None:
            hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
        status, raw = self.http(method, url, hdrs, body)
        try:
            data = json.loads(raw or b'{}')
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}

    def discovery(self):
        if self._discovery is None:
            status, data = self._json('GET', DISCOVERY_URL)
            if status != 200 or data.get('issuer') != ISSUER:
                raise AuthError('no se pudo leer la configuración OpenID')
            self._discovery = data
        return self._discovery

    # -- authorization --
    def start(self, port):
        creds = read_json(self.cred_path)
        reg = read_json(self.dir / 'registration.json') or {}
        # Legacy files without a verified subject may have come from a failed
        # sign-in in another workspace; do not use them as account mappings.
        if not reg.get('subject'):
            reg = {}
        with self._mutex:
            retry = self._retry_registration
            self._retry_registration = None
        if retry and retry['at'] + PENDING_TTL <= self.clock():
            retry = None
        client_id = ((creds or {}).get('client_id') or reg.get('client_id') or
                     (retry or {}).get('client_id') or DYNAMIC_CLIENT)
        expected_subject = ((creds or {}).get('subject') or
                            (reg.get('subject') if reg.get('client_id') == client_id else None))
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        uri = redirect_uri(port)
        params = {
            'response_type': 'code', 'client_id': client_id, 'redirect_uri': uri,
            'scope': SCOPE, 'resource': RESOURCE, 'state': state, 'nonce': nonce,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'ext_agent_host_id': self.host_id(),
        }
        if client_id == DYNAMIC_CLIENT:
            params['agent_name_hint'] = AGENT_NAME
        elif (creds or {}).get('id_token'):
            params['id_token_hint'] = creds['id_token']
        now = self.clock()
        with self._mutex:
            self.pending = {k: v for k, v in self.pending.items() if v['at'] + PENDING_TTL > now}
            self.pending[state] = {'verifier': verifier, 'nonce': nonce, 'uri': uri,
                                   'client_id': client_id, 'expected_subject': expected_subject,
                                   'at': now}
        return AUTHORIZE_URL + '?' + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    def _take_pending(self, state):
        if not isinstance(state, str) or not state:
            raise AuthError('state inválido')
        with self._mutex:
            match = next((k for k in self.pending if hmac.compare_digest(k, state)), None)
            attempt = self.pending.pop(match, None) if match else None
        if not attempt or attempt['at'] + PENDING_TTL <= self.clock():
            raise AuthError('state inválido o expirado')
        return attempt

    def complete(self, query):
        """query: dict of callback params (single values). Returns safe status."""
        attempt = self._take_pending(query.get('state'))
        if query.get('error'):
            if query['error'] == 'access_denied':
                raise AuthError('cancelaste o no autorizaste la conexión')
            raise AuthError('OpenAI devolvió un error de autorización')
        code = query.get('code')
        if not code:
            raise AuthError('falta el código de autorización')
        cb_client = query.get('client_id')
        if attempt['client_id'] == DYNAMIC_CLIENT:
            if not cb_client or cb_client == DYNAMIC_CLIENT:
                raise AuthError('registro incompleto: no llegó client_id emitido')
            client_id = cb_client
        else:
            if cb_client and cb_client != attempt['client_id']:
                raise AuthError('client_id distinto al registrado')
            client_id = attempt['client_id']
        status, tok = self._json('POST', TOKEN_URL, {
            'grant_type': 'authorization_code', 'client_id': client_id, 'code': code,
            'code_verifier': attempt['verifier'], 'redirect_uri': attempt['uri'],
            'resource': RESOURCE})
        if status != 200 or not tok.get('access_token'):
            if tok.get('error') == 'invalid_grant' and not attempt['expected_subject']:
                with self._mutex:
                    self._retry_registration = {'client_id': client_id, 'at': self.clock()}
            # Safe diagnostics: HTTP status and OAuth error code only (no tokens, no description).
            raise AuthError('falló el canje del código; vuelve a intentarlo'
                            f' (HTTP {status}, error={str(tok.get("error", ""))[:60]})')
        scopes = str(tok.get('scope', '')).split()
        if PLAN_SCOPE not in scopes:
            raise PlanNotGranted('no concedió uso del plan')
        disc = self.discovery()
        _, jwks = self._json('GET', disc['jwks_uri'])
        claims = verify_id_token(tok.get('id_token', ''), jwks, issuer=ISSUER,
                                 audience=client_id, nonce=attempt['nonce'], now=self.clock())
        old = read_json(self.cred_path)
        if ((attempt['expected_subject'] and attempt['expected_subject'] != claims['sub']) or
                (old and old.get('subject') != claims['sub'])):
            raise AuthError('la cuenta no coincide con la registrada')
        self._save(tok, client_id, claims['sub'])
        atomic_write_json(self.dir / 'registration.json',
                          {'client_id': client_id, 'subject': claims['sub']})
        self.set_paused(False)
        return self.status()

    def _save(self, tok, client_id, subject, previous=None):
        now = self.clock()
        rec = dict(previous or {})
        rec.update({
            'issuer': ISSUER, 'subject': subject, 'client_id': client_id,
            'ext_agent_host_id': self.host_id(), 'access_token': tok['access_token'],
            'refresh_token': tok.get('refresh_token') or rec.get('refresh_token'),
            'token_type': tok.get('token_type', 'Bearer'),
            'expires_at': now + int(tok.get('expires_in', 3600)),
            'earliest_refresh_at': tok.get('earliest_refresh_at'),
            'scopes': sorted(str(tok.get('scope', '')).split()), 'saved_at': now,
        })
        if tok.get('id_token'):
            rec['id_token'] = tok['id_token']
        atomic_write_json(self.cred_path, rec)

    # -- refresh with a single renewer (thread lock + file lock) --
    @contextlib.contextmanager
    def _refresh_lock(self):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._mutex:
            fd = os.open(self.dir / 'refresh.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def access_token(self, force_refresh=False):
        creds = read_json(self.cred_path)
        if not creds:
            raise AuthError('no conectado')
        if not force_refresh and creds['expires_at'] - 120 > self.clock():
            return creds['access_token']
        with self._refresh_lock():
            creds = read_json(self.cred_path)  # another renewer may have won
            if not creds:
                raise AuthError('no conectado')
            if not force_refresh and creds['expires_at'] - 120 > self.clock():
                return creds['access_token']
            status, tok = self._json('POST', TOKEN_URL, {
                'grant_type': 'refresh_token', 'client_id': creds['client_id'],
                'refresh_token': creds['refresh_token'], 'resource': RESOURCE})
            if status != 200 or not tok.get('access_token'):
                raise AuthError('no se pudo renovar la sesión; vuelve a conectar')
            if PLAN_SCOPE not in str(tok.get('scope', '')).split():
                raise PlanNotGranted('no concedió uso del plan')
            self._save(tok, creds['client_id'], creds['subject'], previous=creds)
            return tok['access_token']

    def disconnect(self):
        creds = read_json(self.cred_path)
        # Upgrade an existing verified session's legacy registration before
        # removing its tokens, preserving reauthorization for that account.
        if creds and creds.get('client_id') and creds.get('subject'):
            atomic_write_json(self.dir / 'registration.json',
                              {'client_id': creds['client_id'], 'subject': creds['subject']})
        revoked = True
        if creds and creds.get('refresh_token'):
            try:
                endpoint = self.discovery().get('revocation_endpoint')
                status, _ = self._json('POST', endpoint, {
                    'token': creds['refresh_token'], 'token_type_hint': 'refresh_token',
                    'client_id': creds['client_id']})
                revoked = status == 200
            except Exception:
                revoked = False
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.cred_path)
        self.set_paused(False)
        return revoked

    # -- pause flag shared by the web page and the provider --
    def set_paused(self, paused):
        atomic_write_json(self.state_path, {'paused_at': self.clock() if paused else None})

    def paused(self):
        return bool((read_json(self.state_path) or {}).get('paused_at'))

    def status(self):
        if not self.cred_path.exists():
            return 'desconectado'
        return 'pausado' if self.paused() else 'conectado'

"""Offline tests for the ChatGPT connection: every endpoint is mocked."""
import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout

from experiments.puffle_sim import chatgpt_auth as ca
from experiments.puffle_sim.chatgpt_provider import ChatGPTProvider, ProviderError, build_request
from experiments.puffle_sim.engine import QuotaExhausted

SECRET_MARKERS = ('AT-secret', 'RT-secret', 'RT-new', 'AT-new')


def _is_probable_prime(n):
    if n % 2 == 0:
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(24):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        p = secrets.randbits(bits) | (1 << bits - 1) | 1
        if all(p % q for q in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31)) and _is_probable_prime(p):
            return p


def make_key():
    e = 65537
    while True:
        p, q = _prime(1024), _prime(1024)
        phi = (p - 1) * (q - 1)
        n = p * q
        if n.bit_length() == 2048 and phi % e:
            return n, e, pow(e, -1, phi)


def b64int(i):
    return ca.b64url(i.to_bytes((i.bit_length() + 7) // 8, 'big'))


def sign(claims, key, kid='k1', alg='RS256'):
    n, e, d = key
    h = ca.b64url(json.dumps({'alg': alg, 'kid': kid}).encode())
    p = ca.b64url(json.dumps(claims).encode())
    t = ca._SHA256_PREFIX + hashlib.sha256(f'{h}.{p}'.encode()).digest()
    em = b'\x00\x01' + b'\xff' * (256 - len(t) - 3) + b'\x00' + t
    s = pow(int.from_bytes(em, 'big'), d, n).to_bytes(256, 'big')
    return f'{h}.{p}.{ca.b64url(s)}'


class FakeOpenAI:
    """Mocks discovery, JWKS, token and revocation endpoints."""

    def __init__(self, key, scope=ca.SCOPE):
        self.key, self.scope, self.calls, self.nonce = key, scope, [], None
        self.sub = 'user-sub'

    def __call__(self, method, url, headers=None, body=None):
        form = dict(urllib.parse.parse_qsl(body.decode())) if body else {}
        self.calls.append((method, url, form))
        if url == ca.DISCOVERY_URL:
            return 200, json.dumps({'issuer': ca.ISSUER, 'jwks_uri': 'https://jwks.test/keys',
                                    'revocation_endpoint': 'https://auth.test/revoke'}).encode()
        if url == 'https://jwks.test/keys':
            n, e, _ = self.key
            return 200, json.dumps({'keys': [{'kty': 'RSA', 'kid': 'k1', 'n': b64int(n), 'e': b64int(e)}]}).encode()
        if url == ca.TOKEN_URL and form['grant_type'] == 'authorization_code':
            idt = sign({'iss': ca.ISSUER, 'aud': form['client_id'], 'exp': 2e9, 'nonce': self.nonce,
                        'sub': self.sub, 'email': 'x@y.z'}, self.key)
            return 200, json.dumps({'access_token': 'AT-secret', 'refresh_token': 'RT-secret', 'id_token': idt,
                                    'expires_in': 3600, 'scope': self.scope, 'token_type': 'Bearer'}).encode()
        if url == ca.TOKEN_URL and form['grant_type'] == 'refresh_token':
            return 200, json.dumps({'access_token': 'AT-new', 'refresh_token': 'RT-new',
                                    'expires_in': 3600, 'scope': self.scope}).encode()
        if url == 'https://auth.test/revoke':
            return 200, b'{}'
        return 404, b'{}'


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = make_key()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, 'puffle-lab')
        self.fake = FakeOpenAI(self.key)
        self.auth = ca.ChatGPTAuth(self.dir, http=self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, client_id='oaiapp_test'):
        url = self.auth.start(1455)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.fake.nonce = q['nonce']
        return self.auth.complete({'code': 'c0de', 'state': q['state'], 'client_id': client_id}), q

    def test_authorize_url(self):
        url = self.auth.start(1455)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(f'{parts.scheme}://{parts.netloc}{parts.path}', ca.AUTHORIZE_URL)
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.assertEqual(q['response_type'], 'code')
        self.assertEqual(q['client_id'], 'dynamic_agent_client')
        self.assertEqual(q['scope'], 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct')
        self.assertEqual(q['resource'], 'https://api.openai.com/v1')
        self.assertEqual(q['redirect_uri'], 'http://127.0.0.1:1455/auth/callback')
        self.assertEqual(q['agent_name_hint'], 'Puffle Lab')
        self.assertEqual(q['code_challenge_method'], 'S256')
        self.assertTrue(q['ext_agent_host_id'].startswith('urn:uuid:'))
        self.assertEqual(q['ext_agent_host_id'], self.auth.host_id())  # stable
        self.assertNotIn('+', parts.query)  # spaces encoded as %20
        self.assertGreaterEqual(len(q['state']), 32)
        self.assertGreaterEqual(len(q['nonce']), 32)

    def test_pkce_s256(self):
        verifier, challenge = ca.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertEqual(challenge, ca.b64url(hashlib.sha256(verifier.encode()).digest()))
        self.assertNotIn('=', challenge)
        url = self.auth.start(1455)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        pending = self.auth.pending[q['state']]
        self.assertEqual(q['code_challenge'], ca.b64url(hashlib.sha256(pending['verifier'].encode()).digest()))

    def test_login_success_exchange_and_permissions(self):
        status, q = self.login()
        self.assertEqual(status, 'conectado')
        token_call = next(c for c in self.fake.calls if c[1] == ca.TOKEN_URL)[2]
        self.assertEqual(token_call['client_id'], 'oaiapp_test')
        self.assertEqual(token_call['redirect_uri'], q['redirect_uri'])
        self.assertEqual(token_call['resource'], ca.RESOURCE)
        self.assertEqual(ca.b64url(hashlib.sha256(token_call['code_verifier'].encode()).digest()), q['code_challenge'])
        for path in (self.auth.cred_path, os.path.join(self.dir, 'host.json')):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        creds = ca.read_json(self.auth.cred_path)
        self.assertEqual(creds['client_id'], 'oaiapp_test')
        self.assertNotIn('email', creds)
        # returning sign-in reuses the issued client id
        q2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.auth.start(1456)).query))
        self.assertEqual(q2['client_id'], 'oaiapp_test')
        self.assertNotIn('agent_name_hint', q2)

    def test_rejects_wrong_or_reused_state(self):
        url = self.auth.start(1455)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        with self.assertRaises(ca.AuthError):
            self.auth.complete({'code': 'c', 'state': 'forged', 'client_id': 'oaiapp_x'})
        with self.assertRaises(ca.AuthError):
            self.auth.complete({'code': 'c', 'state': '', 'client_id': 'oaiapp_x'})
        self.fake.nonce = q['nonce']
        self.auth.complete({'code': 'c', 'state': q['state'], 'client_id': 'oaiapp_x'})
        with self.assertRaises(ca.AuthError):  # single use
            self.auth.complete({'code': 'c', 'state': q['state'], 'client_id': 'oaiapp_x'})

    def test_rejects_missing_issued_client_id(self):
        url = self.auth.start(1455)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        with self.assertRaises(ca.AuthError):
            self.auth.complete({'code': 'c', 'state': q['state'], 'client_id': 'dynamic_agent_client'})
        self.assertFalse(any(c[1] == ca.TOKEN_URL for c in self.fake.calls))

    def test_rejects_without_plan_scope_and_saves_nothing(self):
        self.fake.scope = 'openid profile email offline_access'
        with self.assertRaises(ca.PlanNotGranted) as cm:
            self.login()
        self.assertIn('no concedió uso del plan', str(cm.exception))
        self.assertFalse(self.auth.cred_path.exists())
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'registration.json')))

    def test_rejects_bad_nonce_or_signature(self):
        url = self.auth.start(1455)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.fake.nonce = 'other'
        with self.assertRaises(ca.AuthError):
            self.auth.complete({'code': 'c', 'state': q['state'], 'client_id': 'oaiapp_x'})
        self.assertFalse(self.auth.cred_path.exists())
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'registration.json')))
        tok = sign({'iss': ca.ISSUER, 'aud': 'a', 'exp': 2e9, 'nonce': 'n', 'sub': 's'}, self.key)
        jwks = json.loads(self.fake('GET', 'https://jwks.test/keys')[1])
        h, p, s = tok.split('.')
        tampered = f'{h}.{ca.b64url(json.dumps({"iss": ca.ISSUER, "aud": "a", "exp": 2e9, "nonce": "n", "sub": "evil"}).encode())}.{s}'
        with self.assertRaises(ca.AuthError):
            ca.verify_id_token(tampered, jwks, issuer=ca.ISSUER, audience='a', nonce='n')
        with self.assertRaises(ca.AuthError):
            ca.verify_id_token(tok, jwks, issuer=ca.ISSUER, audience='b', nonce='n')
        self.assertEqual(ca.verify_id_token(tok, jwks, issuer=ca.ISSUER, audience='a', nonce='n')['sub'], 's')

    def test_access_denied(self):
        url = self.auth.start(1455)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        with self.assertRaises(ca.AuthError):
            self.auth.complete({'error': 'access_denied', 'state': q['state']})
        self.assertFalse(any(c[1] == ca.TOKEN_URL for c in self.fake.calls))

    def test_registration_is_committed_only_after_validated_identity(self):
        self.login()
        reg = ca.read_json(os.path.join(self.dir, 'registration.json'))
        self.assertEqual(reg, {'client_id': 'oaiapp_test', 'subject': 'user-sub'})
        self.assertTrue(self.auth.disconnect())
        self.fake.sub = 'another-user'
        with self.assertRaises(ca.AuthError):
            self.login()
        self.assertFalse(self.auth.cred_path.exists())
        self.assertEqual(ca.read_json(os.path.join(self.dir, 'registration.json')), reg)

    def test_legacy_unverified_registration_is_not_used(self):
        ca.atomic_write_json(os.path.join(self.dir, 'registration.json'), {'client_id': 'oaiapp_unverified'})
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.auth.start(1455)).query))
        self.assertEqual(q['client_id'], ca.DYNAMIC_CLIENT)

    def test_invalid_grant_retries_issued_id_without_persisting_it(self):
        original = self.auth.http
        def fail_exchange(method, url, headers=None, body=None):
            if url == ca.TOKEN_URL:
                return 400, b'{"error":"invalid_grant"}'
            return original(method, url, headers, body)
        self.auth.http = fail_exchange
        with self.assertRaises(ca.AuthError):
            self.login('oaiapp_retry')
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'registration.json')))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.auth.start(1455)).query))
        self.assertEqual(q['client_id'], 'oaiapp_retry')
        self.auth.http = original
        self.fake.nonce = q['nonce']
        self.auth.complete({'code': 'fresh', 'state': q['state']})
        self.assertEqual(ca.read_json(os.path.join(self.dir, 'registration.json'))['subject'], 'user-sub')

    def test_refresh_single_renewer_and_disconnect(self):
        self.login()
        creds = ca.read_json(self.auth.cred_path)
        creds['expires_at'] = 0
        ca.atomic_write_json(self.auth.cred_path, creds)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.auth.access_token())) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(set(results), {'AT-new'})
        refreshes = [c for c in self.fake.calls if c[2].get('grant_type') == 'refresh_token']
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(refreshes[0][2]['client_id'], 'oaiapp_test')
        self.assertEqual(ca.read_json(self.auth.cred_path)['refresh_token'], 'RT-new')
        self.assertTrue(self.auth.disconnect())
        revoke = [c for c in self.fake.calls if c[1] == 'https://auth.test/revoke'][0][2]
        self.assertEqual(revoke['token'], 'RT-new')
        self.assertFalse(self.auth.cred_path.exists())
        self.assertEqual(self.auth.status(), 'desconectado')

    def test_no_tokens_in_logs_or_output(self):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(buf_err)
        logging.getLogger().addHandler(handler)
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                self.login()
                self.auth.access_token(force_refresh=True)
                self.auth.disconnect()
        finally:
            logging.getLogger().removeHandler(handler)
        captured = buf_out.getvalue() + buf_err.getvalue()
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, captured)


def sse(*events):
    return [f'data: {json.dumps(e)}'.encode() for e in events]


class ProviderCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_blocking_request_cannot_overlap_next_request(self):
        entered, release = threading.Event(), threading.Event()
        provider = ChatGPTProvider(auth=object(), http=lambda *args: None)
        def blocking(context):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timed out')
            return True, 'listo'
        provider._evaluate = blocking
        first = asyncio.create_task(provider.evaluate((('player', 'hola'),)))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        try:
            with self.assertRaises(ProviderError):
                await provider.evaluate((('player', 'segundo'),))
        finally:
            release.set()
        for _ in range(100):
            if not provider._inflight.locked():
                break
            await asyncio.sleep(0.01)
        self.assertFalse(provider._inflight.locked())
        self.assertEqual(await provider.evaluate((('player', 'nuevo'),)), (True, 'listo'))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = ca.ChatGPTAuth(self.tmp.name, http=self.http)
        ca.atomic_write_json(self.auth.cred_path, {'client_id': 'oaiapp_t', 'subject': 's',
                             'access_token': 'AT-secret', 'refresh_token': 'RT-secret', 'expires_at': 2e9})
        self.sent, self.responses, self.sleeps = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def http(self, method, url, headers=None, body=None):
        return 200, json.dumps({'models': [{'slug': 'hidden', 'visibility': 'hide'},
                                           {'slug': 'm-list', 'visibility': 'list'},
                                           {'slug': 'm-2', 'visibility': 'list'}]}).encode()

    def transport(self, url, headers, body):
        self.sent.append((url, headers, json.loads(body)))
        return self.responses.pop(0)

    def provider(self):
        return ChatGPTProvider(self.auth, transport=self.transport, sleep=self.sleeps.append)

    def run_eval(self, ctx=(('player', 'hola puffle?'),)):
        return asyncio.run(self.provider().evaluate(ctx))

    def test_request_shape_no_tools_no_store(self):
        self.responses.append((200, sse({'type': 'response.output_text.delta', 'delta': '¡Hola! <b>|%'},
                                         {'type': 'response.completed'})))
        self.assertEqual(self.run_eval(), (True, '¡Hola! b'))
        url, headers, body = self.sent[0]
        self.assertEqual(url, 'https://api.openai.com/v1/responses')
        self.assertEqual(headers['Authorization'], 'Bearer AT-secret')
        self.assertEqual(body['model'], 'm-list')
        self.assertIs(body['store'], False)
        self.assertIs(body['stream'], True)
        self.assertIn('instructions', body)
        for banned in ('tools', 'tool_choice', 'max_output_tokens', 'temperature', 'top_p'):
            self.assertNotIn(banned, body)
        self.assertEqual(set(build_request('m', [('player', 'x')])),
                         {'model', 'instructions', 'input', 'store', 'stream'})

    def test_success_requires_completed(self):
        self.responses.append((200, sse({'type': 'response.output_text.delta', 'delta': 'hola'})))
        with self.assertRaises(Exception):
            self.run_eval()

    def test_silence_means_no_reply(self):
        self.responses.append((200, sse({'type': 'response.output_text.delta', 'delta': '(silencio)'},
                                         {'type': 'response.completed'})))
        self.assertEqual(self.run_eval(), (False, ''))

    def test_429_pauses(self):
        self.responses.append((429, [json.dumps({'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}).encode()]))
        with self.assertRaises(QuotaExhausted):
            self.run_eval()
        self.assertEqual(self.auth.status(), 'pausado')
        with self.assertRaises(QuotaExhausted):  # paused: no new request
            self.run_eval()
        self.assertEqual(len(self.sent), 1)

    def test_quota_mid_stream_pauses(self):
        self.responses.append((200, sse({'type': 'response.failed', 'response': {
            'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}})))
        with self.assertRaises(QuotaExhausted):
            self.run_eval()
        self.assertTrue(self.auth.paused())

    def test_503_bounded_retry(self):
        err = (503, [json.dumps({'error': {'code': 'subscription_sharing_usage_unavailable'}}).encode()])
        self.responses += [err, err, (200, sse({'type': 'response.output_text.delta', 'delta': 'ok'},
                                               {'type': 'response.completed'}))]
        self.assertEqual(self.run_eval(), (True, 'ok'))
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.responses += [err, err, err]
        self.sent.clear()
        with self.assertRaises(Exception):
            self.run_eval()
        self.assertEqual(len(self.sent), 3)

    def test_engine_with_quota_reply(self):
        from experiments.puffle_sim.provider import make_provider, SimulatedProcess
        self.assertIsInstance(make_provider(), SimulatedProcess)
        self.assertIsInstance(make_provider('chatgpt', auth=self.auth), ChatGPTProvider)
        with self.assertRaises(ValueError):
            make_provider('otro')


class ServerTests(unittest.TestCase):
    def test_page_csrf_and_no_secrets(self):
        import http.client
        from http.server import ThreadingHTTPServer
        from experiments.puffle_sim.connect_server import make_handler
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        auth = ca.ChatGPTAuth(tmp.name, http=lambda *a: (500, b''))
        srv = ThreadingHTTPServer(('127.0.0.1', 0), None)
        port = srv.server_address[1]
        srv.RequestHandlerClass = make_handler(auth, port, 'csrf-token')
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def req(method, path, body=None, host=f'127.0.0.1:{port}'):
            c = http.client.HTTPConnection('127.0.0.1', port)
            c.request(method, path, body, {'Host': host, 'Content-Type': 'application/x-www-form-urlencoded'})
            r = c.getresponse()
            result = r.status, r.getheader('Location'), r.read().decode()
            c.close()
            return result

        status, _, page = req('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('Conectar mi ChatGPT', page)
        self.assertIn('Desconectado', page)
        self.assertIn('Puffle Lab', page)
        self.assertEqual(req('GET', '/', host='evil.test')[0], 421)
        self.assertEqual(req('POST', '/connect', 'csrf=bad')[0], 403)
        status, loc, _ = req('POST', '/connect', 'csrf=csrf-token')
        self.assertEqual(status, 303)
        self.assertTrue(loc.startswith(ca.AUTHORIZE_URL))
        status, loc, _ = req('GET', '/auth/callback?code=x&state=forged&client_id=oaiapp_x')
        self.assertEqual((status, loc), (303, '/?m=state'))
        ca.atomic_write_json(auth.cred_path, {'access_token': 'AT-secret', 'refresh_token': 'RT-secret',
                                              'client_id': 'oaiapp_x', 'email': 'x@y.z'})
        _, _, page = req('GET', '/')
        self.assertIn('Conectado', page)
        for leak in ('AT-secret', 'RT-secret', 'x@y.z', 'oaiapp_x'):
            self.assertNotIn(leak, page)

    def test_local_status_cors(self):
        import http.client
        import json
        from http.server import ThreadingHTTPServer
        from experiments.puffle_sim.connect_server import make_handler
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        auth = ca.ChatGPTAuth(tmp.name, http=lambda *a: (500, b''))
        spotify_dir = Path(tmp.name) / 'spotify'
        spotify_dir.mkdir()
        (spotify_dir / 'spotify-client-id').write_text('a' * 32)
        from experiments.puffle_sim.spotify_auth import SpotifyAuth
        spotify = SpotifyAuth(spotify_dir)
        srv = ThreadingHTTPServer(('127.0.0.1', 0), None)
        port = srv.server_address[1]
        srv.RequestHandlerClass = make_handler(auth, port, 'csrf-token', spotify=spotify)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        origin = f'http://127.0.0.1:8080'

        def req(path, origin_header=origin):
            c = http.client.HTTPConnection('127.0.0.1', port)
            c.request('GET', path, headers={'Host': f'127.0.0.1:{port}', 'Origin': origin_header})
            r = c.getresponse()
            body = r.read()
            headers = {k.lower(): v for k, v in r.getheaders()}
            c.close()
            return r.status, headers, body

        self.assertEqual(req('/api/local-status', 'https://evil.test')[0], 403)
        status, headers, body = req('/api/local-status')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('access-control-allow-origin'), origin)
        data = json.loads(body)
        self.assertEqual(data['chatgpt'], 'desconectado')
        self.assertEqual(data['spotify'], 'desconectado')


if __name__ == '__main__':
    unittest.main()

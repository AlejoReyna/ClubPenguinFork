"""The provider client: request shape, bounded retries and error classes.

The fake Kimi server never leaves the loopback interface and is fed no real
credentials: the API key is a test value.
"""
import asyncio
import json
import unittest

from bots.metrics import BotMetrics, Registry
from bots.provider import MoonshotClient, ProviderError, SimulatedProvider


def success_body(content='Hola! La isla esta al final del muelle.'):
    return json.dumps({
        'choices': [{'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 61, 'completion_tokens': 9},
    }).encode()


class FakeKimi:
    """HTTP/1.1 stand-in for {base_url}/chat/completions on the loopback."""

    def __init__(self):
        self.requests = []
        self.responses = []  # (status, raw bytes); last entry repeats forever
        self.delay = 0.0
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(self, reader, writer):
        try:
            request_line = (await reader.readline()).decode()
            parts = request_line.split()
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b'', b'\r\n', b'\n'):
                    break
                key, _, value = line.decode().partition(':')
                headers[key.lower().strip()] = value.strip()
            length = int(headers.get('content-length', '0'))
            body = await reader.readexactly(length) if length else b'{}'
            self.requests.append((parts[0], parts[1],
                                  headers.get('authorization'),
                                  json.loads(body.decode())))
            if self.delay:
                await asyncio.sleep(self.delay)
            status, raw = self._next_response()
            raw = raw if isinstance(raw, bytes) else raw.encode()
            writer.write(
                f'HTTP/1.1 {status} fake\r\n'.encode()
                + b'Content-Type: application/json\r\n'
                + f'Content-Length: {len(raw)}\r\n'.encode()
                + b'Connection: close\r\n\r\n' + raw)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def _next_response(self):
        if not self.responses:
            return 200, success_body()
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]


class MoonshotClientTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.kimi = FakeKimi()
        await self.kimi.start()
        self.base_url = f'http://127.0.0.1:{self.kimi.port}/v1'
        self.registry = Registry()
        self.metrics = BotMetrics(self.registry)

    async def asyncTearDown(self):
        await self.kimi.stop()

    def make_client(self, **overrides):
        defaults = dict(base_url=self.base_url, api_key='sk-fake-test-key',
                        model='kimi-k2.6', max_completion_tokens=120,
                        timeout=10.0, retries=2, retry_wait=0.01,
                        thinking='disabled', max_concurrent=2,
                        metrics=self.metrics)
        defaults.update(overrides)
        return MoonshotClient(**defaults)

    async def test_posts_the_verified_payload(self):
        client = self.make_client()
        reply, usage = await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(reply, 'Hola! La isla esta al final del muelle.')
        self.assertEqual(usage['completion_tokens'], 9)
        method, path, auth, payload = self.kimi.requests[0]
        self.assertEqual((method, path), ('POST', '/v1/chat/completions'))
        self.assertEqual(auth, 'Bearer sk-fake-test-key')
        self.assertEqual(payload['model'], 'kimi-k2.6')
        self.assertEqual(payload['max_completion_tokens'], 120)
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        # K2.6 declares no temperature; sending one is not supported.
        self.assertNotIn('temperature', payload)
        text = self.registry.render()
        self.assertIn('bots_provider_requests_total{result="ok"} 1', text)
        self.assertIn('bots_provider_latency_seconds', text)
        self.assertIn('bots_provider_prompt_tokens_total 61', text)
        self.assertIn('bots_provider_completion_tokens_total 9', text)

    async def test_thinking_auto_omits_the_field(self):
        client = self.make_client(thinking='auto')
        await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertNotIn('thinking', self.kimi.requests[0][3])

    async def test_retries_bounded_on_429_then_succeeds(self):
        self.kimi.responses = [(429, b'{"error":"rate"}'),
                               (429, b'{"error":"rate"}'),
                               (200, success_body())]
        client = self.make_client(retries=2, retry_wait=0.01)
        reply, _ = await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertTrue(reply)
        self.assertEqual(len(self.kimi.requests), 3)
        text = self.registry.render()
        self.assertIn('bots_provider_errors_total{kind="http_429"} 2', text)
        self.assertIn('bots_provider_requests_total{result="ok"} 1', text)

    async def test_429_exhausts_retries_and_raises(self):
        self.kimi.responses = [(429, b'{"error":"rate"}')]
        client = self.make_client(retries=2, retry_wait=0.01)
        with self.assertRaises(ProviderError) as caught:
            await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(caught.exception.kind, 'http_429')
        self.assertEqual(len(self.kimi.requests), 3)  # initial + 2 retries
        self.assertIn('bots_provider_errors_total{kind="http_429"} 3',
                      self.registry.render())

    async def test_5xx_is_retriable(self):
        self.kimi.responses = [(500, b'boom'), (200, success_body('recuperado'))]
        client = self.make_client(retries=2, retry_wait=0.01)
        reply, _ = await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(reply, 'recuperado')
        self.assertIn('bots_provider_errors_total{kind="http_5xx"} 1',
                      self.registry.render())

    async def test_4xx_is_not_retried(self):
        self.kimi.responses = [(401, b'{"error":"bad key"}')]
        client = self.make_client(retries=3, retry_wait=0.01)
        with self.assertRaises(ProviderError) as caught:
            await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(caught.exception.kind, 'http_4xx')
        self.assertEqual(len(self.kimi.requests), 1)

    async def test_empty_content_raises_empty(self):
        self.kimi.responses = [(200, success_body('   '))]
        client = self.make_client(retries=1)
        with self.assertRaises(ProviderError) as caught:
            await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(caught.exception.kind, 'empty')
        self.assertIn('bots_provider_errors_total{kind="empty"} 1',
                      self.registry.render())

    async def test_unreadable_json_raises_empty(self):
        self.kimi.responses = [(200, b'nada de json')]
        client = self.make_client(retries=1)
        with self.assertRaises(ProviderError) as caught:
            await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(caught.exception.kind, 'empty')

    async def test_timeout_becomes_timeout_kind(self):
        self.kimi.delay = 3.0
        client = self.make_client(timeout=0.4, retries=1)
        with self.assertRaises(ProviderError) as caught:
            await client.complete([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(caught.exception.kind, 'timeout')

    async def test_missing_api_key_is_rejected_up_front(self):
        with self.assertRaises(ValueError):
            MoonshotClient(base_url='http://127.0.0.1/v1', api_key='')


class SimulatedProviderTests(unittest.IsolatedAsyncioTestCase):

    async def test_replies_in_order_and_records_messages(self):
        provider = SimulatedProvider(replies=['frase uno', 'frase dos'])
        first, usage = await provider.complete([{'role': 'user', 'content': 'a'}])
        second, _ = await provider.complete([{'role': 'user', 'content': 'b'}])
        self.assertEqual((first, second), ('frase uno', 'frase dos'))
        self.assertEqual(usage['prompt_tokens'], 64)
        self.assertEqual(len(provider.messages_received), 2)

    async def test_failure_sequence_replays_kinds(self):
        provider = SimulatedProvider(replies=['vale'],
                                     fail_kinds=['timeout', 'http_5xx'])
        with self.assertRaises(ProviderError) as first:
            await provider.complete([{'role': 'user', 'content': 'a'}])
        self.assertEqual(first.exception.kind, 'timeout')
        with self.assertRaises(ProviderError) as second:
            await provider.complete([{'role': 'user', 'content': 'b'}])
        self.assertEqual(second.exception.kind, 'http_5xx')
        reply, _ = await provider.complete([{'role': 'user', 'content': 'c'}])
        self.assertEqual(reply, 'vale')


if __name__ == '__main__':
    unittest.main()

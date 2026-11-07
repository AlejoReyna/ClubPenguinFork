"""Prometheus registry rendering and the metrics HTTP server."""
import asyncio
import inspect
import socket
import unittest

from bots import behavior
from bots import metrics
from bots.metrics import BotMetrics, MetricsServer, Registry


def run_async(coroutine):
    return asyncio.run(coroutine)


def http_body(port, path):
    """GET one URL and return (status_line, body)."""

    async def request():
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
        await writer.drain()
        data = b''
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            data += chunk
        writer.close()
        head, _, body = data.decode().partition('\r\n\r\n')
        return head.split('\r\n', 1)[0], body

    return run_async(request())


class RegistryTests(unittest.TestCase):

    def test_facade_series_render(self):
        registry = Registry()
        facade = BotMetrics(registry)

        async def drive():
            await facade.mark_up()
            await facade.connected(0, True)
            await facade.connected(0, False)
            await facade.session_established(0)
            await facade.reconnected('1')
            await facade.auth_failed(2)
            await facade.chat_received(0)
            await facade.chat_sent(0)
            await facade.skipped(0, metrics.SKIP_SELF)
            await facade.skipped(0, metrics.SKIP_QUEUE_FULL)
            await facade.fallback_used(1)
            await facade.provider_ok(0.3)
            await facade.provider_error('http_429')
            await facade.add(metrics.PROVIDER_PROMPT_TOKENS, 64)
            await facade.add(metrics.PROVIDER_COMPLETION_TOKENS, 12)

        run_async(drive())
        text = registry.render()
        self.assertIn('# TYPE bots_up gauge', text)
        self.assertIn('bots_up 1', text)
        self.assertIn('bots_connected{bot="0"} 0', text)
        self.assertIn('bots_sessions_total{bot="0"} 1', text)
        self.assertIn('bots_reconnects_total{bot="1"} 1', text)
        self.assertIn('bots_auth_failures_total{bot="2"} 1', text)
        self.assertIn('bots_chats_received_total{bot="0"} 1', text)
        self.assertIn('bots_chats_sent_total{bot="0"} 1', text)
        self.assertIn('bots_replies_skipped_total{bot="0",reason="self"} 1', text)
        self.assertIn('bots_replies_skipped_total{bot="0",reason="queue_full"} 1', text)
        self.assertIn('bots_provider_fallback_total{bot="1"} 1', text)
        self.assertIn('bots_provider_requests_total{result="ok"} 1', text)
        self.assertIn('bots_provider_errors_total{kind="http_429"} 1', text)
        self.assertIn('bots_provider_prompt_tokens_total 64', text)
        self.assertIn('bots_provider_completion_tokens_total 12', text)

    def test_histogram_rendering(self):
        registry = Registry()

        async def drive():
            await registry.observe('bots_provider_latency_seconds', 0.8)
            await registry.observe('bots_provider_latency_seconds', 3.0)

        run_async(drive())
        text = registry.render()
        self.assertIn('# TYPE bots_provider_latency_seconds histogram', text)
        self.assertIn('bots_provider_latency_seconds_bucket{le="0.25"} 0', text)
        self.assertIn('bots_provider_latency_seconds_bucket{le="1"} 1', text)
        self.assertIn('bots_provider_latency_seconds_bucket{le="5"} 2', text)
        self.assertIn('bots_provider_latency_seconds_bucket{le="+Inf"} 2', text)
        self.assertIn('bots_provider_latency_seconds_sum 3.8', text)
        self.assertIn('bots_provider_latency_seconds_count 2', text)

    def test_labels_are_escaped_and_sorted(self):
        registry = Registry()

        async def drive():
            await registry.add('bots_replies_skipped_total',
                               labels={'reason': 'say "hi"', 'bot': '0'})

        run_async(drive())
        self.assertIn('bots_replies_skipped_total{bot="0",reason="say \\"hi\\""} 1',
                      registry.render())

    def test_skip_reasons_stay_bounded_identifiers(self):
        allowed = {metrics.SKIP_SELF, metrics.SKIP_BOT, metrics.SKIP_NOT_MENTIONED,
                   metrics.SKIP_BOT_GAP, metrics.SKIP_RATE_LIMIT,
                   metrics.SKIP_PLAYER_COOLDOWN, metrics.SKIP_QUEUE_FULL,
                   metrics.SKIP_EMPTY_REPLY}
        for reason in allowed:
            self.assertRegex(reason, r'^[a-z_]+$')
        # Every skip call sites a constant, never free text.
        source = inspect.getsource(behavior)
        for call in ('skipped(self.bot.index, metrics_mod.',):
            self.assertIn(call, source)
        self.assertNotIn("skipped(self.bot.index, '", source)

    def test_empty_registry_renders_a_comment(self):
        self.assertIn('no metrics recorded', Registry().render())

    def test_live_gauge_overrides_push_update_without_duplicate_samples(self):
        registry = Registry()
        run_async(BotMetrics(registry).ledger_state(False))
        registry.collect(metrics.SPEND_LEDGER_OK, lambda: [({}, 1)])
        samples = [line for line in registry.render().splitlines()
                   if line.startswith('bots_spend_ledger_ok ')]
        self.assertEqual(samples, ['bots_spend_ledger_ok 1'])


class MetricsServerTests(unittest.IsolatedAsyncioTestCase):

    async def request(self, port, path):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=3)
        writer.close()
        await writer.wait_closed()
        head, _, body = data.decode().partition('\r\n\r\n')
        return head.split('\r\n', 1)[0], body

    async def test_metrics_and_healthz_endpoints(self):
        registry = Registry()
        await BotMetrics(registry).connected(0, True)
        ready = [False]
        server = MetricsServer(registry, 0, ready=lambda: ready[0])
        # Port 0 is reserved by the OS directly; only negative ports disable metrics.
        server.port = free_tcp_port()
        await server.start()
        try:
            status, body = await self.request(server.port, '/metrics')
            self.assertEqual(status, 'HTTP/1.1 200 OK')
            self.assertIn('bots_connected{bot="0"} 1', body)
            self.assertEqual(await self.request(server.port, '/healthz'), ('HTTP/1.1 200 OK', 'ok'))
            self.assertIn('503', (await self.request(server.port, '/readyz'))[0])
            ready[0] = True
            self.assertEqual(await self.request(server.port, '/readyz'), ('HTTP/1.1 200 OK', 'ready'))
            self.assertIn('404', (await self.request(server.port, '/nope'))[0])
        finally:
            await server.stop()
        self.assertIsNone(server.server)

    async def test_disabled_port_starts_nothing(self):
        server = MetricsServer(Registry(), -1)
        await server.start()
        self.assertIsNone(server.server)
        await server.stop()


def free_tcp_port():
    """Reserve an ephemeral port for a server that starts immediately after."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


if __name__ == '__main__':
    unittest.main()

"""Provider quota persists across restarts, and retries count as real attempts.

The ledger itself (USD caps, priorities, fail-closed, the legacy file) is in
test_spend.py; this file covers how the manager and the provider client use it.
"""
import tempfile
import unittest
from pathlib import Path

from bots.config import build_config
from bots.manager import BotManager
from bots.metrics import Registry, BotMetrics
from bots.provider import MoonshotClient, ProviderError
from bots.spend import SpendLedger
from support import config_document_world
from test_provider import FakeKimi


class ManagerProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_connects_metrics_and_persists_global_budget(self):
        server = FakeKimi()
        await server.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                doc = config_document_world(6112, **{
                    'provider.mode': 'api', 'provider.base_url': f'http://127.0.0.1:{server.port}/v1',
                    'provider.budget_path': str(Path(temp) / 'budget.json'),
                    'policy.requests_per_minute': 1,
                })
                config = build_config(doc, {'MOONSHOT_API_KEY': 'only-synthetic'})
                registry = Registry()
                first = BotManager(config, registry)
                await first.provider.complete([{'role': 'user', 'content': 'synthetic'}])
                self.assertIn('bots_provider_requests_total{result="ok"} 1', registry.render())
                self.assertIn('bots_provider_prompt_tokens_total', registry.render())
                # Reconciled with the reply's usage (61 in, 9 out at 0.95/4.00 per million).
                self.assertIn('bots_spend_usd_micros_total{priority="P0"} 94', registry.render())
                self.assertIn('bots_spend_cap_ratio', registry.render())
                restarted = BotManager(config, Registry())
                with self.assertRaises(ProviderError) as caught:
                    await restarted.provider.complete([{'role': 'user', 'content': 'synthetic'}])
                self.assertEqual(caught.exception.kind, 'budget')
                self.assertEqual(len(server.requests), 1)
        finally:
            await server.stop()

    async def test_retries_consume_budget_and_stop_before_an_extra_attempt(self):
        server = FakeKimi()
        server.responses = [(429, b'{}')]
        await server.start()
        try:
            registry = Registry()
            ledger = SpendLedger(None, per_minute=2, max_requests_per_day=3)
            client = MoonshotClient(f'http://127.0.0.1:{server.port}/v1', 'only-synthetic',
                                    retries=3, retry_wait=0.01,
                                    ledger=ledger, metrics=BotMetrics(registry))
            with self.assertRaises(ProviderError) as caught:
                await client.complete([{'role': 'user', 'content': 'synthetic'}])
            self.assertEqual(caught.exception.kind, 'budget')
            self.assertEqual(len(server.requests), 2)
            self.assertIn(f'bots_spend_usd_micros_total{{priority="P0"}} {ledger.day_micros}', registry.render())
            self.assertGreater(ledger.day_micros, 0)
        finally:
            await server.stop()

    async def test_a_ledger_that_cannot_be_saved_sends_nothing(self):
        server = FakeKimi()
        await server.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                ledger = SpendLedger(Path(temp) / 'budget.json')

                def broken():
                    raise OSError('read-only volume')
                ledger._persist = broken
                client = MoonshotClient(f'http://127.0.0.1:{server.port}/v1', 'only-synthetic',
                                        ledger=ledger)
                with self.assertRaises(ProviderError) as caught:
                    await client.complete([{'role': 'user', 'content': 'synthetic'}])
                self.assertEqual(caught.exception.kind, 'budget_state')
                self.assertEqual(server.requests, [])
        finally:
            await server.stop()

    async def test_local_budget_refusals_are_not_counted_as_provider_errors(self):
        server = FakeKimi()
        await server.start()
        try:
            registry = Registry()
            from bots.metrics import BotMetrics
            client = MoonshotClient(f'http://127.0.0.1:{server.port}/v1', 'only-synthetic',
                                    metrics=BotMetrics(registry),
                                    ledger=SpendLedger(None, per_minute=1))
            await client.complete([{'role': 'user', 'content': 'uno'}])
            with self.assertRaises(ProviderError):
                await client.complete([{'role': 'user', 'content': 'dos'}])
            text = registry.render()
            self.assertIn('bots_spend_denied_total{reason="rate"} 1', text)
            self.assertNotIn('bots_provider_errors_total{kind="budget"}', text)
            self.assertNotIn('result="error"', text)
        finally:
            await server.stop()


if __name__ == '__main__':
    unittest.main()

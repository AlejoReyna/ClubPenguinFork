"""Provider quota persists across restarts, and retries count as real attempts."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from bots.budget import RequestBudget
from bots.config import build_config
from bots.manager import BotManager
from bots.metrics import Registry
from bots.provider import ProviderError
from support import config_document_world
from test_provider import FakeKimi


class BudgetTests(unittest.TestCase):
    def test_minute_day_and_restart_limits(self):
        now = [1800000000.0]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'budget.json'
            budget = RequestBudget(2, 3, path, clock=lambda: now[0])
            self.assertTrue(budget.reserve())
            self.assertTrue(budget.reserve())
            self.assertFalse(budget.reserve())
            now[0] += 61
            self.assertTrue(budget.reserve())
            now[0] += 61
            self.assertFalse(budget.reserve())
            restarted = RequestBudget(2, 3, path, clock=lambda: now[0])
            self.assertFalse(restarted.reserve())
            self.assertEqual(set(json.loads(path.read_text())), {'day', 'used', 'attempts'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            now[0] += 86400
            self.assertTrue(restarted.reserve())

    def test_insecure_ledger_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'budget.json'
            RequestBudget(path=path).reserve()
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                RequestBudget(path=path)


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
            from bots.provider import MoonshotClient
            client = MoonshotClient(f'http://127.0.0.1:{server.port}/v1', 'only-synthetic',
                                    retries=3, retry_wait=0.01, budget=RequestBudget(2, 3))
            with self.assertRaises(ProviderError) as caught:
                await client.complete([{'role': 'user', 'content': 'synthetic'}])
            self.assertEqual(caught.exception.kind, 'budget')
            self.assertEqual(len(server.requests), 2)
        finally:
            await server.stop()

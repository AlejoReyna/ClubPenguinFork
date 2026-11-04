import asyncio
import sys
import unittest

from experiments.puffle_sim.chatgpt_process import ProcessChatGPTProvider


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_worker_answers_without_credentials_in_simulated_mode(self):
        provider = ProcessChatGPTProvider(command=[sys.executable, '-m', 'production.puffle.device_worker', '--simulated'])
        answer = await asyncio.wait_for(provider.evaluate([('player', '¿Jugamos?')]), 5)
        self.assertIs(answer[0], True)
        self.assertTrue(answer[1])
        self.assertLessEqual(len(answer[1]), 48)

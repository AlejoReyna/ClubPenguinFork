import asyncio
import unittest

from experiments.puffle_sim.engine import Binding, Engine, QuotaExhausted, safe_text, select_puffle
from experiments.puffle_sim.provider import SimulatedProcess


class Capture:
    def __init__(self, answer=(True, '¡Hola!'), gate=None, error=None):
        self.answer, self.gate, self.error = answer, gate, error
        self.calls = []
        self.entered = asyncio.Event()

    async def evaluate(self, context):
        self.calls.append(context)
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return self.answer


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.live = set()
        self.deliveries = []
        self.provider = Capture()
        self.engine = Engine(self.provider, lambda b: b in self.live, self.deliver,
                             batch_seconds=0.01, min_interval=0, deadline=0.2)
        self.binding = Binding(1, 101, 10)
        self.live.add(self.binding)
        self.assertTrue(self.engine.start(self.binding))

    async def asyncTearDown(self):
        for s in list(self.engine.sessions.values()):
            await self.engine.stop(s.binding)

    async def deliver(self, binding, text, request_id):
        self.deliveries.append((binding, text, request_id))

    async def settle(self):
        await asyncio.sleep(0.06)

    async def test_batch_and_silence(self):
        self.provider.answer = (False, 'ignored')
        self.assertTrue(self.engine.observe(self.binding, 'hola'))
        self.assertTrue(self.engine.observe(self.binding, '¿jugamos?'))
        await self.settle()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.calls[0], (('player', 'hola'), ('player', '¿jugamos?')))
        self.assertEqual(self.deliveries, [])

    async def test_separate_contexts_and_session_limit(self):
        other = Binding(2, 202, 20)
        self.live.add(other)
        self.assertTrue(self.engine.start(other))
        self.engine.observe(self.binding, 'uno')
        self.engine.observe(other, 'dos')
        await self.settle()
        self.assertCountEqual(self.provider.calls, [(('player', 'uno'),), (('player', 'dos'),)])
        self.assertEqual({x[0] for x in self.deliveries}, {self.binding, other})

    async def test_stop_cancels_and_clears_and_new_session_is_isolated(self):
        self.provider.gate = asyncio.Event()
        self.engine.observe(self.binding, 'privado')
        await self.provider.entered.wait()
        old = self.engine.sessions[self.binding.player]
        await self.engine.stop(self.binding)
        self.assertFalse(old.history)
        self.assertFalse(self.deliveries)
        new = Binding(1, 101, 11)
        self.live.add(new)
        self.assertTrue(self.engine.start(new))
        self.assertFalse(self.engine.observe(self.binding, 'tarde'))
        self.assertTrue(self.engine.observe(new, 'nuevo'))
        self.provider.gate.set()
        await self.settle()
        self.assertEqual(self.provider.calls[-1], (('player', 'nuevo'),))
        self.assertEqual(self.deliveries[0][0], new)

    async def test_authority_revalidated_after_inference(self):
        gate = asyncio.Event()
        self.provider.gate = gate
        self.engine.observe(self.binding, 'hola')
        await self.provider.entered.wait()
        self.live.remove(self.binding)
        gate.set()
        await self.settle()
        self.assertFalse(self.deliveries)

    async def test_timeout_includes_wait_for_global_slot(self):
        self.engine.deadline = 0.05
        self.engine.slots = asyncio.Semaphore(0)
        self.engine.observe(self.binding, 'hola')
        await asyncio.sleep(0.08)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.deliveries)

    async def test_slow_inference_keeps_loop_responsive_and_orders_batches(self):
        gate = asyncio.Event()
        self.provider.gate = gate
        self.engine.observe(self.binding, 'primero')
        await self.provider.entered.wait()
        for n in range(12):
            self.assertTrue(self.engine.observe(self.binding, f'pendiente {n}'))
        ticks = []
        for _ in range(3):
            await asyncio.sleep(0.005)
            ticks.append(True)
        self.assertEqual(len(ticks), 3)
        gate.set()
        await self.settle()
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.provider.calls[0], (('player', 'primero'),))
        self.assertTrue(all(('player', f'pendiente {n}') in self.provider.calls[1]
                            for n in range(12)))
        self.assertEqual(len(self.deliveries), 2)
        self.assertNotEqual(self.deliveries[0][2], self.deliveries[1][2])

    async def test_quota_pauses_until_new_session(self):
        self.provider.error = QuotaExhausted()
        self.engine.observe(self.binding, 'hola')
        await self.settle()
        self.assertEqual(self.deliveries[0][1], 'Necesito descansar un rato.')
        self.assertFalse(self.engine.observe(self.binding, 'otra'))
        self.assertEqual(len(self.provider.calls), 1)

    async def test_exit_before_batch_sends_nothing(self):
        self.engine.batch_seconds = 0.1
        self.engine.observe(self.binding, 'se cancela')
        await self.engine.stop(self.binding)
        await asyncio.sleep(0.12)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.deliveries, [])

    async def test_worker_error_is_contained_and_later_work_can_continue(self):
        self.provider.error = RuntimeError('private exception detail')
        self.engine.observe(self.binding, 'primer intento')
        await self.settle()
        self.assertEqual(self.deliveries, [])
        self.provider.error = None
        self.assertTrue(self.engine.observe(self.binding, 'segundo intento'))
        await self.settle()
        self.assertEqual(len(self.deliveries), 1)

    async def test_output_is_text_with_bounded_safe_bytes(self):
        self.provider.answer = (True, '%xt%sm%|\x00<script>\n' + 'ñ' * 100)
        self.engine.observe(self.binding, 'hola')
        await self.settle()
        out = self.deliveries[0][1]
        self.assertLessEqual(len(out.encode()), 120)
        self.assertLessEqual(len(out), 48)
        self.assertFalse(any(c in out for c in '%|\x00<>\n'))

    async def test_bounded_pending_and_unaccepted_binding(self):
        self.engine.max_pending = 1
        self.assertTrue(self.engine.observe(self.binding, 'uno'))
        first_pending_at = self.engine.sessions[self.binding.player].first_pending_at
        self.assertTrue(self.engine.observe(self.binding, 'dos'))
        self.assertEqual(self.engine.sessions[self.binding.player].pending, 1)
        self.assertEqual(self.engine.sessions[self.binding.player].first_pending_at, first_pending_at)
        self.assertEqual(self.engine.sessions[self.binding.player].history[-1], ('player', 'dos'))
        self.assertFalse(self.engine.observe(Binding(9, 101, 10), 'visitante'))

    async def test_real_separate_simulator(self):
        worker = SimulatedProcess()
        self.assertEqual(await worker.evaluate((('player', 'hola'),)), (False, ''))
        self.assertEqual(await worker.evaluate((('player', '¿jugamos?'),)), (True, '¡Hola! ¿Jugamos?'))


class TextTests(unittest.TestCase):
    def test_selection_and_sanitization(self):
        self.assertEqual(select_puffle([12, 3, 8]), 3)
        self.assertEqual(select_puffle([12, 3, 8], 8), 8)
        self.assertEqual(select_puffle([12, 3, 8], 999), 3)
        self.assertIsNone(select_puffle([]))
        self.assertEqual(safe_text('hola%|\x00<b> & \u202e mundo', 100), 'hola b mundo')


if __name__ == '__main__':
    unittest.main()

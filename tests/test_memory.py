"""Per-speaker memory: isolation, bounds, expiry."""
import unittest

from bots.memory import ConversationMemory


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class MemoryTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.memory = ConversationMemory(max_turns=3, ttl=120.0, clock=self.clock)

    def test_conversation_is_per_speaker(self):
        self.memory.add(7, 'hola', 'saludos')
        self.memory.add(9, 'otro tema', 'respondiendo al otro')
        context = {turn['content'] for turn in self.memory.context(7)}
        self.assertEqual(context, {'hola', 'saludos'})
        self.assertNotIn('otro tema', ' '.join(t['content'] for t in self.memory.context(7)))

    def test_context_role_alternation(self):
        self.memory.add(3, 'pregunta', 'respuesta')
        self.memory.add(3, 'pregunta 2', 'respuesta 2')
        roles = [turn['role'] for turn in self.memory.context(3)]
        self.assertEqual(roles, ['user', 'assistant', 'user', 'assistant'])

    def test_buffer_stays_bounded_by_turns(self):
        for index in range(12):
            self.memory.add(1, f'pregunta {index}', f'respuesta {index}')
        history = self.memory.context(1)
        self.assertEqual(len(history), 6)  # max_turns * 2 messages
        self.assertEqual(history[0]['content'], 'pregunta 9')

    def test_context_returns_a_copy(self):
        self.memory.add(2, 'uno', 'dos')
        context = self.memory.context(2)
        context.clear()
        self.assertEqual(len(self.memory.context(2)), 2)

    def test_forget_drops_only_that_speaker(self):
        self.memory.add(1, 'a', 'b')
        self.memory.add(2, 'c', 'd')
        self.memory.forget(1)
        self.assertEqual(len(self.memory), 1)
        self.assertNotIn(1, self.memory)
        self.assertIn(2, self.memory)

    def test_prune_drops_only_expired_conversations(self):
        self.memory.add(1, 'antiguo', 'si')
        self.clock.now += 60.0
        self.memory.add(2, 'reciente', 'tambien')
        self.memory.add(2, 'otra', 'mas')
        # Only speaker 1 refreshed its timestamp; speaker 2 did not.
        self.assertTrue(all(t['_ts'] == 1000.0 for t in self.memory._turns[1]))
        self.clock.now = 1000.0 + 120.0 + 1.0
        self.assertEqual(self.memory.prune(), 1)
        self.assertNotIn(1, self.memory)
        self.assertIn(2, self.memory)

    def test_invalid_construction(self):
        with self.assertRaises(ValueError):
            ConversationMemory(max_turns=0)
        with self.assertRaises(ValueError):
            ConversationMemory(ttl=0)

    def test_expired_context_is_never_reused_before_periodic_prune(self):
        self.memory.add(1, 'old', 'old reply')
        self.clock.now += 121
        self.assertEqual(self.memory.context(1), [])

    def test_speaker_count_is_bounded(self):
        memory = ConversationMemory(max_speakers=2, clock=self.clock)
        for player in range(3):
            memory.add(player, 'synthetic', 'reply')
            self.clock.now += 1
        self.assertEqual(len(memory), 2)
        self.assertNotIn(0, memory)

    def test_add_accepts_every_speaker_shape(self):
        self.memory.add('5', 'hola')
        self.assertIn(5, self.memory)
        self.memory.add(5.0, 'de nuevo', 'bien')
        self.assertEqual(len(self.memory.context('5')), 3)


if __name__ == '__main__':
    unittest.main()

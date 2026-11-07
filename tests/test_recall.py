"""Phase 4 relationship notes: TTL, upsert bounds and forget requests."""
import tempfile
import unittest

from bots.recall import RecallStore, wants_forget


class RecallTests(unittest.TestCase):
    def test_wants_forget_matches_spanish_phrases(self):
        self.assertTrue(wants_forget('por favor borra mi memoria'))
        self.assertTrue(wants_forget('olvida el recuerdo'))
        self.assertFalse(wants_forget('hola como estas'))

    def test_note_expires_after_ttl(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as tmp:
            path = f'{tmp}/recall.sqlite'
            store = RecallStore(path, ttl_seconds=60, clock=lambda: now[0])
            self.assertTrue(store.upsert(0, 42, 'le gusta el baile'))
            self.assertEqual(store.get(0, 42), 'le gusta el baile')
            now[0] += 61
            self.assertIsNone(store.get(0, 42))

    def test_delete_removes_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f'{tmp}/recall.sqlite'
            store = RecallStore(path, ttl_seconds=3600)
            store.upsert(1, 7, 'hablamos de nieve')
            self.assertTrue(store.delete(1, 7))
            self.assertIsNone(store.get(1, 7))

    def test_note_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f'{tmp}/recall.sqlite'
            store = RecallStore(path, ttl_seconds=86400)
            store.upsert(2, 99, 'volvio al dia siguiente')
            del store
            again = RecallStore(path, ttl_seconds=86400)
            self.assertEqual(again.get(2, 99), 'volvio al dia siguiente')


if __name__ == '__main__':
    unittest.main()

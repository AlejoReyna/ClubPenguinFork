import tempfile
import unittest
from pathlib import Path

from production.puffle.broker import Broker, Rejected

SECRET = 's' * 48


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.b = Broker(SECRET, clock=lambda: self.now)

    def tearDown(self):
        self.b.db.close()

    def device(self, player=1):
        pending = self.b.dispatch('/puffle/api/pair', '', {})
        self.b.dispatch('/internal/pair', SECRET, {'player': player, 'code': pending['code'], 'nickname': 'Dueño'})
        self.b.dispatch('/puffle/api/confirm', pending['token'], {'player': player})
        self.b.dispatch('/puffle/api/poll', pending['token'], {})
        return pending['token']

    def submit(self, player=1):
        return self.b.dispatch('/internal/submit', SECRET,
                               {'player': player, 'context': [['player', '¿Hola?']]})['job']

    def test_pairing_requires_game_and_device_confirmation(self):
        pending = self.b.dispatch('/puffle/api/pair', '', {})
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/confirm', pending['token'], {'player': 1})
        self.b.dispatch('/internal/pair', SECRET, {'player': 1, 'code': pending['code']})
        with self.assertRaises(Rejected):
            self.b.dispatch('/internal/pair', SECRET, {'player': 2, 'code': pending['code']})
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/confirm', pending['token'], {'player': 2})
        self.b.dispatch('/puffle/api/confirm', pending['token'], {'player': 1})
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/confirm', pending['token'], {'player': 1})

    def test_untrusted_device_cannot_call_game_routes(self):
        token = self.device()
        with self.assertRaises(Rejected):
            self.b.dispatch('/internal/submit', token, {'player': 1, 'context': []})

    def test_two_devices_cannot_read_or_complete_each_others_jobs(self):
        a, b = self.device(1), self.device(2)
        key = self.submit(1)
        self.assertIsNone(self.b.dispatch('/puffle/api/poll', b, {})['job'])
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/result', b, {'job': key, 'reply': True, 'text': 'oops'})
        self.assertEqual(self.b.dispatch('/puffle/api/poll', a, {})['job'], key)
        self.b.dispatch('/puffle/api/result', a, {'job': key, 'reply': True, 'text': 'hola'})
        result = self.b.dispatch('/internal/result', SECRET, {'player': 1, 'job': key})
        self.assertEqual(result['result']['text'], 'hola')
        self.assertFalse(self.b.jobs)

    def test_cancellation_erases_context_and_refuses_late_results(self):
        token = self.device()
        key = self.submit()
        self.b.dispatch('/puffle/api/poll', token, {})
        reference = self.b.jobs[key]
        self.b.dispatch('/internal/cancel', SECRET, {'player': 1, 'job': key})
        self.assertEqual(reference['context'], [])
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/result', token, {'job': key, 'reply': True, 'text': 'late'})

    def test_revoke_invalidates_device_and_work(self):
        token = self.device()
        self.submit()
        self.b.dispatch('/internal/revoke', SECRET, {'player': 1})
        self.assertFalse(self.b.jobs)
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/poll', token, {})

    def test_device_loss_cancels_work(self):
        self.device()
        self.submit()
        self.now += 5
        self.b.prune()
        self.assertFalse(self.b.jobs)

    def test_limits_and_expiry(self):
        token = self.device()
        self.submit()
        with self.assertRaises(Rejected):
            self.submit()
        self.now += 26
        self.b.dispatch('/puffle/api/poll', token, {})
        self.assertFalse(self.b.jobs)

    def test_pairing_expiry_and_disabled_service(self):
        pending = self.b.dispatch('/puffle/api/pair', '', {})
        self.now += 301
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/pair-status', pending['token'], {})
        self.b.enabled = False
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/pair', '', {})
        self.assertTrue(self.b.dispatch('/healthz', '', {})['ok'])

    def test_duplicate_result_is_not_accepted(self):
        token = self.device()
        key = self.submit()
        self.b.dispatch('/puffle/api/poll', token, {})
        answer = {'job': key, 'reply': True, 'text': 'hola'}
        self.b.dispatch('/puffle/api/result', token, answer)
        with self.assertRaises(Rejected):
            self.b.dispatch('/puffle/api/result', token, answer)

    def test_malformed_job_identifiers_are_rejected_without_internal_errors(self):
        token = self.device()
        for invalid in ([], {}, None, 1):
            for path, auth in (('/puffle/api/result', token), ('/internal/result', SECRET)):
                with self.assertRaises(Rejected):
                    self.b.dispatch(path, auth, {'player': 1, 'job': invalid})

    def test_persistence_contains_hashes_only_and_no_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'devices.sqlite')
            other = Broker(SECRET, path, clock=lambda: self.now)
            self.b.db.close()
            self.b = other
            token = self.device()
            self.submit()
            self.b.db.close()
            self.b = Broker(SECRET, path, clock=lambda: self.now)
            self.assertEqual(self.b.device(token), 1)
            self.assertFalse(self.b.jobs)
            raw = Path(path).read_bytes()
            self.assertNotIn(token.encode(), raw)
            self.assertNotIn('¿Hola?'.encode(), raw)

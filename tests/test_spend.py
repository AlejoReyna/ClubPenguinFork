"""The USD spend ledger: reserve/settle, caps, priority classes, fail-closed, legacy file."""
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bots import spend
from bots.spend import P0, P1, P2, Prices, SpendDenied, SpendLedger

# 2027-01-15 08:00:00 UTC. Far from any day/month edge.
T0 = 1800000000.0
DAY = 86400.0


def make(path=None, clock=None, **overrides):
    settings = dict(daily_usd=1.0, monthly_usd=30.0, per_minute=1000, max_requests_per_day=0)
    settings.update(overrides)
    return SpendLedger(path, clock=clock or (lambda: T0), **settings)


class PriceTests(unittest.TestCase):

    def test_design_figures_per_call(self):
        prices = Prices()  # 0.95 / 0.16 / 4.00 USD per million tokens
        # Typical call (docs/bots.md, Spend control): ~1000 input and ~40 output tokens.
        self.assertEqual(prices.cost_micros(1000, 0, 40), 1110)       # ~ $0.0011
        self.assertEqual(prices.cost_micros(1000, 600, 40), 636)      # ~ $0.0006 with 60 % cached

    def test_rounds_up_and_clamps(self):
        prices = Prices()
        self.assertEqual(prices.cost_micros(1, 0, 0), 1)              # 0.95 -> 1
        self.assertEqual(prices.cost_micros(0, 0, 0), 0)
        # cached cannot exceed the input; negative counts count as zero
        self.assertEqual(prices.cost_micros(100, 500, 0), prices.cost_micros(100, 100, 0))
        self.assertEqual(prices.cost_micros(-5, 0, -5), 0)

    def test_prices_are_configuration(self):
        prices = Prices(input_per_m=2, cached_input_per_m=1, output_per_m=10)
        self.assertEqual(prices.cost_micros(1000, 0, 100), 3000)
        self.assertEqual(prices.as_dict()['output_per_m'], 10.0)
        for bad in (-1, 'x', float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Prices(input_per_m=bad)

    def test_usage_shapes(self):
        self.assertEqual(spend.usage_tokens({'prompt_tokens': 100, 'completion_tokens': 5}), (100, 0, 5))
        self.assertEqual(spend.usage_tokens(
            {'prompt_tokens': 100, 'completion_tokens': 5, 'cached_tokens': 60}), (100, 60, 5))
        self.assertEqual(spend.usage_tokens(
            {'prompt_tokens': 100, 'completion_tokens': 5,
             'prompt_tokens_details': {'cached_tokens': 40}}), (100, 40, 5))
        # cached cannot exceed the prompt
        self.assertEqual(spend.usage_tokens(
            {'prompt_tokens': 10, 'completion_tokens': 1, 'cached_tokens': 99}), (10, 10, 1))
        for bad in (None, [], {}, {'prompt_tokens': 'x', 'completion_tokens': 1},
                    {'prompt_tokens': -1, 'completion_tokens': 1}, {'prompt_tokens': 5}):
            self.assertIsNone(spend.usage_tokens(bad))


class ReserveSettleTests(unittest.TestCase):

    def test_reserve_then_reconcile_with_usage(self):
        ledger = make()
        reservation = ledger.reserve(P0, 5000)
        self.assertEqual(ledger.day_micros, 5000)
        charged = ledger.settle(reservation, {'prompt_tokens': 1000, 'completion_tokens': 40})
        self.assertEqual(charged, 1110)
        self.assertEqual((ledger.day_micros, ledger.month_micros), (1110, 1110))
        self.assertEqual(ledger.by_priority[P0], 1110)
        self.assertEqual(ledger.requests_day, 1)

    def test_missing_usage_keeps_the_estimate(self):
        ledger = make()
        reservation = ledger.reserve(P1, 700)
        self.assertEqual(ledger.settle(reservation, None), 700)
        self.assertEqual(ledger.day_micros, 700)

    def test_settle_is_idempotent(self):
        ledger = make()
        reservation = ledger.reserve(P0, 900)
        ledger.settle(reservation, {'prompt_tokens': 100, 'completion_tokens': 1})
        total = ledger.day_micros
        ledger.settle(reservation, {'prompt_tokens': 99999, 'completion_tokens': 99999})
        self.assertEqual(ledger.day_micros, total)

    def test_every_attempt_counts_and_failures_keep_their_estimate(self):
        ledger = make()
        for _attempt in range(3):          # a request plus two retries, none settled
            ledger.reserve(P0, 1000)
        self.assertEqual(ledger.day_micros, 3000)
        self.assertEqual(ledger.requests_day, 3)

    def test_estimate_grows_with_the_prompt(self):
        ledger = make()
        small = ledger.estimate_micros([{'role': 'user', 'content': 'hola'}], 120)
        large = ledger.estimate_micros([{'role': 'user', 'content': 'hola ' * 400}], 120)
        self.assertGreater(large, small)
        self.assertGreaterEqual(small, Prices().cost_micros(0, 0, 120))   # output at the cap

    def test_unknown_priority_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            make().reserve('P9', 1)
        with self.assertRaises(ValueError):
            make().allows('P9')


class CapTests(unittest.TestCase):

    def test_daily_cap_is_never_exceeded_by_reservations(self):
        ledger = make(daily_usd=0.01)       # 10_000 micros
        ledger.reserve(P0, 6000)
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 6000)
        self.assertEqual(caught.exception.reason, 'daily')
        self.assertEqual(ledger.day_micros, 6000)       # the denied request held nothing
        ledger.reserve(P0, 4000)                         # exactly the cap is allowed

    def test_monthly_cap(self):
        ledger = make(daily_usd=10.0, monthly_usd=0.02)  # 20_000 micros
        ledger.reserve(P0, 12000)
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 9000)
        self.assertEqual(caught.exception.reason, 'monthly')

    def test_weekly_cap_and_tightest_ratio_for_priority(self):
        ledger = make(daily_usd=10.0, weekly_usd=0.01)  # week is the tight cap
        ledger.reserve(P2, 8000)
        self.assertFalse(ledger.allows(P2))  # 80 % of the weekly cap
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 3000)
        self.assertEqual(caught.exception.reason, 'weekly')

    def test_per_minute_window_slides(self):
        now = [T0]
        ledger = make(clock=lambda: now[0], per_minute=2)
        ledger.reserve(P0, 1)
        ledger.reserve(P0, 1)
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 1)
        self.assertEqual(caught.exception.reason, 'rate')
        now[0] += 61
        ledger.reserve(P0, 1)

    def test_legacy_requests_per_day_cap_stays_available(self):
        ledger = make(max_requests_per_day=2)
        ledger.reserve(P0, 1)
        ledger.reserve(P0, 1)
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 1)
        self.assertEqual(caught.exception.reason, 'requests')

    def test_day_and_month_roll_forward_only(self):
        now = [T0]
        ledger = make(clock=lambda: now[0], max_requests_per_day=1)
        ledger.reserve(P0, 4000)
        now[0] += DAY
        ledger.reserve(P0, 100)             # new day: counters reset
        self.assertEqual(ledger.day_micros, 100)
        self.assertEqual(ledger.month_micros, 4100)
        now[0] -= 2 * DAY                   # clock glitch backwards: nothing resets
        with self.assertRaises(SpendDenied):
            ledger.reserve(P0, 1)
        self.assertEqual(ledger.day_micros, 100)
        now[0] += 31 * DAY                  # a later month
        ledger.reserve(P0, 5)
        self.assertEqual(ledger.month_micros, 5)


class PriorityTests(unittest.TestCase):

    def fill(self, ledger, fraction):
        ledger.day_micros = int(ledger.daily_cap * fraction)

    def test_p2_off_at_80_percent_p1_at_95_everything_at_100(self):
        ledger = make(daily_usd=1.0)
        self.fill(ledger, 0.79)
        self.assertTrue(ledger.allows(P2) and ledger.allows(P1) and ledger.allows(P0))
        ledger.reserve(P2, 1)
        self.fill(ledger, 0.80)
        self.assertFalse(ledger.allows(P2))
        self.assertTrue(ledger.allows(P1) and ledger.allows(P0))
        with self.assertRaises(SpendDenied) as p2:
            ledger.reserve(P2, 1)
        self.assertEqual(p2.exception.reason, 'priority')
        ledger.reserve(P1, 1)
        self.fill(ledger, 0.95)
        self.assertFalse(ledger.allows(P1))
        self.assertTrue(ledger.allows(P0))
        with self.assertRaises(SpendDenied) as p1:
            ledger.reserve(P1, 1)
        self.assertEqual(p1.exception.reason, 'priority')
        ledger.reserve(P0, 1)
        self.fill(ledger, 1.0)
        self.assertFalse(ledger.allows(P0))
        with self.assertRaises(SpendDenied) as p0:
            ledger.reserve(P0, 1)
        self.assertEqual(p0.exception.reason, 'daily')

    def test_ratio_and_snapshot(self):
        ledger = make(daily_usd=1.0)
        reservation = ledger.reserve(P0, 250000)
        ledger.settle(reservation, {'prompt_tokens': 1000, 'completion_tokens': 40})
        snapshot = ledger.snapshot()
        self.assertAlmostEqual(snapshot['ratio'], 0.00111, places=6)
        self.assertEqual(snapshot['daily_cap_micros'], 1_000_000)
        self.assertEqual(snapshot['monthly_cap_micros'], 30_000_000)
        self.assertEqual(snapshot['by_priority'][P0], 1110)

    def test_monthly_cap_also_closes_the_gate(self):
        ledger = make(monthly_usd=0.001)       # 1_000 micros
        self.assertTrue(ledger.allows(P0))
        ledger.reserve(P0, 1000)               # reaches the monthly cap through the API
        self.assertFalse(ledger.allows(P0))


class PersistenceTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'budget.json'

    def test_state_survives_a_restart_and_is_private(self):
        ledger = make(self.path)
        ledger.settle(ledger.reserve(P0, 5000), {'prompt_tokens': 1000, 'completion_tokens': 40})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        restarted = make(self.path)
        self.assertEqual(restarted.day_micros, 1110)
        self.assertEqual(restarted.month_micros, 1110)
        self.assertEqual(restarted.requests_day, 1)
        self.assertEqual(restarted.by_priority[P0], 1110)

    def test_file_is_a_superset_of_the_legacy_format(self):
        ledger = make(self.path)
        ledger.reserve(P0, 10)
        document = json.loads(self.path.read_text())
        self.assertEqual(document['version'], 2)
        # Exactly what the previous image validates on load.
        self.assertIsInstance(document['day'], str)
        self.assertIsInstance(document['used'], int)
        self.assertIsInstance(document['attempts'], list)

    def test_old_format_loads_without_crashing_and_is_never_deleted(self):
        legacy = {'day': '2027-01-15', 'used': 7, 'attempts': [T0 - 10, T0 - 400]}
        self.path.write_text(json.dumps(legacy))
        self.path.chmod(0o600)
        ledger = make(self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(ledger.requests_day, 7)
        self.assertGreater(ledger.day_micros, 0)          # today's requests carried over
        self.assertEqual(ledger.snapshot()['requests_today'], 7)
        ledger.reserve(P0, 10)                            # first write converts in place
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text())['used'], 8)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_old_format_from_a_previous_day_starts_a_clean_day(self):
        self.path.write_text(json.dumps({'day': '2027-01-14', 'used': 99, 'attempts': []}))
        self.path.chmod(0o600)
        ledger = make(self.path, max_requests_per_day=100)
        ledger.reserve(P0, 1)
        self.assertEqual(ledger.requests_day, 1)
        self.assertEqual(ledger.day, '2027-01-15')

    def test_old_format_request_cap_still_applies_to_todays_count(self):
        self.path.write_text(json.dumps({'day': '2027-01-15', 'used': 100, 'attempts': []}))
        self.path.chmod(0o600)
        ledger = make(self.path, max_requests_per_day=100)
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 1)
        self.assertEqual(caught.exception.reason, 'requests')

    def test_insecure_or_symlinked_files_are_rejected(self):
        make(self.path).reserve(P0, 1)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            make(self.path)
        self.path.chmod(0o600)
        link = Path(self.temp.name) / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            make(link)

    def test_invalid_content_is_rejected_and_left_untouched(self):
        for index, body in enumerate(('not json', '[]', '{"day": 5}',
                                      '{"day":"2027-01-15","used":-1,"attempts":[]}',
                                      '{"day":"x","used":true,"attempts":[]}',
                                      '{"version":2,"day":"x"}')):
            path = Path(self.temp.name) / f'bad{index}.json'
            path.write_text(body)
            path.chmod(0o600)
            with self.assertRaises(ValueError, msg=body):
                make(path)
            self.assertEqual(path.read_text(), body)

    def test_fails_closed_when_the_ledger_cannot_be_saved(self):
        ledger = make(self.path)
        ledger.reserve(P0, 100)
        with patch.object(SpendLedger, '_persist', side_effect=OSError('disk full')):
            with self.assertRaises(SpendDenied) as caught:
                ledger.reserve(P0, 100)
        self.assertEqual(caught.exception.reason, 'state')
        self.assertFalse(ledger.state_ok)
        self.assertEqual((ledger.day_micros, ledger.requests_day), (100, 1))   # rolled back
        ledger.reserve(P0, 100)                                                # recovers
        self.assertTrue(ledger.state_ok)

    def test_symlinked_temp_file_fails_closed(self):
        ledger = make(self.path)
        ledger.reserve(P0, 1)
        target = Path(self.temp.name) / 'elsewhere'
        target.write_text('x')
        self.path.with_suffix('.tmp').symlink_to(target)
        with self.assertRaises(SpendDenied) as caught:
            ledger.reserve(P0, 1)
        self.assertEqual(caught.exception.reason, 'state')
        self.assertEqual(target.read_text(), 'x')        # never written through the link

    def test_a_failed_reconcile_does_not_hide_the_spend(self):
        ledger = make(self.path)
        reservation = ledger.reserve(P0, 100)
        with patch.object(SpendLedger, '_persist', side_effect=OSError('disk full')):
            ledger.settle(reservation, {'prompt_tokens': 1000, 'completion_tokens': 40})
        self.assertEqual(ledger.day_micros, 1110)         # kept in memory
        self.assertFalse(ledger.state_ok)
        ledger.reserve(P0, 1)                             # next write saves everything
        self.assertEqual(make(self.path).day_micros, 1111)
        self.assertTrue(ledger.state_ok)

    def test_reconcile_without_price_delta_persists_priority(self):
        ledger = make(self.path)
        reservation = ledger.reserve(P2, 100)
        ledger.settle(reservation, None)
        self.assertEqual(make(self.path).by_priority[P2], 100)

    def test_private_file_checks_are_repeated_before_each_attempt(self):
        ledger = make(self.path)
        ledger.reserve(P0, 1)
        self.path.chmod(0o644)
        with self.assertRaises(SpendDenied):
            ledger.reserve(P0, 1)
        self.assertFalse(ledger.state_ok)

    def test_legacy_previous_day_still_counts_toward_the_month(self):
        self.path.write_text(json.dumps({'day': '2027-01-14', 'used': 5, 'attempts': []}))
        self.path.chmod(0o600)
        ledger = make(self.path)
        ledger.reserve(P0, 1)
        self.assertEqual(ledger.day_micros, 1)
        self.assertEqual(ledger.month_micros, 5 * ledger._legacy_request_micros() + 1)

    def test_without_a_path_the_ledger_is_in_memory(self):
        ledger = make()
        ledger.reserve(P0, 10)
        self.assertEqual(ledger.day_micros, 10)

    def test_player_slots_survive_restart_refund_once_and_reset_next_day(self):
        now = [T0]
        ledger = make(self.path, clock=lambda: now[0])
        token = ledger.reserve_player(999, 1)
        restarted = make(self.path, clock=lambda: now[0])
        self.assertIsNone(restarted.reserve_player(999, 1))
        ledger.release_player(token)
        ledger.release_player(token)
        self.assertEqual(make(self.path).players, {999: 0})
        ledger.reserve_player(999, 1)
        now[0] += DAY
        self.assertIsNotNone(ledger.reserve_player(999, 1))
        self.assertEqual(ledger.players, {999: 1})

    def test_player_slots_fail_closed_if_the_private_ledger_cannot_be_written(self):
        ledger = make(self.path)
        with patch.object(SpendLedger, '_persist', side_effect=OSError('disk full')):
            with self.assertRaises(SpendDenied):
                ledger.reserve_player(999, 1)
        self.assertEqual(ledger.players, {})
        self.assertFalse(ledger.state_ok)

    def test_invalid_persisted_player_slots_are_rejected_without_modification(self):
        ledger = make(self.path)
        ledger.reserve_player(999)
        document = json.loads(self.path.read_text())
        document['players'] = {'a human name': 40}
        self.path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            make(self.path)

    def test_invalid_limits(self):
        for bad in (dict(daily_usd=0), dict(monthly_usd=-1), dict(daily_usd='x'),
                    dict(per_minute=0), dict(max_requests_per_day=-1)):
            with self.assertRaises(ValueError, msg=str(bad)):
                make(**bad)


if __name__ == '__main__':
    unittest.main()

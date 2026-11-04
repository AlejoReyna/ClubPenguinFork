"""Consumption guards: sliding window, cooldowns and the reply gate order."""
import unittest
from unittest.mock import patch

from bots import policy


class FakePolicyClock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


class RateLimiterTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakePolicyClock()
        patcher = patch.object(policy, '_now', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.limiter = policy.RateLimiter(max_requests=2, period=60.0)

    def test_allows_requests_under_the_cap(self):
        self.assertTrue(self.limiter.try_acquire())
        self.assertTrue(self.limiter.try_acquire())
        self.assertFalse(self.limiter.try_acquire())

    def test_window_slides_forward(self):
        self.limiter.try_acquire()
        self.limiter.try_acquire()
        self.assertFalse(self.limiter.try_acquire())
        self.clock.now += 61.0
        self.assertTrue(self.limiter.try_acquire())

    def test_pristine_after_reset(self):
        self.limiter.try_acquire()
        self.limiter.try_acquire()
        self.limiter.reset()
        self.assertTrue(self.limiter.try_acquire())

    def test_construction_validation(self):
        with self.assertRaises(ValueError):
            policy.RateLimiter(max_requests=0)
        with self.assertRaises(ValueError):
            policy.RateLimiter(period=0)


class CooldownMapTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakePolicyClock()
        patcher = patch.object(policy, '_now', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_acquire_inside_the_cooldown_is_refused(self):
        cooldown = policy.CooldownMap(seconds=6.0)
        self.assertTrue(cooldown.try_acquire(14))
        self.assertFalse(cooldown.try_acquire(14))
        self.clock.now += 6.5
        self.assertTrue(cooldown.try_acquire(14))

    def test_keys_are_independent(self):
        cooldown = policy.CooldownMap(seconds=6.0)
        self.assertTrue(cooldown.try_acquire(1))
        self.assertTrue(cooldown.try_acquire(2))
        self.assertFalse(cooldown.try_acquire(1))

    def test_stays_bounded_by_pruning_expired_keys(self):
        cooldown = policy.CooldownMap(seconds=1.0)
        for key in range(5000):
            cooldown.try_acquire(key)
        # Nothing expired yet: the map simply holds every key.
        self.assertEqual(len(cooldown._next_ok), 5000)
        self.clock.now += 2.0
        for key in range(4097):
            cooldown.try_acquire(4097 + key)
        # Expired keys are dropped opportunistically on later acquisitions.
        self.assertEqual(len(cooldown._next_ok), 4097)

    def test_construction_validation(self):
        with self.assertRaises(ValueError):
            policy.CooldownMap(seconds=-1)


class ReplyGateTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakePolicyClock()
        patcher = patch.object(policy, '_now', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_order_bot_gap_before_rate_limit(self):
        gate = policy.ReplyGate(bot_gap=5.0, rate_limit=policy.RateLimiter(1),
                                player_cooldown=policy.CooldownMap(0.0))
        self.assertEqual(gate.try_reserve(0, 10), (True, None))
        blocked, reason = gate.try_reserve(0, 10)
        self.assertFalse(blocked)
        self.assertEqual(reason, 'bot_gap')
        # The rate limiter slot was never taken by the blocked reserve.
        self.assertEqual(len(gate.rate_limit._window), 1)

    def test_rate_limit_beats_player_cooldown(self):
        gate = policy.ReplyGate(bot_gap=0.0, rate_limit=policy.RateLimiter(1),
                                player_cooldown=policy.CooldownMap(6.0))
        self.assertEqual(gate.try_reserve(0, 10), (True, None))
        blocked, reason = gate.try_reserve(0, 10)
        self.assertFalse(blocked)
        self.assertEqual(reason, 'rate_limit')
        self.assertEqual(gate.player_cooldown._next_ok[10], 506.0)
        self.assertEqual(len(gate.rate_limit._window), 1)

    def test_player_cooldown_is_last(self):
        gate = policy.ReplyGate(bot_gap=0.0, rate_limit=policy.RateLimiter(10),
                                player_cooldown=policy.CooldownMap(6.0))
        self.assertEqual(gate.try_reserve(0, 10), (True, None))
        blocked, reason = gate.try_reserve(0, 10)
        self.assertFalse(blocked)
        self.assertEqual(reason, 'player_cooldown')
        self.assertEqual(len(gate.rate_limit._window), 1)


if __name__ == '__main__':
    unittest.main()

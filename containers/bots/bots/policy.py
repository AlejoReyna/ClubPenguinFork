"""Consumption guards: rate limits and per-speaker cooldowns.

Everything here is cooperative and single-loop: the bot behavior calls these
helpers from the reply queue only, never from the packet reader. If a guard
refuses, the bot simply stays silent and a metric records the reason.
"""
import time


def _now():
    return time.monotonic()


class RateLimiter:
    """Sliding-window cap of provider requests per period."""

    def __init__(self, max_requests=6, period=60.0):
        if max_requests < 1:
            raise ValueError('max_requests must be at least 1')
        if period <= 0:
            raise ValueError('period must be positive')
        self.max_requests = max_requests
        self.period = period
        self._window = []
        self._now = _now

    def try_acquire(self):
        """Return True and record the request while under the cap."""
        if not self.available():
            return False
        self._window.append(self._now())
        return True

    def available(self):
        now = self._now()
        cutoff = now - self.period
        while self._window and self._window[0] <= cutoff:
            self._window.pop(0)
        return len(self._window) < self.max_requests

    def reset(self):
        self._window.clear()


class CooldownMap:
    """Minimum seconds between actions, tracked per key (player or bot)."""

    def __init__(self, seconds=6.0):
        if seconds < 0:
            raise ValueError('cooldown seconds cannot be negative')
        self.seconds = seconds
        self._next_ok = {}

    def try_acquire(self, key):
        """True when the key may act now; refreshes the cooldown on success."""
        now = _now()
        ready = self._next_ok.get(key, -1.0)
        if now < ready:
            return False
        self._next_ok[key] = now + self.seconds
        # Opportunistic cleanup; the map stays over a bounded room roster.
        if len(self._next_ok) > 4096:
            expired = [k for k, v in self._next_ok.items() if v <= now]
            for k in expired:
                del self._next_ok[k]
        return True

    def available(self, key):
        return _now() >= self._next_ok.get(key, -1.0)


class ReplyGate:
    """The per-bot gate a message must clear before consuming provider quota.

    Order of evaluation: per-burst bot gap first, global rate limit next and
    per-player cooldown last. Each rejection is classified for metrics.
    """

    def __init__(self, bot_gap, rate_limit, player_cooldown):
        self.bot_gap = CooldownMap(bot_gap)
        self.rate_limit = rate_limit
        self.player_cooldown = player_cooldown

    def try_reserve(self, bot_key, player_id):
        if not self.bot_gap.available(bot_key):
            return False, 'bot_gap'
        if not self.rate_limit.available():
            return False, 'rate_limit'
        if not self.player_cooldown.available(player_id):
            return False, 'player_cooldown'
        self.bot_gap.try_acquire(bot_key)
        self.rate_limit.try_acquire()
        self.player_cooldown.try_acquire(player_id)
        return True, None

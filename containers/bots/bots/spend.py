"""USD spend ledger for the paid provider: reserve before sending, reconcile after.

Replaces the old request counter (``budget.py``). Every attempt, retries
included, first reserves an estimated cost; the reply's ``usage`` then
replaces the estimate. A request that fails keeps its estimate: the provider
may have processed it, so the ledger errs on the side of counting.

Rules (docs/bots.md, Spend control):
- daily and monthly caps in USD, plus a per-minute attempt window and an
  optional legacy requests-per-day cap (kept so an existing deployment keeps
  its old limits until its operator opts in to the USD ones);
- priority classes: P2 (bot-to-bot chatter) switches off at 80 % of the daily
  cap, P1 (model greetings) at 95 %, P0 (a human addressed the bot) only when
  the cap would be exceeded. Denied callers fall back to local lines;
- fail closed: if the ledger cannot be written, nothing is sent;
- the file must be private (no group/other bits) and not a symlink.

The file keeps the legacy keys ``day``, ``used`` and ``attempts`` next to the
new ones, so a rollback to an image that still has the request counter reads
it unchanged. A file in the old format loads cleanly and is converted in
memory; it is only ever replaced by a superset, never deleted.

Money is held as integer micro-USD (1e-6 USD). Prices are per million tokens,
which makes one USD-per-million price equal to micro-USD per token.
"""
import datetime
import json
import logging
import math
import os
import stat
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import time

logger = logging.getLogger('bots.spend')

P0, P1, P2 = 'P0', 'P1', 'P2'
PRIORITIES = (P0, P1, P2)
# Share of the daily cap at which each class stops being served.
CUTOFF_RATIO = {P2: 0.80, P1: 0.95, P0: 1.0}

# Conservative sizing of a request before its usage is known.
_CHARS_PER_TOKEN = 3.0
_REQUEST_OVERHEAD_TOKENS = 16
_LEGACY_ATTEMPT_WINDOW = 60.0
_VERSION = 2


class SpendDenied(Exception):
    """The ledger refuses a request. ``reason`` is a bounded metric label."""

    REASONS = ('daily', 'monthly', 'priority', 'rate', 'requests', 'state')

    def __init__(self, reason, detail=''):
        super().__init__(f'{reason}: {detail}')
        if reason not in self.REASONS:
            raise ValueError(f'unknown spend denial {reason}')
        self.reason = reason


class Prices:
    """USD per million tokens. Configuration, so a price change needs no release."""

    def __init__(self, input_per_m=0.95, cached_input_per_m=0.16, output_per_m=4.0):
        self.input_per_m = self._price(input_per_m, 'input_per_m')
        self.cached_input_per_m = self._price(cached_input_per_m, 'cached_input_per_m')
        self.output_per_m = self._price(output_per_m, 'output_per_m')

    @staticmethod
    def _price(value, name):
        try:
            price = Decimal(str(value))
        except Exception:
            raise ValueError(f'price {name} must be a number, got {value!r}') from None
        if not price.is_finite() or price < 0:
            raise ValueError(f'price {name} must be a non-negative number, got {value!r}')
        return price

    def cost_micros(self, input_tokens, cached_tokens, output_tokens):
        """Cost in whole micro-USD, rounded up. ``cached_tokens`` is part of the input."""
        input_tokens = max(0, int(input_tokens))
        cached = min(max(0, int(cached_tokens)), input_tokens)
        total = ((input_tokens - cached) * self.input_per_m
                 + cached * self.cached_input_per_m
                 + max(0, int(output_tokens)) * self.output_per_m)
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    def as_dict(self):
        return {'input_per_m': float(self.input_per_m),
                'cached_input_per_m': float(self.cached_input_per_m),
                'output_per_m': float(self.output_per_m)}


class Reservation:
    """A cost held for one in-flight attempt; settle it once the reply is known."""

    __slots__ = ('priority', 'micros', 'day', 'month', 'settled')

    def __init__(self, priority, micros, day, month):
        self.priority = priority
        self.micros = micros
        self.day = day
        self.month = month
        self.settled = False


class PlayerReservation:
    """One reserved model response for a numeric human id; no chat or names."""

    def __init__(self, day, player_id):
        self.day, self.player_id, self.released = day, player_id, False


def usage_tokens(usage):
    """(input, cached, output) tokens from a provider ``usage`` object, or None.

    Reads the OpenAI-style ``prompt_tokens_details.cached_tokens`` and the
    Moonshot-style top-level ``cached_tokens``. A missing or malformed usage
    returns None so the caller keeps its estimate.
    """
    if not isinstance(usage, dict):
        return None

    def count(value):
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if number >= 0 else None

    prompt = count(usage.get('prompt_tokens'))
    completion = count(usage.get('completion_tokens'))
    if prompt is None or completion is None:
        return None
    cached = count(usage.get('cached_tokens'))
    if cached is None:
        details = usage.get('prompt_tokens_details')
        cached = count(details.get('cached_tokens')) if isinstance(details, dict) else None
    return prompt, min(cached or 0, prompt), completion


def _utc_day(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%d')


def _valid_day(value):
    if value == '':  # an unused legacy counter
        return True
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


class SpendLedger:
    """Reserve/settle ledger. Single event loop: no await between read and write."""

    def __init__(self, path=None, *, daily_usd=1.0, monthly_usd=30.0,
                 per_minute=30, max_requests_per_day=0, prices=None,
                 clock=time.time):
        self.daily_cap = self._cap(daily_usd, 'daily_usd')
        self.monthly_cap = self._cap(monthly_usd, 'monthly_usd')
        self.per_minute = int(per_minute)
        self.max_requests_per_day = int(max_requests_per_day or 0)
        if self.per_minute < 1 or self.max_requests_per_day < 0:
            raise ValueError('request limits must be positive')
        self.prices = prices if prices is not None else Prices()
        self.clock = clock
        self.path = Path(path) if path else None
        self.state_ok = True
        self.day = ''
        self.month = ''
        self.day_micros = 0
        self.month_micros = 0
        self.requests_day = 0
        self.by_priority = {name: 0 for name in PRIORITIES}  # today's settled micros
        self.attempts = []
        self.players = {}  # today's numeric human id -> reserved/sent model responses
        self.events = {}  # last claimed local-calendar key, four fixed event kinds; no script text
        if self.path and (self.path.exists() or self.path.is_symlink()):
            self._load()

    @staticmethod
    def _cap(value, name):
        try:
            cap = Decimal(str(value))
        except Exception:
            raise ValueError(f'{name} must be a number, got {value!r}') from None
        if not cap.is_finite() or cap <= 0:
            raise ValueError(f'{name} must be a positive number of USD, got {value!r}')
        return int((cap * 1_000_000).to_integral_value(rounding=ROUND_CEILING))

    # ------------------------------------------------------------ persistence
    def _check_file(self):
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid()):
            raise ValueError('budget file must be private and not a symlink')

    def _load(self):
        self._check_file()
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('invalid budget ledger') from None
        if not isinstance(data, dict):
            raise ValueError('invalid budget ledger')
        if data.get('version') == _VERSION:
            self._load_current(data)
        elif 'version' not in data:
            self._load_legacy(data)
        else:
            raise ValueError('unsupported budget ledger version')

    def _load_current(self, data):
        try:
            day, month = str(data['day']), str(data['month'])
            day_micros, month_micros = int(data['day_micros']), int(data['month_micros'])
            requests = int(data['used'])
            attempts = [float(stamp) for stamp in data['attempts']]
            by_priority = data.get('by_priority') or {}
            parsed = {name: int(by_priority.get(name, 0)) for name in PRIORITIES}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('invalid budget ledger') from None
        if min(day_micros, month_micros, requests, *parsed.values()) < 0:
            raise ValueError('invalid budget ledger')
        if (not isinstance(data['attempts'], list) or any(not math.isfinite(s) for s in attempts)
                or not _valid_day(day) or month != day[:7]):
            raise ValueError('invalid budget ledger')
        self.day, self.month = day, month
        self.day_micros, self.month_micros = day_micros, month_micros
        self.requests_day, self.attempts, self.by_priority = requests, attempts, parsed
        players = data.get('players', {})
        if (not isinstance(players, dict) or len(players) > 4096
                or any(not isinstance(key, str) or not key.isdigit() or int(key) <= 0
                       or type(value) is not int or value < 0 for key, value in players.items())):
            raise ValueError('invalid player quota ledger')
        self.players = {int(key): value for key, value in players.items()}
        events = data.get('events', {})
        if (not isinstance(events, dict) or set(events) - {'theatre', 'party', 'race', 'dojo'}
                or any(not isinstance(value, str) or len(value) > 32 for value in events.values())):
            raise ValueError('invalid event claim ledger')
        self.events = events

    def _load_legacy(self, data):
        """Old ``{day, used, attempts}`` counter: carry over today's request count.

        Its requests had no recorded price, so today's spend is estimated with a
        typical request (larger than the design's average) to stay conservative.
        """
        used, attempts, day = data.get('used'), data.get('attempts'), data.get('day')
        if (not isinstance(used, int) or isinstance(used, bool) or used < 0
                or not isinstance(attempts, list) or not isinstance(day, str)
                or any(isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                       or not math.isfinite(stamp) for stamp in attempts)
                or not _valid_day(day)):
            raise ValueError('invalid budget ledger')
        self.day = day
        self.requests_day = used
        self.attempts = [float(stamp) for stamp in attempts]
        if day[:7] == _utc_day(self.clock())[:7]:
            self.month = day[:7]
            self.month_micros = used * self._legacy_request_micros()
            if day == _utc_day(self.clock()):
                self.day_micros = self.month_micros
        logger.info('budget ledger in the legacy format converted in memory '
                    '(%d requests today)', used)

    def _legacy_request_micros(self):
        return self.prices.cost_micros(1000, 0, 120)

    def _document(self):
        return {
            'version': _VERSION,
            'day': self.day, 'month': self.month,
            'day_micros': self.day_micros, 'month_micros': self.month_micros,
            'by_priority': dict(self.by_priority),
            # Legacy keys, so the previous image can still read this file.
            'used': self.requests_day, 'attempts': list(self.attempts),
            'players': {str(key): value for key, value in self.players.items()},
            'events': dict(self.events),
        }

    def _persist(self):
        """Atomic private write. Raises OSError/ValueError when it cannot be saved."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() or self.path.is_symlink():
            self._check_file()
        temp = self.path.with_suffix('.tmp')
        if self.path.is_symlink() or temp.is_symlink():
            raise ValueError('budget path must not be a symlink')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(self._document(), handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, self.path)

    # ----------------------------------------------------------------- rolls
    def _roll(self, now):
        """Start a new day/month when the clock moves forward (never backwards)."""
        today = _utc_day(now)
        if today > self.day:
            self.day = today
            self.day_micros = 0
            self.requests_day = 0
            self.by_priority = {name: 0 for name in PRIORITIES}
            self.players = {}
        if today[:7] > self.month:
            self.month = today[:7]
            self.month_micros = 0

    # --------------------------------------------------------------- sizing
    def estimate_micros(self, messages, max_completion_tokens):
        """Upper-ish cost of a request: input sized by characters, output at the cap."""
        chars = sum(len(str(item.get('content', ''))) + 4 for item in messages)
        input_tokens = math.ceil(chars / _CHARS_PER_TOKEN) + _REQUEST_OVERHEAD_TOKENS
        return self.prices.cost_micros(input_tokens, 0, max_completion_tokens)

    # ----------------------------------------------------------------- state
    def ratio(self):
        """Fraction of today's cap already held (settled plus in flight)."""
        return self.day_micros / self.daily_cap

    def allows(self, priority):
        """Cheap pre-check: would this class be served now? (No reservation.)"""
        self._roll(self.clock())
        if priority not in CUTOFF_RATIO:
            raise ValueError(f'unknown priority {priority!r}')
        return (self.ratio() < CUTOFF_RATIO[priority]
                and self.month_micros < self.monthly_cap)

    def snapshot(self):
        self._roll(self.clock())
        return {'day': self.day, 'day_micros': self.day_micros,
                'month_micros': self.month_micros, 'daily_cap_micros': self.daily_cap,
                'monthly_cap_micros': self.monthly_cap, 'ratio': self.ratio(),
                'requests_today': self.requests_day, 'state_ok': self.state_ok,
                'by_priority': dict(self.by_priority)}

    # ------------------------------------------------------------ reserve/settle
    def claim_event(self, kind, key):
        """Persist a calendar claim before generating; retries still count in USD."""
        if kind not in ('theatre', 'party', 'race', 'dojo') or not isinstance(key, str) or len(key) > 32:
            raise ValueError('invalid event claim')
        self._roll(self.clock())
        previous = self.events.get(kind)
        if previous is not None and previous >= key:
            return False
        self.events[kind] = key
        try:
            self._persist()
        except (OSError, ValueError, TypeError):
            if previous is None:
                self.events.pop(kind, None)
            else:
                self.events[kind] = previous
            self.state_ok = False
            raise SpendDenied('state', 'event claim cannot be saved') from None
        self.state_ok = True
        return True

    def reserve_player(self, player_id, limit=40):
        """Persist a response slot before inference; None at the per-human cap."""
        if type(player_id) is not int or player_id <= 0 or type(limit) is not int or limit < 1:
            raise ValueError('invalid player quota')
        self._roll(self.clock())
        previous = self.players.get(player_id, 0)
        if previous >= limit or (player_id not in self.players and len(self.players) >= 4096):
            return None
        self.players[player_id] = previous + 1
        try:
            self._persist()
        except (OSError, ValueError, TypeError):
            if previous:
                self.players[player_id] = previous
            else:
                self.players.pop(player_id, None)
            self.state_ok = False
            raise SpendDenied('state', 'player quota cannot be saved') from None
        self.state_ok = True
        return PlayerReservation(self.day, player_id)

    def release_player(self, reservation):
        """Local/filtered/cancelled replies release their slot at most once."""
        if reservation.released:
            return
        reservation.released = True
        if reservation.day != self.day:
            return
        self.players[reservation.player_id] = max(0, self.players.get(reservation.player_id, 0) - 1)
        try:
            self._persist()
            self.state_ok = True
        except (OSError, ValueError, TypeError):
            # Disk still holds the more conservative count. No paid call was
            # enabled by this refund; the next admission must persist again.
            self.state_ok = False
            logger.error('player quota refund not saved')

    def reserve(self, priority, micros):
        """Hold ``micros`` for one attempt or raise :class:`SpendDenied`."""
        if priority not in CUTOFF_RATIO:
            raise ValueError(f'unknown priority {priority!r}')
        micros = max(0, int(micros))
        now = self.clock()
        self._roll(now)
        recent = [stamp for stamp in self.attempts if stamp > now - _LEGACY_ATTEMPT_WINDOW]
        if len(recent) >= self.per_minute:
            raise SpendDenied('rate', 'per-minute request window is full')
        if self.max_requests_per_day and self.requests_day >= self.max_requests_per_day:
            raise SpendDenied('requests', 'daily request cap reached')
        if self.month_micros + micros > self.monthly_cap:
            raise SpendDenied('monthly', 'monthly cap would be exceeded')
        if self.day_micros + micros > self.daily_cap:
            raise SpendDenied('daily', 'daily cap would be exceeded')
        if self.ratio() >= CUTOFF_RATIO[priority]:
            raise SpendDenied('priority', f'{priority} is switched off at this spend level')
        before = (self.day_micros, self.month_micros, self.requests_day, self.attempts)
        self.day_micros += micros
        self.month_micros += micros
        self.requests_day += 1
        self.attempts = recent + [now]
        try:
            self._persist()
        except (OSError, ValueError, TypeError) as error:
            # Fail closed: an unrecorded request must not be sent.
            self.day_micros, self.month_micros, self.requests_day, self.attempts = before
            self.state_ok = False
            raise SpendDenied('state', f'ledger cannot be saved ({type(error).__name__})') from None
        self.state_ok = True
        return Reservation(priority, micros, self.day, self.month)

    def settle(self, reservation, usage):
        """Replace the estimate with the real cost; returns the micros charged."""
        if reservation.settled:
            return reservation.micros
        self._roll(self.clock())
        reservation.settled = True
        tokens = usage_tokens(usage)
        actual = reservation.micros if tokens is None else self.prices.cost_micros(*tokens)
        delta = actual - reservation.micros
        if reservation.day == self.day:
            self.day_micros = max(0, self.day_micros + delta)
            self.by_priority[reservation.priority] = (
                self.by_priority.get(reservation.priority, 0) + actual)
        if reservation.month == self.month:
            self.month_micros = max(0, self.month_micros + delta)
        reservation.micros = actual
        try:
            self._persist()
            self.state_ok = True
        except (OSError, ValueError, TypeError) as error:
            # Already sent and billed: keep counting in memory. The next
            # reserve() rewrites the whole state and fails closed if it cannot.
            self.state_ok = False
            logger.error('ledger reconcile not saved (%s)', type(error).__name__)
        return actual

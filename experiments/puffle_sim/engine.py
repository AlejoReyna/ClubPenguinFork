"""Bounded in-memory conversation prototype, deliberately not a game adapter.

The caller must establish and revalidate game authority. This module does not
authenticate users, classify bots, deliver packets, or persist credentials.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import time
import unicodedata
from uuid import uuid4


def safe_text(value, limit):
    if not isinstance(value, str):
        return ''
    # Protocol delimiters, markup, and every Unicode control/format character
    # are removed rather than interpreted. The final limit is in UTF-8 bytes.
    value = ''.join(' ' if c in '%|<>&' or unicodedata.category(c).startswith('C') else c
                    for c in value)
    value = ' '.join(value.split())
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore').strip()


# Same cap as the penguin chat input (interface.swf chat_input maxChars = 48), so the
# puffle reuses the DefaultBalloon at the size players already see (<= 3 lines).
MAX_SPEECH_CHARS = 48


def speech_text(value):
    return safe_text(value, 120)[:MAX_SPEECH_CHARS].strip()


def select_puffle(owned_visible_ids, preferred=None):
    ids = sorted({x for x in owned_visible_ids if type(x) is int and x > 0})
    return preferred if preferred in ids else (ids[0] if ids else None)


class QuotaExhausted(Exception):
    pass


@dataclass(frozen=True)
class Binding:
    player: int
    igloo: int
    puffle: int
    session: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class _Session:
    binding: Binding
    history: deque = field(default_factory=lambda: deque(maxlen=12))
    pending: int = 0
    first_pending_at: float = 0.0
    task: asyncio.Task | None = None
    closed: bool = False
    paused: bool = False
    last_evaluation: float = 0.0


class Engine:
    """Serializes a player's evaluations; callers must deliver only accepted chat.

    valid(binding) and deliver(binding, text, request_id) are test adapters,
    not a replacement for Houdini's room/ownership checks or packet renderer.
    """
    def __init__(self, provider, valid, deliver, *, max_sessions=32,
                 max_parallel=2, batch_seconds=0.2, min_interval=1.0,
                 deadline=5.0, max_pending=12):
        if min(max_sessions, max_parallel, max_pending) < 1 or deadline <= 0:
            raise ValueError('invalid bounds')
        if batch_seconds < 0 or min_interval < 0:
            raise ValueError('invalid timing')
        self.provider, self.valid, self.deliver = provider, valid, deliver
        self.max_sessions, self.max_pending = max_sessions, max_pending
        self.batch_seconds, self.min_interval, self.deadline = batch_seconds, min_interval, deadline
        self.sessions = {}
        self.slots = asyncio.Semaphore(max_parallel)

    def start(self, binding):
        if binding.player in self.sessions or len(self.sessions) >= self.max_sessions:
            return False
        if not self.valid(binding):
            return False
        # Keep room for an entire pending batch even if an in-flight reply is
        # appended before that batch is evaluated.
        self.sessions[binding.player] = _Session(
            binding, history=deque(maxlen=12 + self.max_pending + 1))
        return True

    def observe(self, binding, message):
        s = self.sessions.get(binding.player)
        if not s or s.binding != binding or s.closed or s.paused or not self.valid(binding):
            return False
        message = safe_text(message, 512)
        if not message:
            return False
        s.history.append(('player', message))
        if s.pending == 0:
            s.first_pending_at = time.monotonic()
        # Retain the newest eligible messages even when the pending batch is
        # full. The bounded history deque evicts the oldest context instead.
        s.pending = min(s.pending + 1, self.max_pending)
        if s.task is None:
            s.task = asyncio.create_task(self._run(s))
        return True

    def cancel(self, binding):
        """Invalidate synchronously before a room change can send a late reply."""
        s = self.sessions.get(binding.player)
        if not s or s.binding != binding:
            return None
        del self.sessions[binding.player]
        s.closed = True
        s.history.clear()
        s.pending = 0
        if s.task:
            s.task.cancel()
        return s.task

    async def stop(self, binding):
        task = self.cancel(binding)
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def _active(self, s):
        return (not s.closed and self.sessions.get(s.binding.player) is s
                and self.valid(s.binding))

    async def _run(self, s):
        try:
            while s.pending and self._active(s) and not s.paused:
                expires = s.first_pending_at + self.deadline
                delay = max(self.batch_seconds, s.last_evaluation + self.min_interval - time.monotonic())
                await asyncio.sleep(min(delay, max(0, expires - time.monotonic())))
                if time.monotonic() >= expires:
                    s.pending = 0
                    continue
                snapshot = tuple(s.history)
                s.pending = 0
                request_id = uuid4().hex
                s.last_evaluation = time.monotonic()
                try:
                    async with asyncio.timeout(max(0, expires - time.monotonic())):
                        async with self.slots:
                            if not self._active(s):
                                return
                            evaluate_for = getattr(self.provider, 'evaluate_for', None)
                            if evaluate_for is not None:
                                # Per-player actions (e.g. Spotify) get the server-validated
                                # binding's player and a live re-check right before acting.
                                decision, answer = await evaluate_for(
                                    s.binding.player, snapshot, lambda: self._active(s))
                            else:
                                decision, answer = await self.provider.evaluate(snapshot)
                except QuotaExhausted:
                    s.paused = True
                    s.pending = 0
                    if self._active(s) and time.monotonic() < expires:
                        await self.deliver(s.binding, 'Necesito descansar un rato.', request_id)
                    continue
                except Exception:
                    # Provider exceptions may contain chat or secrets: never log
                    # them here. Cancellation remains a BaseException.
                    continue
                if not self._active(s) or time.monotonic() >= expires:
                    continue
                answer = speech_text(answer)
                if decision is True and answer:
                    await self.deliver(s.binding, answer, request_id)
                    if self._active(s):
                        s.history.append(('puffle', answer))
        finally:
            s.task = None

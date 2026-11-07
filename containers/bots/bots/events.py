"""Bounded local-calendar events; all speech uses the room director queue."""
import asyncio
from collections import OrderedDict
import datetime

from . import spend
from .travel import TravelDenied


class Events:
    def __init__(self, director, now=None):
        self.director = director
        self.config = director.config
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.seen = OrderedDict()
        self.current = {}  # room -> one of four fixed event kinds, never chat
        self.task = None

    def due(self, now):
        now = now.astimezone(self.config.service.tz)
        if now.minute >= 10:
            return []
        events = []
        if now.hour == 20:
            events.append(('theatre', 340))
        if now.weekday() == 4 and now.hour == 21:
            events.append(('party', 120))
        if now.hour % 2 == 0:
            events.append(('race', 230))
        if now.weekday() == 5 and now.hour == self.config.life.dojo_hour:
            events.append(('dojo', 320))
        key = now.strftime('%Y-%m-%dT%H:00')
        return [(kind, room, key) for kind, room in events if (kind, key) not in self.seen]

    def pinned(self, session, now=None):
        """Prepare fixed performers five minutes before an event; never force a conversation away."""
        now = (now or self.now()).astimezone(self.config.service.tz)
        minute = now.hour * 60 + now.minute
        name = session.bot.username
        if name in ('dontelon', 'divaperla', 'bruno', 'kiki'):
            return 19 * 60 + 55 <= minute < 20 * 60 + 10
        if name in ('bruma', 'tormenta') and now.weekday() == 5:
            start = self.config.life.dojo_hour * 60
            return (minute - start + 5) % (24 * 60) < 15
        if name == 'avalancha':
            return (minute + 5) % 120 < 15
        return False

    async def tick(self, now=None):
        for kind, room, key in self.due(now or self.now()):
            if kind == 'party':
                # Party migration is purely local; no provider invocation.
                candidates = list(self.director.sessions)
                self.director.travel.rng.shuffle(candidates)
                accepted_count = 0
                for session in candidates:
                    card = session.bot.card
                    if card is None or card.chattiness < .5 or session.room_id in (120, 121):
                        continue
                    target = (120, 121)[accepted_count % 2]
                    try:
                        self.director.travel.start(self.director.travel.reserve(session, target, kind='party'))
                        accepted_count += 1
                        if accepted_count == 4:
                            break
                    except TravelDenied:
                        pass
                accepted = True
            else:
                names = {'theatre': ('dontelon', 'divaperla', 'bruno', 'kiki'),
                         'race': ('avalancha', 'capigol'), 'dojo': ('bruma', 'tormenta')}[kind]
                speakers = [s for s in self.director.active(room) if s.bot.username in names]
                minimum, maximum = {'theatre': (8, 12), 'race': (1, 3), 'dojo': (4, 6)}[kind]
                if kind == 'theatre' and len(speakers) != 4:
                    continue
                accepted = self.director.event(kind, room, key, speakers, minimum, maximum)
            if accepted:
                self.seen[(kind, key)] = True
                while len(self.seen) > 128:
                    self.seen.popitem(last=False)

    async def model_allowed(self, event, session):
        kind, key, _minimum, _maximum = event
        ledger = self.director.ledger
        if ledger is None:
            return True
        try:
            return ledger.claim_event(kind, key)
        except spend.SpendDenied:
            await session.metrics.spend_denied('state')
            await session.metrics.ledger_state(False)
            return None  # local script only; no provider when the claim cannot be saved

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop(), name='island-calendar')

    async def _loop(self):
        while True:
            await self.tick()
            await asyncio.sleep(5)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.current.clear()

"""Local, seeded bodies. Optional Phase 3 travel goes through the director.

Unmeasured rooms hold their documented spawn. Walks stay in a single measured
rectangle, including the segment from the previous position to the target.
"""
import asyncio
import datetime
import math
import random
import zlib

from . import actions, as2


class Life:
    def __init__(self, session, rng=None, clock=None):
        self.session = session
        self.bot = session.bot
        self.card = self.bot.card
        seed = session.service.life_seed if session.service is not None else 0
        self.rng = rng or random.Random(seed ^ zlib.crc32(self.bot.username.encode()))
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.needs = dict(energy=0.8, hunger=0.2, social=0.3, boredom=0.0)
        self.activity = 'idle'
        self.position = self.bot.spawn
        self._last = None
        self._choices = {}  # chance for each schedule block on the current local day
        self._day = None

    def choose(self, now=None):
        now = now or self.clock()
        if self.session.service is not None:
            now = now.astimezone(self.session.service.tz)
        elapsed = max(0.0, (now - self._last).total_seconds()) if self._last else 0.0
        self._last = now
        # Need values are bounded and change with elapsed time, not loop frequency.
        rates = {'energy': -0.0003, 'hunger': 0.0004, 'social': 0.0002, 'boredom': 0.0005}
        recovery = {'rest': ('energy', 0.002), 'eat': ('hunger', -0.003),
                    'wave': ('social', -0.002), 'walk': ('boredom', -0.003)}
        if self.activity in recovery:
            key, rate = recovery[self.activity]
            rates[key] = rate
        for key, rate in rates.items():
            self.needs[key] = min(1.0, max(0.0, self.needs[key] + elapsed * rate))
        if self._day != now.date():
            self._day, self._choices = now.date(), {}
        minute = now.hour * 60 + now.minute
        planned = None
        for index, block in enumerate(self.card.schedule):
            room = self.session.room_id if getattr(self.session, 'travel', None) else self.bot.room
            if block.room != room or not block.covers(minute):
                continue
            if index not in self._choices:
                self._choices[index] = self.rng.random() < block.chance
            if self._choices[index]:
                planned = block.activity
                break
        if self.needs['energy'] < 0.2:
            activity = 'rest'
        elif self.needs['hunger'] > 0.85:
            activity = 'eat'
        elif planned:
            activity = planned
        elif self.needs['social'] > 0.8:
            activity = 'wave'
        elif self.needs['boredom'] > 0.6:
            activity = 'walk'
        else:
            activity = self.rng.choice(list(self.card.idle_actions) + ['walk', 'idle'])
        self.activity = activity
        return activity

    def target(self):
        visiting = bool(getattr(self.session, 'travel', None) and self.session.room_id != self.bot.room)
        room = self.session.catalog.require(self.session.room_id if visiting else self.bot.room)
        if not room.measured or self.position is None:
            return None
        # No pathfinding yet: only a convex, measured rectangle is safe to cross.
        reachable = [rect for rect in room.walk if rect.contains(*self.position)]
        if not reachable:
            return None
        travel = getattr(self.session, 'travel', None)
        lease = travel.leases.get(self.bot.index) if travel else None
        if lease and lease.human is not None:
            human = next((m for m in self.session.world.humans_in(room.id) if m.id == lease.human), None)
            if human and human.x is not None and human.y is not None:
                point = (human.x, human.y)
                return point if any(rect.contains(*point) for rect in reachable) else None
            return None
        if self.activity != 'walk':
            for name in (() if visiting else self.card.home_pois):
                point = room.poi_point(name, self.rng)
                if point is not None and any(rect.contains(*point) for rect in reachable):
                    return point
            return None
        return self.rng.choice(reachable).point(self.rng)

    async def step(self):
        travel = getattr(self.session, 'travel', None)
        if travel:
            self.choose()
            await travel.consider(self, self._last)
            if self.bot.index in travel.jobs or self.bot.index in travel.reservations:
                return
        async with self.session.body_lock:
            await self._step(chosen=travel is not None)

    async def _step(self, chosen=False):
        if not chosen:
            self.choose()
        # Without Phase 3, an explicit change_room must not run a home walk elsewhere.
        if self.session.room_id != self.bot.room and not getattr(self.session, 'travel', None):
            return
        point = self.target()
        if point is not None and point != self.position:
            distance = math.hypot(point[0] - self.position[0], point[1] - self.position[1])
            await self.session.send_move(*point)
            self.position = point
            # Conservative local pacing before a frame, so it doesn't interrupt a walk.
            await asyncio.sleep(max(0.6, distance / 40.0 + 0.5))
        action = actions.BODY_ACTIVITY.get(self.activity)
        if action is None and self.activity != 'walk':
            room = self.session.catalog.require(self.session.room_id)
            action = next((room.pois[name].action for name in self.card.home_pois
                           if self.session.room_id == self.bot.room
                           and name in room.pois and room.pois[name].action), None)
        if action:
            kind, number = actions.BODY_ACTIONS[action]
            if kind == actions.KIND_FRAME:
                await self.session.send_frame(number)
            else:
                await self.session.send_action(number)

    async def run(self):
        settings = self.session.behavior.behavior
        try:
            await asyncio.sleep(self.rng.uniform(settings.first_move_delay_min,
                                                settings.first_move_delay_max))
            while True:
                await self.step()
                await asyncio.sleep(self.rng.uniform(settings.life_min_delay, settings.life_max_delay))
        except as2.Disconnected:
            return

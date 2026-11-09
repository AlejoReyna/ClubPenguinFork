"""Local, seeded bodies. Optional Phase 3 travel goes through the director.

Unmeasured rooms hold their documented spawn. Walks stay in a single measured
rectangle, including the segment from the previous position to the target.

With ``behavior.personal_space`` > 0 a body also keeps its distance (spacing.py):
it walks to free spots, steps off a crowded arrival point, stays near the person
it is talking to and stands beside, not on, a player it follows. With
``behavior.rest_median`` > 0 it moves like a player: long, uneven pauses, short
bursts of clicks and, now and then, a few minutes away from the keyboard.
"""
import asyncio
import datetime
import math
import random
import zlib

from . import actions, as2, spacing

BURST_MAX = 3             # clicks in one burst of walking
BURST_GAP = (2.0, 6.0)    # seconds between the clicks of a burst
ATTENTIVE_REST = (3.0, 8.0)  # pauses while following someone or mid-conversation
AFK_SECONDS = (240.0, 600.0)
REST_SIGMA = 0.6          # lognormal spread: most pauses near the median, a few much longer


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
        self.afk = False   # away from the keyboard: no clicks, skipped by unnamed chat
        self._wake = None  # asyncio.Event while away; a direct mention sets it

    def _settings(self):
        return getattr(getattr(self.session, 'behavior', None), 'behavior', None)

    def personal_space(self):
        """Minimum distance (px) kept from other penguins; 0 = the original random walk."""
        return float(getattr(self._settings(), 'personal_space', 0) or 0)

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
            settings = getattr(getattr(self.session, 'travel', None), 'settings', None)
            weight = max(1, int(getattr(settings, 'walk_weight', 1)))
            activity = self.rng.choice(list(self.card.idle_actions) + ['walk'] * weight + ['idle'])
        self.activity = activity
        return activity

    # ------------------------------------------------------------- the floor
    def _floor(self):
        """(room, visiting, rectangles this body may stand on); no rectangles = hold the post."""
        visiting = bool(getattr(self.session, 'travel', None) and self.session.room_id != self.bot.room)
        room = self.session.catalog.require(self.session.room_id if visiting else self.bot.room)
        settings = getattr(getattr(self.session, 'travel', None), 'settings', None)
        approximate = bool(getattr(settings, 'approximate_walk', 0))
        return room, visiting, room.wander_rects(approximate)

    def _reachable(self, rects):
        # No pathfinding yet: only a convex rectangle (measured, else labelled approximate)
        # is crossed in a straight line.
        if self.position is None:
            return []
        return [rect for rect in rects if rect.contains(*self.position)]

    def _others(self, room_id):
        """Every other penguin the shared world model places in ``room_id``."""
        world = getattr(self.session, 'world', None)
        if world is None:
            return []
        me = getattr(self.session, 'player_id', None)
        return [(m.x, m.y) for m in world.members(room_id)
                if m.id != me and m.x is not None and m.y is not None]

    def _person(self, room_id, player_id):
        world = getattr(self.session, 'world', None)
        member = next((m for m in world.humans_in(room_id) if m.id == player_id), None) if world else None
        if member is None or member.x is None or member.y is None:
            return None
        return member.x, member.y

    def _partner(self):
        """The human this bot is chatting with right now (director continuity), or None."""
        partner = getattr(getattr(self.session, 'director', None), 'partner', None)
        return partner(self.session) if partner else None

    def _attentive(self):
        travel = getattr(self.session, 'travel', None)
        lease = travel.leases.get(self.bot.index) if travel else None
        return (lease is not None and lease.human is not None) or self._partner() is not None

    def _beside(self, center, reachable, others, personal):
        """Stay when already at talking distance on a free spot; else a free spot of the ring."""
        if (math.dist(self.position, center) <= spacing.RING[1] + 20
                and spacing.can_stay(self.position, others, personal)):
            return None
        return spacing.ring_spot(center, reachable, others, self.rng, personal=personal,
                                 origin=self.position)

    def target(self):
        room, visiting, rects = self._floor()
        if not rects or self.position is None:
            return None
        reachable = self._reachable(rects)
        if not reachable:
            return None
        personal = self.personal_space()
        others = self._others(room.id) if personal > 0 else []
        travel = getattr(self.session, 'travel', None)
        lease = travel.leases.get(self.bot.index) if travel else None
        if lease and lease.human is not None:
            point = self._person(room.id, lease.human)
            if point is None:
                return None
            if personal > 0:
                return self._beside(point, reachable, others, personal)
            return point if any(rect.contains(*point) for rect in reachable) else None
        if personal > 0:
            partner = self._partner()
            center = self._person(room.id, partner) if partner is not None else None
            if center is not None:
                return self._beside(center, reachable, others, personal)
        if self.activity != 'walk':
            for name in (() if visiting else self.card.home_pois):
                poi = room.pois.get(name)
                if poi is None:
                    continue
                if personal > 0:
                    if poi.rect.contains(*self.position) and spacing.can_stay(self.position, others, personal):
                        return None  # already on a free spot of its post: stay and do the activity
                    point = spacing.pick_spot([poi.rect], others, self.rng, personal=personal)
                else:
                    point = room.poi_point(name, self.rng)
                if point is not None and any(rect.contains(*point) for rect in reachable):
                    return point
            # No post for this activity here: active labels (play, sled, patrol...) move around.
            roam = actions.ROAMING.get(self.activity, 0)
            if not roam or self.rng.random() >= roam:
                return None
        if personal > 0:
            return spacing.pick_spot(reachable, others, self.rng, personal=personal)
        return self.rng.choice(reachable).point(self.rng)

    # ---------------------------------------------------- moves outside a step
    async def _move(self, point):
        if point is None or point == self.position:
            return False
        await self.session.send_move(*point)
        self.position = point
        return True

    async def settle(self):
        """After joining a room, step off a crowded arrival point onto a free spot nearby.

        The caller holds ``session.body_lock``. True when the body moved.
        """
        personal = self.personal_space()
        if personal <= 0 or self.position is None:
            return False
        room, _visiting, rects = self._floor()
        reachable = self._reachable(rects)
        others = self._others(room.id)
        if not reachable or spacing.can_stay(self.position, others, personal):
            return False
        return await self._move(spacing.pick_spot(reachable, others, self.rng, personal=personal,
                                                  near=self.position, radius=spacing.ARRIVAL_RADIUS))

    async def approach(self, human):
        """Walk to talking distance of ``human``; the caller holds ``session.body_lock``."""
        personal = self.personal_space()
        if personal <= 0 or self.position is None:
            return False
        room, _visiting, rects = self._floor()
        center = self._person(room.id, human)
        reachable = self._reachable(rects)
        if room.id != self.session.room_id or center is None or not reachable:
            return False
        return await self._move(self._beside(center, reachable, self._others(room.id), personal))

    # ------------------------------------------------------------------ steps
    async def step(self):
        """One life step; True when the body walked somewhere."""
        travel = getattr(self.session, 'travel', None)
        if travel:
            self.choose()
            await travel.consider(self, self._last)
            if self.bot.index in travel.jobs or self.bot.index in travel.reservations:
                return False
        async with self.session.body_lock:
            return await self._step(chosen=travel is not None)

    async def _again(self):
        """The next click of a walking burst."""
        travel = getattr(self.session, 'travel', None)
        moving = self.activity == 'walk' or self.activity in actions.ROAMING
        if not moving or (travel and (self.bot.index in travel.jobs
                                      or self.bot.index in travel.reservations)):
            return False
        async with self.session.body_lock:
            return await self._step(chosen=True)

    async def _step(self, chosen=False):
        if not chosen:
            self.choose()
        # Without Phase 3, an explicit change_room must not run a home walk elsewhere.
        if self.session.room_id != self.bot.room and not getattr(self.session, 'travel', None):
            return False
        point = self.target()
        moved = False
        if point is not None and point != self.position:
            distance = math.hypot(point[0] - self.position[0], point[1] - self.position[1])
            await self.session.send_move(*point)
            self.position = point
            moved = True
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
        return moved

    # ----------------------------------------------------------------- pacing
    def _rest(self, settings):
        """Seconds until the next step: long-tailed like a player, short while attentive."""
        if self._attentive():
            return self.rng.uniform(*ATTENTIVE_REST)
        median = float(settings.rest_median)
        value = self.rng.lognormvariate(math.log(median), REST_SIGMA)
        return min(max(value, min(settings.life_min_delay, median)), median * 5)

    def _goes_afk(self, settings):
        chance = float(getattr(settings, 'afk_chance', 0) or 0)
        return chance > 0 and not self._attentive() and self.rng.random() < chance

    async def _away(self):
        """A few minutes away from the keyboard; a direct mention (wake) brings it back."""
        self.afk, self._wake = True, asyncio.Event()
        try:
            await asyncio.wait_for(self._wake.wait(), self.rng.uniform(*AFK_SECONDS))
        except asyncio.TimeoutError:
            pass
        finally:
            self.afk, self._wake = False, None

    def wake(self):
        if self._wake is not None:
            self._wake.set()

    async def run(self):
        settings = self.session.behavior.behavior
        try:
            await asyncio.sleep(self.rng.uniform(settings.first_move_delay_min,
                                                settings.first_move_delay_max))
            async with self.session.body_lock:
                await self.settle()
            if float(getattr(settings, 'rest_median', 0) or 0) <= 0:
                while True:  # original pacing: one step every life_min..life_max seconds
                    await self.step()
                    await asyncio.sleep(self.rng.uniform(settings.life_min_delay, settings.life_max_delay))
            burst_chance = float(getattr(settings, 'burst_chance', 0) or 0)
            while True:
                await asyncio.sleep(self._rest(settings))
                if self._goes_afk(settings):
                    await self._away()
                    continue
                moved, clicks = await self.step(), 1
                while moved and clicks < BURST_MAX and self.rng.random() < burst_chance:
                    await asyncio.sleep(self.rng.uniform(*BURST_GAP))
                    moved, clicks = await self._again(), clicks + 1
        except as2.Disconnected:
            return

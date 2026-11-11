"""Local map travel with atomic occupancy reservations and bounded absence leases.

No guessed exits or walk targets: v1 can jump using the catalog's documented
spawn. A source exit is walked only when its whole segment is measured.
The director owns this controller; a model cannot bypass these decisions.
"""
import asyncio
from dataclasses import dataclass
import math
import random
import re
import time

from . import as2, text
from .session import RoomChangeError

ROOM_ALIASES = {100: ('pueblo',), 110: ('cafeteria', 'cafe'), 111: ('biblioteca',),
                120: ('disco', 'pista de baile'), 121: ('lounge', 'techo'),
                230: ('ski hill', 'colina', 'montana'), 300: ('plaza',),
                310: ('tienda de mascotas',), 320: ('dojo',), 330: ('pizzeria',),
                340: ('teatro',), 800: ('muelle',), 801: ('fuertes de nieve',), 802: ('estadio',)}


def invitation(message, catalog):
    folded = text.fold(message)
    if re.search(r'\b(?:no|nunca)\b', folded):
        return None
    if re.search(r'\b(?:sigueme|ven conmigo|acompaname)\b', folded):
        follow = True
    else:
        follow = False
    if not follow and not re.search(r'\b(?:vamos|vayamos|ven|llevame)\b', folded):
        return None
    matches = [room for room, aliases in ROOM_ALIASES.items()
               if room in catalog.rooms and text.mentions(folded, aliases)]
    if len(matches) == 1:
        return f'go:{matches[0]}'
    return 'follow' if follow and not matches else None


class TravelDenied(Exception):
    REASONS = ('offline', 'fixed', 'unknown', 'personality', 'conversation',
               'busy', 'minimum', 'capacity', 'budget', 'already', 'expired')

    def __init__(self, reason):
        if reason not in self.REASONS:
            raise ValueError('unknown travel rejection')
        self.reason = reason
        super().__init__(reason)


@dataclass
class Ticket:
    session: object
    source: int
    target: int
    connection: object
    seconds: float
    requester: object
    kind: str
    deadline: float


@dataclass
class Lease:
    expires: float
    human: object = None
    lost_since: object = None
    ready: bool = True


class Travel:
    def __init__(self, director, clock=time.monotonic, rng=None):
        self.director = director
        self.config, self.world = director.config, director.world
        self.settings = self.config.life
        self.sessions = director.sessions
        self.clock, self.rng = clock, rng or random.Random(self.config.service.life_seed)
        self.reservations, self.leases, self.jobs = {}, {}, {}
        self.plan = {}
        for session in self.sessions:
            self.plan.setdefault(session.bot.room, []).append(session)
        initial = min(180.0, self.settings.visit_seconds)
        self.bot_credit = {s.index: initial for s in self.sessions}
        self.room_credit = {room: initial for room in self.plan}
        self.last_update = self.clock()
        self.scheduler = None
        self.closed = False

    def online(self, session):
        return session.joined_room and session.conn is not None and not session.conn.closed

    def update(self):
        now = self.clock()
        elapsed = max(0, now - self.last_update)
        self.last_update = now
        for session in self.sessions:
            if not self.online(session):
                continue  # outages neither earn credit nor accumulate absence debt
            share = max(self.settings.minimum_home_share,
                        session.bot.card.wander_home_share if session.bot.card else 1)
            rate = (1 - share) / share if session.room_id == session.bot.room else -1
            self.bot_credit[session.index] = min(900, self.bot_credit[session.index] + elapsed * rate)
        for room, residents in self.plan.items():
            if all(self.online(s) for s in residents):
                full = all(s.room_id == room for s in residents)
                rate = (1 - self.settings.minimum_home_share) / self.settings.minimum_home_share if full else -1
                self.room_credit[room] = min(900, self.room_credit[room] + elapsed * rate)
        for key, ticket in list(self.reservations.items()):
            if now >= ticket.deadline and key not in self.jobs:
                self.reservations.pop(key, None)

    def projected(self, exclude=None):
        counts = {}
        for session in self.sessions:
            if self.online(session):
                counts[session.room_id] = counts.get(session.room_id, 0) + 1
        for ticket in self.reservations.values():
            if ticket is not exclude and ticket.session.room_id == ticket.source:
                counts[ticket.source] = counts.get(ticket.source, 0) - 1
                counts[ticket.target] = counts.get(ticket.target, 0) + 1
        return counts

    def holds(self, exclude=None):
        """Physical occupancy plus separate in-flight reservations.

        An arrival is never coverage until it has actually landed. Likewise,
        an outgoing visitor cannot free capacity before its departure finishes.
        """
        physical, outgoing, incoming = {}, {}, {}
        for session in self.sessions:
            if self.online(session):
                physical[session.room_id] = physical.get(session.room_id, 0) + 1
        for ticket in self.reservations.values():
            if ticket is not exclude and ticket.session.room_id == ticket.source:
                outgoing[ticket.source] = outgoing.get(ticket.source, 0) + 1
                incoming[ticket.target] = incoming.get(ticket.target, 0) + 1
        return physical, outgoing, incoming

    def _allowed_rooms(self, session):
        card = session.bot.card
        allowed = {session.bot.room} | set(card.wander_favorites) | {b.room for b in card.schedule}
        for other in self.sessions:
            if any(text.mentions(row['with'], other.bot.mention_names) for row in card.relationships):
                allowed.add(other.bot.room)
        return allowed

    def _check(self, session, target, requester, kind, exclude=None):
        if self.closed or not self.online(session):
            raise TravelDenied('offline')
        if exclude is not None and (session.room_id != exclude.source or session.conn is not exclude.connection):
            raise TravelDenied('expired')
        card = session.bot.card
        if card is None or card.wander_home_share >= 1:
            raise TravelDenied('fixed')
        room = self.config.catalog.get(target)
        if room is None or room.spawn is None or not room.spawn_source:
            raise TravelDenied('unknown')
        if session.room_id == target:
            raise TravelDenied('already')
        homecoming = target == session.bot.room
        if not homecoming and self.director.events and self.director.events.pinned(session):
            raise TravelDenied('busy')
        party = kind == 'party' and target in (120, 121) and card.chattiness >= .5
        if not homecoming and not party and target not in self._allowed_rooms(session):
            raise TravelDenied('personality')
        if session.index in self.reservations and self.reservations[session.index] is not exclude:
            raise TravelDenied('busy')
        state = self.director.rooms.get(session.room_id)
        own_turn = 1 if kind == 'invitation' else 0
        if state and state.pending.get(session.index, 0) > own_turn:
            raise TravelDenied('busy')
        if not homecoming and self.world.conversation_locked(session.player_id, requester):
            raise TravelDenied('conversation')
        physical, outgoing, incoming = self.holds(exclude)
        source = session.room_id
        planned = len(self.plan.get(source, []))
        # Keep every planned room observed, even if it currently has no humans.
        minimum = max(1, planned - 1) if planned else 0
        if physical.get(source, 0) - outgoing.get(source, 0) - 1 < minimum:
            raise TravelDenied('minimum')
        if planned == 1 and self.world.has_humans(source) and session.bot.room == source:
            raise TravelDenied('minimum')
        # Visitors cannot consume the slots reserved for absent residents' return.
        residents = self.plan.get(target, [])
        def projected_room(resident):
            pending = self.reservations.get(resident.index)
            return pending.target if pending is not None and pending is not exclude else resident.room_id
        absent = sum(projected_room(s) != target or not self.online(s) for s in residents if s is not session)
        cap = min(room.bot_cap, len(residents) + 2)
        if physical.get(target, 0) + incoming.get(target, 0) + absent + 1 > cap:
            raise TravelDenied('capacity')
        return homecoming

    def reserve(self, session, target, requester=None, kind='wander', seconds=None):
        self.update()
        homecoming = self._check(session, int(target), requester, kind)
        duration = self.settings.visit_seconds if seconds is None else seconds
        if not homecoming:
            duration = min(duration, self.bot_credit[session.index], self.room_credit[session.bot.room], 900)
            if duration < min(30, self.settings.visit_seconds):
                raise TravelDenied('budget')
        ticket = Ticket(session, session.room_id, int(target), session.conn, duration,
                        requester, kind, self.clock() + 60)
        self.reservations[session.index] = ticket  # no await between policy check and reservation
        return ticket

    def offer(self, session, action, requester):
        if action == 'follow':
            target = self.world.human_room(requester)
            if target is None:
                raise TravelDenied('unknown')
            if target == session.room_id:
                # A following agreement can start before the human takes a door.
                if session.bot.card is None or session.bot.card.wander_home_share >= 1:
                    raise TravelDenied('fixed')
                self.update()
                seconds = min(self.settings.follow_seconds, self.bot_credit[session.index],
                              self.room_credit[session.bot.room])
                if seconds < min(30, self.settings.visit_seconds):
                    raise TravelDenied('budget')
                if self.world.conversation_locked(session.player_id, requester):
                    raise TravelDenied('conversation')
                self.leases[session.index] = Lease(self.clock() + seconds, requester)
                return None
        elif re.fullmatch(r'go:[1-9][0-9]{0,3}', str(action)):
            target = int(action.split(':')[1])
        else:
            raise TravelDenied('unknown')
        return self.reserve(session, target, requester, 'invitation', self.settings.follow_seconds)

    def start(self, ticket):
        if ticket is None:
            return
        key = ticket.session.index
        if key not in self.jobs or self.jobs[key].done():
            task = asyncio.create_task(self.perform(ticket), name='penguin-travel')
            self.jobs[key] = task
            task.add_done_callback(lambda done: self.jobs.pop(key, None) if self.jobs.get(key) is done else None)

    async def perform(self, ticket):
        session = ticket.session
        entered = False
        try:
            async with session.body_lock:
                if self.clock() >= ticket.deadline or session.conn is not ticket.connection:
                    raise TravelDenied('expired')
                self._check(session, ticket.target, ticket.requester, ticket.kind, ticket)
                session.traveling = True
                source = self.config.catalog.require(ticket.source)
                life = session.behavior.life
                position = life.position if life else session.bot.spawn
                exits = [e for e in source.exits if e['to'] == ticket.target]
                if position is not None and exits:
                    exit_point = (exits[0]['x'], exits[0]['y'])
                    if any(r.contains(*position) and r.contains(*exit_point) for r in source.walk):
                        await session.send_move(*exit_point)
                        await asyncio.sleep(math.dist(position, exit_point) / 40 + .6)
                await asyncio.sleep(self.rng.uniform(self.settings.departure_min, self.settings.departure_max))
                self._check(session, ticket.target, ticket.requester, ticket.kind, ticket)
                if session.conn is not ticket.connection:
                    raise TravelDenied('expired')
                target = self.config.catalog.require(ticket.target)
                point = session.bot.spawn if ticket.target == session.bot.room else target.spawn
                await session.change_room(ticket.target, *point)
                entered = True
                self.update()
                if life:
                    life.position = point
                if ticket.target == session.bot.room:
                    lease = self.leases.get(session.index)
                    if (ticket.kind != 'follow' or lease is None or lease.human is None
                            or self.world.human_room(lease.human) != ticket.target
                            or self.clock() >= lease.expires - self.settings.departure_max - 3):
                        self.leases.pop(session.index, None)
                else:
                    previous = self.leases.get(session.index)
                    expiry = min(previous.expires, self.clock() + ticket.seconds) if previous else self.clock() + ticket.seconds
                    human = ticket.requester if ticket.kind in ('invitation', 'follow') else None
                    ready = human is None or self.world.human_room(human) == ticket.target
                    self.leases[session.index] = Lease(expiry, human, None if ready else self.clock(), ready)
                if life:
                    try:
                        await life.settle()  # walk in off the shared entry point, like a player
                    except (as2.Disconnected, OSError):
                        pass  # the session loop reconnects; the room change itself succeeded
                return True
        except TravelDenied as error:
            await session.metrics.travel_denied(error.reason)
        except (RoomChangeError, as2.Disconnected, OSError):
            pass  # change_room already records bounded wire failures; retry the lease later
        finally:
            session.traveling = False
            if self.reservations.get(session.index) is ticket:
                self.reservations.pop(session.index, None)
            if not entered and session.room_id == session.bot.room:
                self.leases.pop(session.index, None)
        return False

    def destination(self, life, now):
        session, card = life.session, life.card
        if session.room_id != session.bot.room or session.index in self.leases:
            return None
        minute = now.hour * 60 + now.minute
        for index, block in enumerate(card.schedule):
            if block.room != session.bot.room and block.covers(minute):
                if index not in life._choices:
                    life._choices[index] = life.rng.random() < block.chance
                if life._choices[index]:
                    return block.room
        if life.needs['hunger'] > .85 and 330 in card.wander_favorites:
            return 330
        if life.rng.random() >= self.settings.wander_chance:
            return None
        friends = [s.room_id for s in self.sessions if self.online(s)
                   and s.room_id != session.bot.room
                   and any(text.mentions(row['with'], s.bot.mention_names) for row in card.relationships)]
        choices = friends if life.needs['social'] > .8 and friends else list(card.wander_favorites)
        return life.rng.choice(choices) if choices else None

    async def consider(self, life, now):
        if not self.settings.autonomous:
            return
        if self.director.events and self.director.events.pinned(life.session, now):
            return
        if life.session.index in self.jobs or life.session.index in self.reservations:
            return
        target = self.destination(life, now)
        if target is None:
            return
        try:
            self.start(self.reserve(life.session, target))
        except TravelDenied:
            pass

    async def tick(self):
        self.update()
        for session in self.sessions:
            lease = self.leases.get(session.index)
            job = self.jobs.get(session.index)
            # perform() often finishes inside the dialogue drain. Its done-callback
            # is only call_soon'd, and awaiting an already-finished task does not
            # yield, so the callback has not popped the job yet. A finished trip
            # is not still in flight; leaving it here skips the walk home.
            if job is not None and job.done():
                self.jobs.pop(session.index, None)
                job = None
            if lease is None or job is not None or session.index in self.reservations:
                continue
            if not self.online(session):
                self.leases.pop(session.index, None)
                continue
            expired = self.clock() >= lease.expires - self.settings.departure_max - 3
            target = session.bot.room if expired else None
            preparing = (self.director.events and self.director.events.pinned(session)
                         and lease.human is None and not self.world.conversation_locked(session.player_id))
            if preparing:
                target = session.bot.room
            if not expired and lease.human is not None:
                target = self.world.human_room(lease.human)
                if not lease.ready:
                    if target == session.room_id:
                        lease.ready, lease.lost_since = True, None
                    elif self.clock() - lease.lost_since < 8:
                        target = None  # give the invited human time to take the door
                    else:
                        target = session.bot.room
                elif target is None:
                    if lease.lost_since is None:
                        lease.lost_since = self.clock()
                    if self.clock() - lease.lost_since >= 8:
                        target = session.bot.room  # never guess an unobserved destination
                else:
                    lease.lost_since = None
            if target == session.room_id:
                if expired:
                    self.leases.pop(session.index, None)
                continue
            if target is not None:
                try:
                    self.start(self.reserve(session, target, lease.human, 'follow', max(0, lease.expires - self.clock())))
                except TravelDenied:
                    pass

    def start_scheduler(self):
        if self.scheduler is None:
            self.scheduler = asyncio.create_task(self._loop(), name='travel-leases')

    async def _loop(self):
        while not self.closed:
            await self.tick()
            await asyncio.sleep(self.settings.tick_seconds)

    async def close(self):
        self.closed = True
        tasks = list(self.jobs.values()) + ([self.scheduler] if self.scheduler else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.reservations.clear()
        self.leases.clear()

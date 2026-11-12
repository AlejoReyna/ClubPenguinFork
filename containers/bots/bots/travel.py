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

from . import as2, igloo_invite, spend, text
from .cast import may_enter_igloo
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
    if re.search(r'\biglu\b|\bigloo\b|\b(?:tu|mi) casa\b|\bcasa mia\b', folded):
        if re.search(r'\b(?:mi casa|casa mia)\b', folded):
            return 'igloo:mine'
        return 'igloo:yours'
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
               'busy', 'minimum', 'capacity', 'budget', 'already', 'expired', 'role')

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
    owner_id: object = None
    invite_id: object = None
    reason: object = None


@dataclass
class Lease:
    expires: float
    human: object = None
    lost_since: object = None
    ready: bool = True
    hold: bool = False
    invite_id: object = None


class Travel:
    def __init__(self, director, clock=time.monotonic, rng=None):
        self.director = director
        self.config, self.world = director.config, director.world
        self.settings = self.config.life
        self.sessions = director.sessions
        self.clock, self.rng = clock, rng or random.Random(self.config.service.life_seed)
        self.reservations, self.leases, self.jobs = {}, {}, {}
        self._recent_pairs = {}  # (a, b) -> monotonic expiry; a decline or a visit
        self._invite_seq = 0
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
        igloo = kind == 'igloo'
        if igloo and session.bot.role in ('helper', 'guide'):
            raise TravelDenied('role')
        card = session.bot.card
        if card is None or card.wander_home_share >= 1:
            raise TravelDenied('fixed')
        if igloo and not may_enter_igloo(session.bot):
            raise TravelDenied('fixed')
        room = None if igloo else self.config.catalog.get(target)
        if not igloo and (room is None or room.spawn is None or not room.spawn_source):
            raise TravelDenied('unknown')
        if session.room_id == target:
            raise TravelDenied('already')
        homecoming = target == session.bot.room
        if not homecoming and self.director.events and self.director.events.pinned(session):
            raise TravelDenied('busy')
        party = kind == 'party' and target in (120, 121) and card.chattiness >= .5
        if not igloo and not homecoming and not party and target not in self._allowed_rooms(session):
            raise TravelDenied('personality')
        if session.index in self.reservations and self.reservations[session.index] is not exclude:
            raise TravelDenied('busy')
        state = self.director.rooms.get(session.room_id)
        # The dialogue turn that is carrying this invitation is not a second commitment.
        own_turn = 1 if kind in ('invitation', 'igloo') else 0
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
        # An igloo is the owner plus one guest. It is not a catalog room.
        cap = 2 if igloo else min(room.bot_cap, len(residents) + 2)
        if physical.get(target, 0) + incoming.get(target, 0) + (0 if igloo else absent) + 1 > cap:
            raise TravelDenied('capacity')
        return homecoming

    def reserve(self, session, target, requester=None, kind='wander', seconds=None, owner_id=None,
                invite_id=None, reason=None):
        self.update()
        homecoming = self._check(session, int(target), requester, kind)
        duration = self.settings.visit_seconds if seconds is None else seconds
        if not homecoming:
            duration = min(duration, self.bot_credit[session.index], self.room_credit[session.bot.room], 900)
            if duration < min(30, self.settings.visit_seconds):
                raise TravelDenied('budget')
        ticket = Ticket(session, session.room_id, int(target), session.conn, duration,
                        requester, kind, self.clock() + 60, owner_id, invite_id, reason)
        self.reservations[session.index] = ticket  # no await between policy check and reservation
        return ticket

    def reserve_igloo(self, session, owner_id=None, invite_id=None, reason=None, requester=None):
        """Reserve a walk into an igloo. ``owner_id`` defaults to this penguin.

        ``requester`` set means the host waits in the igloo for that human
        instead of chasing them. The decision to call this is ``igloo_invite``.
        """
        if session.player_id is None and owner_id is None:
            raise TravelDenied('offline')
        owner_id = int(session.player_id if owner_id is None else owner_id)
        return self.reserve(session, as2.igloo_external_id(owner_id), requester, 'igloo',
                            owner_id=owner_id, invite_id=invite_id, reason=reason)

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
                source = self.config.catalog.get(ticket.source) if self.config.catalog else None
                life = session.behavior.life
                position = life.position if life else session.bot.spawn
                exits = [e for e in source.exits if e['to'] == ticket.target] if source else []
                if position is not None and exits:
                    exit_point = (exits[0]['x'], exits[0]['y'])
                    if any(r.contains(*position) and r.contains(*exit_point) for r in source.walk):
                        await session.send_move(*exit_point)
                        await asyncio.sleep(math.dist(position, exit_point) / 40 + .6)
                await asyncio.sleep(self.rng.uniform(self.settings.departure_min, self.settings.departure_max))
                self._check(session, ticket.target, ticket.requester, ticket.kind, ticket)
                if session.conn is not ticket.connection:
                    raise TravelDenied('expired')
                if ticket.kind == 'igloo':
                    point = as2.IGLOO_POST
                    opened = False
                    try:
                        if ticket.owner_id == session.player_id:
                            await session.open_igloo()
                            opened = True
                        await session.join_igloo(ticket.owner_id)
                    except (RoomChangeError, as2.Disconnected, OSError):
                        if opened:
                            try:
                                await session.close_igloo()
                            except (as2.Disconnected, OSError):
                                session.igloo_opened = False
                        raise
                else:
                    target = self.config.catalog.require(ticket.target)
                    point = session.bot.spawn if ticket.target == session.bot.room else target.spawn
                    await session.change_room(ticket.target, *point)
                    if session.igloo_opened:
                        try:
                            await session.close_igloo()
                        except (as2.Disconnected, OSError):
                            session.igloo_opened = False
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
                    waiting = ticket.kind == 'igloo' and ticket.requester is not None
                    human = ticket.requester if ticket.kind in ('invitation', 'follow') or waiting else None
                    ready = True if ticket.kind == 'igloo' else (
                        human is None or self.world.human_room(human) == ticket.target)
                    self.leases[session.index] = Lease(
                        expiry, human, None if ready else self.clock(), ready,
                        ticket.kind == 'igloo', ticket.invite_id)
                self._emit_travel(session, ticket)
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

    def _emit_travel(self, session, ticket):
        from .dialog_log import emit
        if not session.bot.username:
            return
        emit(room_id=int(ticket.source), speaker=session.bot.username, kind='travel',
             target_kind='room', source='local', invite_id=ticket.invite_id,
             reason=ticket.reason, dest_room_id=int(ticket.target))

    def _pair(self, left, right):
        return tuple(sorted((str(left), str(right))))

    def remember_pair(self, left, right):
        self._recent_pairs[self._pair(left, right)] = self.clock() + igloo_invite.PAIR_SECONDS
        if len(self._recent_pairs) > 64:
            oldest = min(self._recent_pairs, key=self._recent_pairs.get)
            self._recent_pairs.pop(oldest, None)

    def pair_is_recent(self, left, right):
        key = self._pair(left, right)
        expiry = self._recent_pairs.get(key)
        if expiry is None:
            return False
        if self.clock() >= expiry:
            self._recent_pairs.pop(key, None)
            return False
        return True

    def _invite_id(self):
        self._invite_seq += 1
        return f'ig{self._invite_seq}'

    def _guest_facts(self, session, now, host_key, host_names, host_is_human):
        card = session.bot.card
        life = getattr(session.behavior, 'life', None)
        needs = life.needs if life is not None else {'energy': 1.0, 'social': 0.0}
        busy = self.world.conversation_locked(session.player_id)
        partner = getattr(self.director, 'partner', None)
        if partner and partner(session) is not None:
            busy = True
        pinned = bool(self.director.events and self.director.events.pinned(session, now))
        day = now.date().isoformat() if now is not None else 'day'
        rng = igloo_invite.decision_rng(self.config.service.life_seed, session.bot.username, day, host_key)
        return igloo_invite.verdict(
            rng,
            role_blocked=not igloo_invite.eligible(session.bot),
            on_duty=igloo_invite.on_duty(card, now),
            pinned=pinned,
            busy=busy,
            energy=needs.get('energy', 1.0),
            social=needs.get('social', 0.0),
            recent=self.pair_is_recent(f'b:{session.bot.username}', host_key),
            warm=igloo_invite.warm_toward(card, host_names),
            host_is_human=host_is_human,
        )

    async def _say(self, session, line, priority, human_here):
        """One in-character line. The model writes it only with a human listening and budget left."""
        from .provider import SimulatedProvider
        brain = self.director.brains.get(session.index)
        source = 'local'
        if human_here and brain is not None and not isinstance(session.provider, SimulatedProvider):
            ledger = self.director.ledger
            if ledger is None or ledger.allows(priority):
                try:
                    raw, _usage = await session.provider.complete(
                        [{'role': 'user', 'content':
                          'Escribe una sola frase en personaje, maximo 54 caracteres, sin JSON. '
                          'La decision ya esta tomada: ' + line}],
                        priority=priority)
                    spoken = raw
                    if isinstance(raw, str) and raw.lstrip().startswith('{'):
                        import json
                        spoken = json.loads(raw).get('say') or line
                    line, source = spoken, 'model'
                except Exception:
                    source = 'fallback'
            else:
                source = 'fallback'
        if brain is None:
            return line, source
        answer = await brain.safe(line, source=source if source != 'model' else 'llm')
        if answer.source == 'llm':
            source = 'model'
        elif source == 'model':
            source = 'fallback'
        else:
            source = answer.source if answer.source in ('local', 'fallback') else source
        if answer.say and session.player_id is not None:
            await session.send_chat(session.player_id, answer.say)
        return answer.say, source

    def _emit_invite(self, speaker, kind, *, room_id, target_kind, target, player_id,
                     text, source, priority, invite_id, reason, dest_room_id):
        from .dialog_log import emit
        emit(room_id=int(room_id), speaker=speaker, kind=kind, target_kind=target_kind,
             target=target, player_id=player_id, text=text or None, source=source,
             priority=priority, invite_id=invite_id, reason=reason,
             dest_room_id=int(dest_room_id))

    async def consider_igloo(self, life, now):
        """Maybe invite someone in this room to the host's igloo. True when it tried."""
        session = life.session
        if as2.is_igloo_room(session.room_id) or session.player_id is None:
            return False
        if not igloo_invite.eligible(session.bot):
            return False
        if igloo_invite.on_duty(session.bot.card, now):
            return False
        if self.world.conversation_locked(session.player_id):
            return False
        if life.needs.get('energy', 1) < 0.2:
            return False
        if not self.world.has_humans(session.room_id):
            return False  # no bot-bot loop in an empty room, and no paid line
        room = session.room_id
        if not self.director.conversation_ready(room, (session,)):
            return False  # the 10-minute rest after the last bot-started conversation
        if life.rng.random() >= igloo_invite.HOST_CHANCE:
            return False
        guest = self._spare_guest(session)
        if guest is not None and self.director.conversation_ready(room, (guest,)):
            try:
                await self.invite_bot(session, guest, now)
            finally:
                self.director.conversation_ended(room, (session, guest))
            return True
        if guest is not None:
            return False
        humans = self.world.humans_in(room)
        try:
            await self._host_departs(session, humans[0].id, now)
        finally:
            self.director.conversation_ended(room, (session,))
        return True

    def _spare_guest(self, session):
        """Another body who can leave with the host without dropping the minimum.

        A helper still counts as someone in the room, but is never the guest.
        """
        room = session.room_id
        planned = len(self.plan.get(room, []))
        minimum = max(1, planned - 1) if planned else 0
        if planned == 1 and self.world.has_humans(room) and session.bot.room == room:
            return None
        present = sum(1 for other in self.sessions if self.online(other) and other.room_id == room)
        if present - 2 < minimum:
            return None
        for other in self.sessions:
            if other is session or not self.online(other) or other.room_id != room:
                continue
            if igloo_invite.eligible(other.bot):
                return other
        return None

    async def answer_human_igloo(self, session, human_id, whose, now=None):
        """A human asked this bot into an igloo. The bot decides; the model does not."""
        life = getattr(session.behavior, 'life', None)
        now = now or (life._last if life is not None and life._last is not None else self.director.utcnow())
        if whose == 'mine':
            return await self._invite_to(session, now, owner_id=int(human_id), human_id=int(human_id),
                                         host_is_human=True, host_key=f'p:{int(human_id)}', host_names=())
        return await self._invite_to(session, now, owner_id=session.player_id, human_id=int(human_id),
                                     host_is_human=True, host_key=f'p:{int(human_id)}',
                                     host_names=session.bot.mention_names)

    async def _host_departs(self, session, human_id, now):
        """The host already decided. Open the igloo and wait. The human decides in the client."""
        invite_id = self._invite_id()
        dest = as2.igloo_external_id(session.player_id)
        self._emit_invite(session.bot.username, 'invite', room_id=session.room_id,
                          target_kind='player', target=None, player_id=int(human_id),
                          text=None, source='local', priority=None, invite_id=invite_id,
                          reason=None, dest_room_id=dest)
        denied = await self._depart(session, None, session.player_id, int(human_id), invite_id, 'seeded_roll')
        if not denied:
            return
        line = (session.bot.card.fallbacks[0] if session.bot.card and session.bot.card.fallbacks
                else 'Ahora no puedo.')
        said, source = await self._say(session, line, spend.P2, True)
        self._emit_invite(session.bot.username, 'decline', room_id=session.room_id,
                          target_kind='player', target=None, player_id=int(human_id),
                          text=said, source=source, priority=spend.P2, invite_id=invite_id,
                          reason=self._travel_reason(denied), dest_room_id=dest)

    async def invite_bot(self, session, guest, now):
        """Host ``session`` invites ``guest`` while a human is in the room. Tests and the cycle."""
        if not self.world.has_humans(session.room_id):
            return None
        return await self._invite_to(session, now, owner_id=session.player_id, guest=guest,
                                     human_id=None, host_is_human=False,
                                     host_key=f'b:{session.bot.username}',
                                     host_names=session.bot.mention_names)

    async def _invite_to(self, session, now, *, owner_id, human_id, host_is_human, host_key,
                         host_names, guest=None, deciding=None):
        """Run one invitation. ``deciding`` defaults to ``guest`` or ``session``."""
        deciding = deciding or guest or session
        invite_id = self._invite_id()
        dest = as2.igloo_external_id(owner_id)
        human_here = self.world.has_humans(session.room_id) or human_id is not None
        priority = spend.P0 if host_is_human else spend.P2
        decision, reason = self._guest_facts(deciding, now, host_key, host_names, host_is_human)
        pair_left = f'b:{deciding.bot.username}'
        if decision == 'skip':
            return decision
        if guest is not None:
            # The host asked. The guest's accept or decline uses the same invite_id.
            self._emit_invite(session.bot.username, 'invite', room_id=session.room_id,
                              target_kind='bot', target=guest.bot.username, player_id=None,
                              text=None, source='local', priority=None, invite_id=invite_id,
                              reason=None, dest_room_id=dest)
        if decision == 'decline':
            line = (deciding.bot.card.fallbacks[0] if deciding.bot.card and deciding.bot.card.fallbacks
                    else 'Ahora no puedo.')
            said, source = await self._say(deciding, line, priority, human_here)
            self._emit_invite(deciding.bot.username, 'decline', room_id=deciding.room_id,
                              target_kind='player' if human_id and guest is None else 'bot',
                              target=None if human_id and guest is None else (
                                  session.bot.username if deciding is not session else (
                                      guest.bot.username if guest else None)),
                              player_id=human_id, text=said, source=source, priority=priority,
                              invite_id=invite_id, reason=reason, dest_room_id=dest)
            self.remember_pair(pair_left, host_key)
            return decision
        # accept
        line = (deciding.bot.card.greetings[0] if deciding.bot.card and deciding.bot.card.greetings
                else 'Vamos.')
        said, source = await self._say(deciding, line, priority, human_here)
        target_kind = 'player' if guest is None else 'bot'
        target = None if guest is None else (
            guest.bot.username if deciding is session else session.bot.username)
        if guest is None:
            self._emit_invite(session.bot.username, 'invite', room_id=session.room_id,
                              target_kind='player', target=None, player_id=human_id,
                              text=None, source='local', priority=None, invite_id=invite_id,
                              reason=None, dest_room_id=dest)
        denied = await self._depart(session, guest, owner_id, human_id, invite_id, reason)
        if denied:
            self._emit_invite(deciding.bot.username, 'decline', room_id=deciding.room_id,
                              target_kind=target_kind, target=target, player_id=human_id,
                              text=None, source='local', priority=None, invite_id=invite_id,
                              reason=self._travel_reason(denied), dest_room_id=dest)
            return 'decline'
        self._emit_invite(deciding.bot.username, 'accept', room_id=deciding.room_id,
                          target_kind=target_kind, target=target, player_id=human_id,
                          text=said, source=source, priority=priority, invite_id=invite_id,
                          reason=reason, dest_room_id=dest)
        self.remember_pair(pair_left, host_key)
        return decision

    async def _depart(self, host, guest, owner_id, human_id, invite_id, reason):
        tickets = []
        try:
            tickets.append(self.reserve_igloo(
                host, owner_id, invite_id=invite_id, reason=reason,
                requester=human_id if guest is None else None))
            if guest is not None:
                tickets.append(self.reserve_igloo(
                    guest, owner_id, invite_id=invite_id, reason=reason))
        except TravelDenied as error:
            for ticket in tickets:
                if self.reservations.get(ticket.session.index) is ticket:
                    self.reservations.pop(ticket.session.index, None)
            return error.reason
        for ticket in tickets:
            self.start(ticket)
        return None

    @staticmethod
    def _travel_reason(reason):
        """Map a reservation refusal onto the dialog-log vocabulary. Unknown stays null."""
        if reason in ('minimum', 'capacity', 'budget'):
            return 'room_min_guard'
        if reason in ('conversation', 'busy'):
            return 'busy_with_human'
        return None

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
        if await self.consider_igloo(life, now):
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
            if getattr(lease, 'hold', False) and not expired:
                # Stay in the igloo. Do not chase the guest around the island.
                guest_here = (lease.human is None
                              or self.world.human_room(lease.human) == session.room_id)
                if guest_here:
                    lease.lost_since = None
                    target = session.room_id
                elif lease.lost_since is None:
                    lease.lost_since = self.clock()
                    target = session.room_id
                elif self.clock() - lease.lost_since < 8:
                    target = session.room_id
                else:
                    expired = True
                    target = session.bot.room
            elif not expired and lease.human is not None:
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
                    if session.igloo_opened:
                        try:
                            await session.close_igloo()
                        except (as2.Disconnected, OSError):
                            session.igloo_opened = False
                continue
            if target is not None:
                try:
                    self.start(self.reserve(
                        session, target, lease.human, 'follow', max(0, lease.expires - self.clock()),
                        invite_id=lease.invite_id, reason='lease_expired' if expired else None))
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
        for session in self.sessions:
            if getattr(session, 'igloo_opened', False):
                try:
                    await session.close_igloo()
                except (as2.Disconnected, OSError):
                    session.igloo_opened = False
        self.jobs.clear()
        self.reservations.clear()
        self.leases.clear()

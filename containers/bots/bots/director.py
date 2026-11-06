"""One dialogue owner per observed room, independent of socket readers and bodies.

Only enabled explicitly in Phase 2. Queues, deduplication and numeric-player
maps are bounded. Optional Phase 3 travel shares this owner's reservations;
chat and social memory are never persisted here.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
import datetime
import hashlib
import logging
import math
import random
import re
import time

from . import metrics, spend, text
from .brain import Brain, asks_about_ai
from .provider import SimulatedProvider

logger = logging.getLogger('bots.director')
_CACHE_LIMIT = 4096
_ROOM_NAMES = {100: 'Pueblo', 110: 'Cafeteria', 111: 'Biblioteca', 120: 'Disco',
               121: 'Lounge', 230: 'Ski Hill', 300: 'Plaza', 310: 'Tienda de mascotas',
               320: 'Dojo', 330: 'Pizzeria', 340: 'Teatro', 800: 'Muelle',
               801: 'Fuertes de nieve', 802: 'Estadio'}


@dataclass
class Turn:
    room: int
    sessions: tuple
    connections: tuple
    sender: object = None
    message: str = ''
    kind: str = 'reply'
    model: bool = True
    priority: str = spend.P0
    local: object = None
    event: object = None
    lines: object = None


class RoomQueue:
    def __init__(self, maximum):
        self.queue = asyncio.PriorityQueue(maxsize=maximum)
        self.worker = None
        self.current = None
        self.last_sent = float('-inf')
        self.pending = {}
        self.human_pending = 0

    def idle(self):
        return self.current is None and self.queue.empty()


class Director:
    def __init__(self, config, sessions, world, rng=None, clock=time.monotonic, utcnow=None, ledger=None):
        self.config, self.sessions, self.world = config, list(sessions), world
        self.settings = config.dialogue
        self.ledger = ledger
        self.rng = rng or random.Random(config.service.life_seed)
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.brains = {s.index: Brain(s, self.rng) for s in self.sessions}
        self.rooms = {}  # bounded by the catalog
        self.seen = OrderedDict()  # hashes of recently seen frames, never a chat log
        self.continuity = OrderedDict()  # (room, human id) -> (session index, time)
        self.greeted = OrderedDict()
        self.greetings = {}
        self.daily_day, self.daily = None, {}
        self._sequence = 0
        self._ambient = None
        self._closed = False
        self._snowball_until = 0.0
        self.travel = None
        self.events = None
        if config.life.enabled:
            from .travel import Travel
            from .events import Events
            self.travel = Travel(self)
            self.events = Events(self) if config.life.events else None
            for session in self.sessions:
                session.travel = self.travel

    def start(self):
        if self.travel:
            self.travel.start_scheduler()
        if self.events:
            self.events.start()
        if self._ambient is None and not self._closed:
            self._ambient = asyncio.create_task(self._ambient_loop(), name='dialogue-ambient')

    def active(self, room):
        return [s for s in self.sessions if s.room_id == room and s.joined_room
                and s.conn is not None and not s.conn.closed and not s.behavior.stopped
                and not s.traveling]

    def is_human(self, room, player_id):
        return (not self.world.is_bot(player_id)
                and any(m.id == player_id for m in self.world.humans_in(room)))

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _CACHE_LIMIT:
            cache.popitem(last=False)

    def _fresh_frame(self, room, sender, message):
        now = self.clock()
        while self.seen and next(iter(self.seen.values())) <= now - 1.5:
            self.seen.popitem(last=False)
        key = (room, sender, hashlib.blake2s(message.encode('utf-8'), digest_size=12).digest())
        if key in self.seen:
            return False
        self._remember(self.seen, key, now)
        return True

    def lookup_target(self, message):
        question = re.search(r'\bdonde\s+(?:esta|anda|se encuentra)\b', text.fold(message))
        if not question:
            return None
        subject = text.fold(message)[question.end():]
        return next((s for s in self.sessions if text.mentions(subject, s.bot.mention_names)), None)

    def knows(self, speaker, target, room):
        if speaker.bot.username == 'pacopregon' or target.room_id == room:
            return True
        card = speaker.bot.card
        return bool(card and any(text.mentions(relation['with'], target.bot.mention_names)
                                 for relation in card.relationships))

    def _named(self, message, exclude=None):
        matches = []
        folded = text.fold(message)
        for session in self.sessions:
            if session is exclude:
                continue
            for name in session.bot.mention_names:
                match = re.search(r'(?<!\w)' + re.escape(text.fold(name)) + r'(?!\w)', folded)
                if match:
                    matches.append((match.start(), -len(name), session.index, session))
        return min(matches, key=lambda item: item[:3])[3] if matches else None

    def choose(self, room, sender, message):
        candidates = self.active(room)
        target = self.lookup_target(message)
        named = self._named(message, exclude=target)
        if named is not None:
            return named if named in candidates else None
        if target:
            informed = [s for s in candidates if self.knows(s, target, room)]
            return min(informed, key=lambda s: (s.bot.username != 'pacopregon', s.index)) if informed else None
        # A normal explicit mention includes the selected subject itself.
        named = self._named(message)
        if named is not None:
            return named if named in candidates else None
        folded = text.fold(message)
        probability = self.settings.other_chance
        if re.search(r'\b(?:hola|buenas|hey|ey|saludos)\b', folded):
            probability = self.settings.hello_chance
        elif '?' in message or '¿' in message or re.search(r'\b(?:que|como|donde|cuando|quien|por que)\b', folded):
            probability = self.settings.question_chance
        if not asks_about_ai(message) and self.rng.random() >= probability:
            return None
        last = self.continuity.get((room, sender))
        if last and self.clock() - last[1] < self.settings.continuity_ttl:
            previous = next((s for s in candidates if s.index == last[0]), None)
            if previous:
                return previous
        members = {m.id: m for m in self.world.members(room)}
        human = members.get(sender)
        state = self.rooms.get(room)

        def rank(session):
            card = session.bot.card
            score = sum(text.mentions(message, [topic]) for topic in card.topics) if card else 0
            member = members.get(session.player_id)
            distance = math.inf
            if human and member and None not in (human.x, human.y, member.x, member.y):
                distance = (human.x - member.x) ** 2 + (human.y - member.y) ** 2
            busy = state.pending.get(session.index, 0) if state else 0
            return -score, distance, busy, session.index

        return min(candidates, key=rank) if candidates else None

    async def on_chat(self, observer, sender, message):
        room = observer.room_id
        if (self._closed or room not in self.config.catalog.rooms or not self.is_human(room, sender)
                or not self._fresh_frame(room, sender, str(message))):
            return
        pending = self.greetings.pop((room, sender), None)
        if pending:
            pending.cancel()  # a speaking human gets a reply, not a delayed second hello
        speaker = self.choose(room, sender, str(message))
        if speaker is None:
            return
        allowed, reason = speaker.gate.try_reserve(speaker.index, sender)
        if not allowed:
            await speaker.metrics.skipped(speaker.index, reason)
            return
        target = self.lookup_target(message)
        local = None
        if target:
            location = self.world.room_of(target.player_id) if target.player_id is not None else None
            if self.knows(speaker, target, room) and location in _ROOM_NAMES:
                local = f'{target.bot.nickname} esta en {_ROOM_NAMES[location]}.'
            else:
                local = f'Ahora no se donde esta {target.bot.nickname}.'
        if not self._enqueue(Turn(room, (speaker,), (speaker.conn,), sender, str(message)[:300], local=local)):
            await speaker.metrics.skipped(speaker.index, metrics.SKIP_DIALOGUE_QUEUE)

    def member_added(self, room, player_id):
        if self._closed or room not in self.config.catalog.rooms or not self.is_human(room, player_id):
            return
        key, now = (room, player_id), self.clock()
        if key in self.greetings:
            return
        if now - self.greeted.get(key, float('-inf')) < self.settings.greeting_ttl:
            return
        self._remember(self.greeted, key, now)
        if len(self.greetings) >= 64 or self.rng.random() >= self.settings.greeting_chance:
            return
        task = asyncio.create_task(self._greet_after(room, player_id), name='dialogue-greeting')
        self.greetings[key] = task

    async def _greet_after(self, room, player_id):
        key = (room, player_id)
        try:
            await asyncio.sleep(self.rng.uniform(self.settings.greeting_delay_min, self.settings.greeting_delay_max))
            candidates = self.active(room)
            if self._closed or not candidates or not self.is_human(room, player_id):
                return
            speaker = self.rng.choice(candidates)
            self._enqueue(Turn(room, (speaker,), (speaker.conn,), player_id,
                               kind='greeting', priority=spend.P1,
                               model=self.rng.random() < self.settings.greeting_model_share))
        finally:
            if self.greetings.get(key) is asyncio.current_task():
                self.greetings.pop(key, None)

    def presence_changed(self, room):
        if not self.world.has_humans(room):
            state = self.rooms.get(room)
            if state and state.current:
                state.current.cancel()  # cancels an HTTP/semaphore wait as well

    async def snowball(self, observer, sender, x, y):
        if (not self.travel or not self.is_human(observer.room_id, sender)
                or not self._fresh_frame(observer.room_id, sender, f'snow:{x}:{y}')):
            return
        bolazo = next((s for s in self.active(observer.room_id) if s.bot.username == 'bolazo'), None)
        members = {m.id: m for m in self.world.members(observer.room_id)}
        target = members.get(sender)
        body = members.get(bolazo.player_id) if bolazo else None
        if (body is None or target is None or None in (body.x, body.y, target.x, target.y)
                or (body.x - x) ** 2 + (body.y - y) ** 2 > 40 ** 2
                or not (0 <= target.x <= 1520 and 0 <= target.y <= 960)
                or self.clock() < self._snowball_until):
            return
        self._snowball_until = self.clock() + 2
        await bolazo.send_snowball(target.x, target.y)

    def _enqueue(self, turn):
        if self._closed:
            return False
        state = self.rooms.setdefault(turn.room, RoomQueue(self.settings.queue_size))
        self._sequence += 1
        try:
            state.queue.put_nowait((spend.PRIORITIES.index(turn.priority), self._sequence, turn))
        except asyncio.QueueFull:
            return False
        for session in turn.sessions:
            state.pending[session.index] = state.pending.get(session.index, 0) + 1
        if turn.priority == spend.P0:
            state.human_pending += 1
        if state.worker is None:
            state.worker = asyncio.create_task(self._worker(turn.room, state), name='dialogue-room')
        return True

    def _valid(self, turn):
        return (not self._closed and self.world.has_humans(turn.room)
                and (turn.sender is None or self.is_human(turn.room, turn.sender))
                and all(s in self.active(turn.room) and s.conn is conn
                        for s, conn in zip(turn.sessions, turn.connections)))

    async def _worker(self, room, state):
        while not self._closed:
            _priority, _sequence, turn = await state.queue.get()
            if turn.priority == spend.P0:
                state.human_pending -= 1
            try:
                wait = state.last_sent + self.settings.room_gap - self.clock()
                if wait > 0:
                    await asyncio.sleep(wait)
                if self._valid(turn):
                    state.current = asyncio.create_task(self._perform(turn, state))
                    try:
                        await state.current
                    except asyncio.CancelledError:
                        if self._closed:
                            raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error('room dialogue failed (%s)', type(error).__name__)
            finally:
                state.current = None
                for session in turn.sessions:
                    state.pending[session.index] = max(0, state.pending.get(session.index, 1) - 1)
                state.queue.task_done()

    def _model_slot(self, sender):
        if self.ledger is not None:
            return self.ledger.reserve_player(sender, self.settings.player_daily_limit)
        day = self.utcnow().date()
        if self.daily_day is None or day > self.daily_day:
            self.daily_day, self.daily = day, {}
        if sender not in self.daily and len(self.daily) >= _CACHE_LIMIT:
            return None  # fail closed instead of evicting an active daily quota
        count = self.daily.get(sender, 0)
        if count >= self.settings.player_daily_limit:
            return None
        self.daily[sender] = count + 1
        return self.daily_day, sender

    def _release_slot(self, token):
        if self.ledger is not None:
            self.ledger.release_player(token)
            return
        day, sender = token
        if day == self.daily_day:
            self.daily[sender] = max(0, self.daily.get(sender, 0) - 1)

    async def _perform(self, turn, state):
        if turn.kind == 'event':
            kind, _key, minimum, maximum = turn.event
            self.events.current[turn.room] = kind
            try:
                if turn.lines is None:
                    allowed = await self.events.model_allowed(turn.event, turn.sessions[0])
                    if allowed is False:
                        return
                    brains = [self.brains[s.index] for s in turn.sessions]
                    lines = await brains[0].script(brains[1:], kind, minimum, maximum, model=allowed is True)
                else:
                    lines = turn.lines
                for index, (brain, answer) in enumerate(lines):
                    if not self._valid(turn):
                        break
                    if state.human_pending:
                        self._enqueue(replace(turn, lines=lines[index:]))
                        break
                    if index:
                        await asyncio.sleep(self.rng.uniform(self.settings.line_delay_min, self.settings.line_delay_max))
                    if self._valid(turn) and await brain.publish(answer, spend.P1):
                        state.last_sent = self.clock()
            finally:
                self.events.current.pop(turn.room, None)
            return
        if turn.kind == 'scene':
            brains = [self.brains[s.index] for s in turn.sessions]
            lines = await brains[0].scene(brains[1:])
            for index, (brain, answer) in enumerate(lines):
                # Human turns queued during inference take precedence over an ambient scene.
                if not self._valid(turn) or state.human_pending:
                    break
                if index:
                    await asyncio.sleep(self.rng.uniform(self.settings.line_delay_min, self.settings.line_delay_max))
                if self._valid(turn) and await brain.publish(answer, spend.P2):
                    state.last_sent = self.clock()
            return
        session = turn.sessions[0]
        brain = self.brains[session.index]
        if self.travel and turn.kind == 'reply':
            from .travel import invitation, TravelDenied, ROOM_ALIASES
            action = invitation(turn.message, self.config.catalog)
            if action and self._named(turn.message) is session:
                try:
                    ticket = self.travel.offer(session, action, turn.sender)
                    destination = ROOM_ALIASES.get(ticket.target, ('otra sala',))[0] if ticket else None
                    line = f'Intentare ir a {destination} contigo.' if ticket else 'Te sigo un ratito, si hay sitio.'
                except TravelDenied as error:
                    await session.metrics.travel_denied(error.reason)
                    ticket, line = None, 'Ahora me quedo aqui para cuidar mi puesto.'
                answer = await brain.safe(line)
                if self._valid(turn) and await brain.publish(answer, spend.P0):
                    state.last_sent = self.clock()
                    self.world.talked(session.player_id, turn.sender, self.config.life.conversation_seconds)
                    self.travel.start(ticket)
                elif ticket:
                    self.travel.reservations.pop(session.index, None)
                return
        previous = self.continuity.get((turn.room, turn.sender))
        if turn.kind == 'greeting' and previous and self.clock() - previous[1] < self.settings.continuity_ttl:
            return
        if turn.kind == 'reply' and await session.behavior._music(
                turn.sender, turn.message, valid=lambda: self._valid(turn)):
            state.last_sent = self.clock()
            if self.travel:
                self.world.talked(session.player_id, turn.sender, self.config.life.conversation_seconds)
            return
        slot, keep_slot, travel_ticket = None, False, None
        try:
            if turn.local is not None:
                answer = await brain.safe(turn.local)
            else:
                model = turn.model
                if model and not isinstance(session.provider, SimulatedProvider) and not asks_about_ai(turn.message):
                    try:
                        slot = self._model_slot(turn.sender)
                    except spend.SpendDenied:
                        await session.metrics.spend_denied('state')
                        await session.metrics.ledger_state(False)
                    model = slot is not None
                answer = await brain.reply(turn.sender, turn.message, priority=turn.priority,
                                           model=model, greeting=turn.kind == 'greeting')
            if self.travel and isinstance(answer.action, str) and (answer.action == 'follow' or answer.action.startswith('go:')):
                from .travel import TravelDenied
                try:
                    travel_ticket = self.travel.offer(session, answer.action, turn.sender)
                    answer = await brain.safe('Intentare acompanarte, si hay sitio.')
                except TravelDenied as error:
                    await session.metrics.travel_denied(error.reason)
                    answer = await brain.safe('Ahora me quedo aqui para cuidar mi puesto.')
            keep_slot = bool(answer.source == 'llm' and answer.say and self._valid(turn))
        finally:
            if slot is not None and not keep_slot:
                # Local fallbacks, filtered text, cancellation and local budget
                # refusals are not one of the human's 40 model responses. Paid
                # failed attempts still remain in the separate USD ledger.
                self._release_slot(slot)
        if not self._valid(turn):
            if travel_ticket:
                self.travel.reservations.pop(session.index, None)
            return
        if await brain.publish(answer, turn.priority):
            state.last_sent = self.clock()
            if self.travel and turn.sender is not None:
                self.world.talked(session.player_id, turn.sender, self.config.life.conversation_seconds)
            if turn.kind == 'reply':
                self._remember(self.continuity, (turn.room, turn.sender), (session.index, self.clock()))
                # Only this human's turns enter this bot's bounded short-term memory.
                content = brain.messages(turn.sender, turn.message)[-1]['content']
                brain.memory.add(turn.sender, content, answer.say)
            if travel_ticket:
                self.travel.start(travel_ticket)

    def event(self, kind, room, key, speakers, minimum, maximum):
        if self.events is None or not speakers or not self.world.has_humans(room):
            return False
        state = self.rooms.get(room)
        if state and not state.idle():
            return False
        return self._enqueue(Turn(room, tuple(speakers), tuple(s.conn for s in speakers),
                                  kind='event', priority=spend.P1, event=(kind, key, minimum, maximum)))

    def scene(self, room):
        state = self.rooms.get(room)
        candidates = self.active(room)
        if (self._closed or not self.world.has_humans(room) or len(candidates) < 2
                or (state is not None and not state.idle())):
            return False
        picked = tuple(self.rng.sample(candidates, min(3, len(candidates))))
        return self._enqueue(Turn(room, picked, tuple(s.conn for s in picked),
                                  kind='scene', priority=spend.P2))

    async def _ambient_loop(self):
        due = {room: self.clock() + self.rng.uniform(self.settings.scene_min_delay, self.settings.scene_max_delay)
               for room in self.config.catalog.rooms}
        while not self._closed:
            now = self.clock()
            for room, deadline in due.items():
                if now >= deadline:
                    self.scene(room)
                    due[room] = now + self.rng.uniform(self.settings.scene_min_delay, self.settings.scene_max_delay)
            await asyncio.sleep(min(5.0, max(0.05, min(due.values()) - self.clock())))

    async def close(self):
        self._closed = True
        if self.events:
            await self.events.close()
        if self.travel:
            await self.travel.close()
        tasks = list(self.greetings.values()) + ([self._ambient] if self._ambient else [])
        for state in self.rooms.values():
            tasks.extend(task for task in (state.worker, state.current) if task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.greetings.clear()
        self.rooms.clear()
        self.seen.clear()

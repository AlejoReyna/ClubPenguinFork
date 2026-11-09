"""One dialogue owner per observed room, independent of socket readers and bodies.

Only enabled explicitly in Phase 2. Queues, deduplication and numeric-player
maps are bounded. Optional Phase 3 travel shares this owner's reservations;
short-term chat stays in RAM; optional Phase 4 notes use recall.sqlite.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field, replace
import datetime
import hashlib
import logging
import math
import random
import re
import time
import zlib

from . import actions, as2, metrics, spend, text
from .brain import Brain, asks_about_ai
from .provider import SimulatedProvider
from .recall import wants_forget

logger = logging.getLogger('bots.director')
_CACHE_LIMIT = 4096
_ENGAGED_SECONDS = 60.0   # a bot that replied this recently is "talking with" that player
_REACTION = (0.6, 1.8)    # seconds to notice a message before typing starts
_TYPING_MAX = 9.0         # nobody waits longer than this for a short chat line
_ROOM_NAMES = {100: 'Pueblo', 110: 'Cafeteria', 111: 'Biblioteca', 120: 'Disco',
               121: 'Lounge', 230: 'Ski Hill', 300: 'Plaza', 310: 'Tienda de mascotas',
               320: 'Dojo', 330: 'Pizzeria', 340: 'Teatro', 800: 'Muelle',
               801: 'Fuertes de nieve', 802: 'Estadio'}
# Chat aimed at everyone (matched on folded text): plural "you" (ustedes and vosotros
# verb forms), group vocatives and open calls such as "alguien".
_GROUP_RE = re.compile(
    r'\b(?:ustedes|vosotr[oa]s|tod[oa]s|chic[oa]s|muchach[oa]s|chav[oa]s|amig[oa]s|pinguin[oa]s'
    r'|gente|compas|panas|alguien|quienes|les|os|cada uno'
    r'|hacen|estan|andan|quieren|pueden|saben|tienen|van|vienen|juegan|piensan|opinan|creen|dicen'
    r'|haceis|estais|quereis|podeis|sabeis|teneis|vais|sois|venis|jugais)\b')


def addresses_group(message):
    """True when a human line speaks to the penguins as a group ("que hacen", "hola chicos")."""
    return bool(_GROUP_RE.search(text.fold(message)))


@dataclass
class Thread:
    """One human line answered by several penguins: when it arrived and what they said to it."""
    started: float
    group: bool = False
    lines: list = field(default_factory=list)  # (nickname, published line), bot output only


@dataclass
class Turn:
    room: int
    sessions: tuple
    connections: tuple
    sender: object = None
    message: str = ''
    kind: str = 'reply'  # reply (lead voice), chorus (extra voice), greeting, scene, event
    model: bool = True
    priority: str = spend.P0
    local: object = None
    event: object = None
    lines: object = None
    thread: object = None


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
    def __init__(self, config, sessions, world, rng=None, clock=time.monotonic, utcnow=None,
                 ledger=None, recall=None):
        self.config, self.sessions, self.world = config, list(sessions), world
        self.settings = config.dialogue
        self.ledger = ledger
        self.recall = recall
        self._summarized = {}
        self.rng = rng or random.Random(config.service.life_seed)
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.brains = {s.index: Brain(s, self.rng) for s in self.sessions}
        self.rooms = {}  # bounded by the catalog
        self.seen = OrderedDict()  # hashes of recently seen frames, never a chat log
        self.continuity = OrderedDict()  # (room, human id) -> (session index, time)
        self._threads = OrderedDict()  # (room, human id) -> Thread of that human's latest line
        self.greeted = OrderedDict()
        self.greetings = {}
        self._returns = {}  # host session index -> task walking back to its post
        # Host timing (seconds): settle before walking up, walk cap, idle time before going back, poll.
        self.host_settle, self.host_walk_cap, self.host_idle, self.host_poll = (0.8, 1.6), 4.0, 20.0, 10.0
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

    def _named_all(self, message, exclude=None):
        """Every penguin called by name, in the order named; a longer name wins an overlap."""
        matches = []
        folded = text.fold(message)
        for session in self.sessions:
            if session is exclude:
                continue
            for name in session.bot.mention_names:
                match = re.search(r'(?<!\w)' + re.escape(text.fold(name)) + r'(?!\w)', folded)
                if match:
                    matches.append((match.start(), -len(name), session.index, match.end(), session))
        named, end = [], -1
        for start, _length, _index, stop, session in sorted(matches, key=lambda item: item[:3]):
            if start >= end:
                end = stop
                if session not in named:
                    named.append(session)
        return named

    def _named(self, message, exclude=None):
        named = self._named_all(message, exclude)
        return named[0] if named else None

    def _rank(self, room, sender, message):
        """Sort key for unnamed chat: topic match, then distance to the human, then queued work."""
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
        return rank

    def choose(self, room, sender, message):
        """The lead voice for a human line, or None; ``responders`` has the whole group."""
        voices = self.responders(room, sender, message)
        return voices[0] if voices else None

    def responders(self, room, sender, message):
        """Who answers one human line, lead first, at most ``group_max`` penguins.

        Everyone named answers. Chat aimed at the group lets each penguin who hears it
        decide on its own; other unnamed chat gets one lead and, now and then, a second
        voice chiming in. Location questions keep a single informed answer.
        """
        candidates = self.active(room)
        limit = max(1, int(self.settings.group_max))
        target = self.lookup_target(message)
        if target:
            named = self._named(message, exclude=target)
            if named is not None:
                return [named] if named in candidates else []
            informed = [s for s in candidates if self.knows(s, target, room)]
            return [min(informed, key=lambda s: (s.bot.username != 'pacopregon', s.index))] if informed else []
        named = self._named_all(message)
        if named:
            return [s for s in named if s in candidates][:limit]
        folded = text.fold(message)
        group = addresses_group(message)
        probability = self.settings.other_chance
        if re.search(r'\b(?:hola|buenas|hey|ey|saludos)\b', folded):
            probability = self.settings.hello_chance
        elif '?' in message or '¿' in message or re.search(r'\b(?:que|como|donde|cuando|quien|por que)\b', folded):
            probability = self.settings.question_chance
        if group:
            probability = max(probability, self.settings.group_chance)
        sincere = asks_about_ai(message)
        previous = None
        last = self.continuity.get((room, sender))
        if last and self.clock() - last[1] < self.settings.continuity_ttl:
            previous = next((s for s in candidates if s.index == last[0]), None)
        continuing = max(probability, self.settings.continuity_chance)
        if previous and not group:
            # Mid-conversation the same penguin keeps answering, as a player would.
            return [previous] if sincere or self.rng.random() < continuing else []
        # Unnamed chat: players away from the keyboard do not answer it.
        pool = [s for s in candidates if not self._afk(s)] or (candidates if sincere else [])
        if not pool:
            return []
        pool.sort(key=self._rank(room, sender, message))
        if previous in pool:
            pool.remove(previous)
            pool.insert(0, previous)  # whoever was already talking with this player goes first

        def answers(session, chance):
            if sincere:
                return True
            if session is previous:
                return self.rng.random() < continuing
            return self.rng.random() < min(1.0, chance * self._attention(room, session, sender))

        if group:
            voices = []
            for session in pool:
                if len(voices) >= limit:
                    break
                if answers(session, probability):
                    voices.append(session)
            return voices
        if not answers(pool[0], probability):
            return []
        voices = pool[:1]
        chime = self.settings.chime_chance
        if not sincere and limit > 1 and chime > 0 and len(pool) > 1 and answers(pool[1], chime):
            voices.append(pool[1])  # a second penguin chimes in, as in any busy room
        return voices

    # ---------------------------------------------------------- attention
    def _afk(self, session):
        life = getattr(session.behavior, 'life', None)
        return bool(life is not None and life.afk)

    def _distance(self, room, a, b):
        members = {m.id: m for m in self.world.members(room)}
        first, second = members.get(a), members.get(b)
        if first is None or second is None or None in (first.x, first.y, second.x, second.y):
            return None
        return math.hypot(first.x - second.x, first.y - second.y)

    def _attention(self, room, speaker, sender):
        """How likely ``speaker`` is to pick up unnamed chat: distance and character."""
        factor = 1.0
        radius = self.settings.hearing_radius
        distance = self._distance(room, speaker.player_id, sender) if radius > 0 else None
        if distance is not None:
            factor *= 1.0 if distance <= radius else 0.4 if distance <= 2 * radius else 0.15
        card = speaker.bot.card
        if self.settings.chattiness_weight > 0 and card is not None:
            factor *= 1 + self.settings.chattiness_weight * (card.chattiness - 0.5)
        return factor

    def partner(self, session):
        """The human this bot answered in its room within the last minute, or None."""
        now = self.clock()
        for (room, human), (index, at) in reversed(self.continuity.items()):
            if now - at >= _ENGAGED_SECONDS:
                return None
            if index == session.index and room == session.room_id and self.is_human(room, human):
                return human
        return None

    def _typing(self, session, line, elapsed=0.0):
        """Seconds to wait before ``line`` appears: reaction plus typing, minus time already spent."""
        cps = self.settings.typing_cps
        if cps <= 0 or not line:
            return 0.0
        # A stable 0.8x-1.2x speed per penguin, so each one has its own rhythm.
        speed = cps * (0.8 + 0.4 * (zlib.crc32(session.bot.username.encode()) % 1000) / 999)
        delay = self.rng.uniform(*_REACTION) + len(line) / speed
        return max(0.0, min(_TYPING_MAX, delay) - elapsed)

    async def _type(self, session, line, started):
        delay = self._typing(session, line, self.clock() - started)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _pause(self, session, line, index, started):
        """Before a scripted line: its typing time, never shorter than the configured line gap."""
        pause = self._typing(session, line, self.clock() - started if index == 0 else 0.0)
        if index:
            pause = max(pause, self.rng.uniform(self.settings.line_delay_min, self.settings.line_delay_max))
        if pause > 0:
            await asyncio.sleep(pause)

    async def _step_closer(self, session, room, human):
        """Walk over to talk, like a player; skipped while the body is busy with its own walk."""
        life = getattr(session.behavior, 'life', None)
        if life is None or getattr(session.bot, 'greeter', False) or session.traveling or session.body_lock.locked():
            return
        try:
            async with session.body_lock:
                if session.room_id == room and session.conn is not None and not session.conn.closed:
                    await life.approach(human)
        except as2.Disconnected:
            return

    async def on_chat(self, observer, sender, message):
        room = observer.room_id
        if (self._closed or room not in self.config.catalog.rooms or not self.is_human(room, sender)
                or not self._fresh_frame(room, sender, str(message))):
            return
        pending = self.greetings.pop((room, sender), None)
        if pending:
            pending.cancel()  # a speaking human gets a reply, not a delayed second hello
        message = str(message)
        speakers = self.responders(room, sender, message)
        forget = bool(self.recall and wants_forget(message))
        if not speakers and forget:
            last = self.continuity.get((room, sender))
            if last:
                speakers = [s for s in self.active(room) if s.index == last[0]]
        if not speakers:
            return
        if forget:
            speaker = speakers[0]
            self._wake(speaker)
            self.recall.delete(speaker.index, int(sender))
            self.brains[speaker.index].memory.forget(sender)
            self._summarized.pop((speaker.index, int(sender)), None)
            if not self._enqueue(Turn(room, (speaker,), (speaker.conn,), sender, message[:300],
                                      local='Listo, borre lo que recordaba de ti.')):
                await speaker.metrics.skipped(speaker.index, metrics.SKIP_DIALOGUE_QUEUE)
            return
        # Every voice shares one thread, so later voices see what earlier ones said and
        # a newer line from this human makes any voice still waiting stand down.
        thread = Thread(self.clock(), addresses_group(message) or len(self._named_all(message)) > 1)
        self._remember(self._threads, (room, sender), thread)
        lead = None
        for speaker in speakers:
            self._wake(speaker)
            allowed, reason = speaker.gate.try_reserve(speaker.index, sender)
            if not allowed:
                await speaker.metrics.skipped(speaker.index, reason)
                continue
            if lead is None:
                lead = speaker
                turn = Turn(room, (speaker,), (speaker.conn,), sender, message[:300],
                            local=self._location_line(speaker, room, message), thread=thread)
            else:
                # Extra voices yield to replies another human is waiting for (P1, not P0).
                turn = Turn(room, (speaker,), (speaker.conn,), sender, message[:300],
                            kind='chorus', priority=spend.P1, thread=thread)
            if not self._enqueue(turn):
                await speaker.metrics.skipped(speaker.index, metrics.SKIP_DIALOGUE_QUEUE)

    def _wake(self, speaker):
        life = getattr(speaker.behavior, 'life', None)
        if life is not None and life.afk:
            life.wake()  # addressed directly: back at the keyboard

    def _location_line(self, speaker, room, message):
        target = self.lookup_target(message)
        if not target:
            return None
        location = self.world.room_of(target.player_id) if target.player_id is not None else None
        if self.knows(speaker, target, room) and location in _ROOM_NAMES:
            return f'{target.bot.nickname} esta en {_ROOM_NAMES[location]}.'
        return f'Ahora no se donde esta {target.bot.nickname}.'

    def member_added(self, room, player_id):
        if self._closed or room not in self.config.catalog.rooms or not self.is_human(room, player_id):
            return
        key, now = (room, player_id), self.clock()
        if key in self.greetings:
            return
        if now - self.greeted.get(key, float('-inf')) < self.settings.greeting_ttl:
            return
        self._remember(self.greeted, key, now)
        host = next((s for s in self.active(room) if getattr(s.bot, 'greeter', False)), None)
        if host is not None and len(self.greetings) < 64:
            self.greetings[key] = asyncio.create_task(self._host_welcome(room, player_id, host),
                                                      name='dialogue-host')
            return
        if len(self.greetings) >= 64 or self.rng.random() >= self.settings.greeting_chance:
            return
        task = asyncio.create_task(self._greet_after(room, player_id), name='dialogue-greeting')
        self.greetings[key] = task

    async def _greet_after(self, room, player_id):
        key = (room, player_id)
        try:
            await asyncio.sleep(self.rng.uniform(self.settings.greeting_delay_min, self.settings.greeting_delay_max))
            candidates = [s for s in self.active(room) if not self._afk(s)]
            if self._closed or not candidates or not self.is_human(room, player_id):
                return
            # Whoever stands closest to the door says hi, not someone across the room.
            distances = [(self._distance(room, s.player_id, player_id), s) for s in candidates]
            known = [(d, s.index, s) for d, s in distances if d is not None]
            speaker = min(known)[2] if known else self.rng.choice(candidates)
            self._enqueue(Turn(room, (speaker,), (speaker.conn,), player_id,
                               kind='greeting', priority=spend.P1,
                               model=self.rng.random() < self.settings.greeting_model_share))
        finally:
            if self.greetings.get(key) is asyncio.current_task():
                self.greetings.pop(key, None)

    async def _host_welcome(self, room, player_id, host):
        """A host walks up to the newcomer, greets through the model and later returns to its post."""
        key = (room, player_id)
        try:
            await asyncio.sleep(self.rng.uniform(*self.host_settle))
            if self._closed or not self.is_human(room, player_id) or host not in self.active(room):
                return
            await self._approach(host, room, player_id)
            if self._closed or not self.is_human(room, player_id) or host not in self.active(room):
                return
            self._enqueue(Turn(room, (host,), (host.conn,), player_id, kind='greeting',
                               priority=spend.P1, model=True))
            previous = self._returns.pop(host.index, None)
            if previous:
                previous.cancel()
            self._returns[host.index] = asyncio.create_task(self._return_to_post(host, room),
                                                            name='dialogue-host-return')
        except as2.Disconnected:
            return
        finally:
            if self.greetings.get(key) is asyncio.current_task():
                self.greetings.pop(key, None)

    async def _approach(self, host, room, player_id):
        """Stand about 50 px from the newcomer, on the side of the post, inside a roamable rectangle."""
        member = next((m for m in self.world.members(room) if m.id == player_id), None)
        post = host.bot.spawn
        rects = self.config.catalog.require(room).wander_rects(approximate=True)
        if member is None or member.x is None or member.y is None or not rects or post is None:
            return
        dx, dy = post[0] - member.x, post[1] - member.y
        distance = math.hypot(dx, dy) or 1.0
        reach = min(50.0, distance)
        tx, ty = member.x + dx / distance * reach, member.y + dy / distance * reach

        def gap(rect):
            return (max(rect.min_x - tx, 0, tx - rect.max_x) ** 2
                    + max(rect.min_y - ty, 0, ty - rect.max_y) ** 2)
        rect = min(rects, key=gap)
        tx, ty = int(min(max(tx, rect.min_x), rect.max_x)), int(min(max(ty, rect.min_y), rect.max_y))
        mine = next((m for m in self.world.members(room) if m.id == host.player_id), None)
        origin = (mine.x, mine.y) if mine is not None and mine.x is not None else post
        await host.send_move(tx, ty)
        await asyncio.sleep(min(self.host_walk_cap, math.hypot(tx - origin[0], ty - origin[1]) / 40.0 + 0.5))

    async def _return_to_post(self, host, room):
        """Go back to the post once nobody has talked to this host for a minute (or the room is empty)."""
        try:
            await asyncio.sleep(self.host_idle)
            while not self._closed and host in self.active(room) and self.world.has_humans(room):
                talking = any(r == room and idx == host.index and self.clock() - at < 60
                              for (r, _human), (idx, at) in list(self.continuity.items()))
                state = self.rooms.get(room)
                if not talking and not (state and state.pending.get(host.index, 0)):
                    break
                await asyncio.sleep(self.host_poll)
            if not self._closed and host in self.active(room) and host.bot.spawn:
                await host.send_move(*host.bot.spawn)
        except as2.Disconnected:
            return
        finally:
            if self._returns.get(host.index) is asyncio.current_task():
                self._returns.pop(host.index, None)

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
        started = self.clock()  # model latency counts as typing time
        if turn.kind == 'chorus':
            # An extra voice started typing when the human spoke, alongside the lead, so
            # it lands a room gap after the lead instead of a full typing time later.
            started = turn.thread.started
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
                    await self._pause(brain.session, answer.say, index, started)
                    if state.human_pending:  # a player spoke while this line was being typed
                        self._enqueue(replace(turn, lines=lines[index:]))
                        break
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
                await self._pause(brain.session, answer.say, index, started)
                if self._valid(turn) and not state.human_pending and await brain.publish(answer, spend.P2):
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
                try:
                    await self._type(session, answer.say, started)
                except asyncio.CancelledError:
                    if ticket:
                        self.travel.reservations.pop(session.index, None)
                    raise
                if self._valid(turn) and await brain.publish(answer, spend.P0):
                    state.last_sent = self.clock()
                    self.world.talked(session.player_id, turn.sender, self.config.life.conversation_seconds)
                    if turn.thread is not None:
                        turn.thread.lines.append((session.bot.nickname, answer.say))
                    self.travel.start(ticket)
                elif ticket:
                    self.travel.reservations.pop(session.index, None)
                return
        previous = self.continuity.get((turn.room, turn.sender))
        if turn.kind == 'greeting' and previous and self.clock() - previous[1] < self.settings.continuity_ttl:
            return
        chorus = turn.kind == 'chorus'
        if chorus and self._threads.get((turn.room, turn.sender)) is not turn.thread:
            return  # the human has said something newer; a late chime-in would answer the wrong line
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
                thread = turn.thread
                answer = await brain.reply(turn.sender, turn.message, priority=turn.priority,
                                           model=model, greeting=turn.kind == 'greeting',
                                           group=bool(thread and thread.group),
                                           heard=tuple(thread.lines) if thread else ())
            if chorus:
                # An extra voice is optional: a canned fallback (no model slot, provider
                # error, filtered text) would be a non sequitur next to the lead, so it
                # stays quiet. Only the lead may act on a travel intent.
                if (answer.source != 'llm' and not isinstance(session.provider, SimulatedProvider)
                        and not asks_about_ai(turn.message)):
                    return
                if answer.action is not None and answer.action not in actions.BODY_ACTIONS:
                    answer = replace(answer, action=None)
            if self.travel and isinstance(answer.action, str) and (answer.action == 'follow' or answer.action.startswith('go:')):
                from .travel import TravelDenied
                try:
                    travel_ticket = self.travel.offer(session, answer.action, turn.sender)
                    answer = await brain.safe('Intentare acompanarte, si hay sitio.')
                except TravelDenied as error:
                    await session.metrics.travel_denied(error.reason)
                    answer = await brain.safe('Ahora me quedo aqui para cuidar mi puesto.')
            if turn.sender is not None and answer.say and not chorus:
                await self._step_closer(session, turn.room, turn.sender)  # one walker, not a crowd
            await self._type(session, answer.say, started)
            keep_slot = bool(answer.source == 'llm' and answer.say and self._valid(turn))
        except asyncio.CancelledError:
            if travel_ticket:
                self.travel.reservations.pop(session.index, None)
            raise
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
            first = turn.thread is None or not turn.thread.lines
            if turn.thread is not None:
                turn.thread.lines.append((session.bot.nickname, answer.say))
            if turn.kind in ('reply', 'chorus'):
                if turn.kind == 'reply' or first:
                    # The first penguin to answer keeps the conversation; chorus voices do not take it over.
                    self._remember(self.continuity, (turn.room, turn.sender), (session.index, self.clock()))
                # Only this human's turns enter this bot's bounded short-term memory.
                content = brain.messages(turn.sender, turn.message)[-1]['content']
                brain.memory.add(turn.sender, content, answer.say)
                if self.recall and self.config.recall.enabled:
                    key = (session.index, int(turn.sender))
                    turns = sum(1 for item in brain.memory.context(turn.sender) if item['role'] == 'user')
                    if turns >= self.config.recall.min_turns and self._summarized.get(key, 0) < turns:
                        self._summarized[key] = turns
                        asyncio.create_task(self._summarize(session.index, turn.sender),
                                            name=f'recall-{session.index}-{turn.sender}')
            if travel_ticket:
                self.travel.start(travel_ticket)

    async def _summarize(self, bot_index, sender_id):
        brain = self.brains.get(bot_index)
        if brain is None:
            return
        try:
            await brain.summarize_relationship(sender_id)
        except Exception:
            logger.exception('relationship summary failed for bot %s player %s', bot_index, sender_id)

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
        tasks = (list(self.greetings.values()) + list(self._returns.values())
                 + ([self._ambient] if self._ambient else []))
        for state in self.rooms.values():
            tasks.extend(task for task in (state.worker, state.current) if task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.greetings.clear()
        self._returns.clear()
        self.rooms.clear()
        self.seen.clear()
        self._threads.clear()

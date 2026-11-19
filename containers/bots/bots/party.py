"""Dev disco party: five guests, circle dance, staggered affection emotes."""
import asyncio
import json
import logging
import math
import os
from pathlib import Path

from . import actions, spacing
from .rooms import load_catalog

logger = logging.getLogger('bots.party')

DISCO_ROOM = 120
DANCE_FRAME = actions.BODY_ACTIONS['dance'][1]
GUEST_COUNT = 5

# ``u#se`` ids from the vanilla emote menu (heart, flower, music note).
EMOTE_HEART = 1
EMOTE_FLOWER = 3
EMOTE_MUSIC = 4
AFFECTION_EMOTES = (EMOTE_HEART, EMOTE_FLOWER, EMOTE_MUSIC)
EMOTE_VISIBLE_SECONDS = 5.5
EMOTE_GAP = (7.0, 13.0)

RADIUS_X = 58
RADIUS_Y = 38

GUESTS = (
    {
        'username': 'fiestalila',
        'nickname': 'Lila',
        'lines': (
            '¿Me guardas este baile?',
            'Con esa pose ya me tienes mirándote.',
            'No me hagas esperar en la pista.',
        ),
    },
    {
        'username': 'fiestasol',
        'nickname': 'Sol',
        'lines': (
            'Brillas más que la bola del DJ.',
            'Con esos pasos me vas a conquistar.',
            'Ven, quiero bailar cerquita.',
        ),
    },
    {
        'username': 'fiestamar',
        'nickname': 'Mar',
        'lines': (
            'El ritmo te queda demasiado bien.',
            'Si sigues así no suelto tu mano.',
            '¿Siempre bailas así de bien?',
        ),
    },
    {
        'username': 'fiestaverde',
        'nickname': 'Vera',
        'lines': (
            'Esta pista es nuestra esta noche.',
            'Me encanta cómo mueves los pies.',
            'Un paso más y te sigo a todas partes.',
        ),
    },
    {
        'username': 'fiestaglam',
        'nickname': 'Glam',
        'lines': (
            'Tú mandas en esta fiesta.',
            'Quédate en el centro, nosotras te rodeamos.',
            'Así sí se arma la disco.',
        ),
    },
)


class PartySignal:
    """Polls the JSON bus the world server writes."""

    def __init__(self, path, clock):
        self.path = Path(path)
        self.clock = clock
        self._generation = 0

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, patch):
        document = self.read() or {}
        document.update(patch)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        payload = json.dumps(document, separators=(',', ':'), sort_keys=True)
        tmp.write_text(payload + '\n', encoding='utf-8')
        os.replace(tmp, self.path)

    def fresh_request(self):
        document = self.read()
        if not document:
            return None
        generation = int(document.get('generation', 0))
        if generation <= self._generation:
            return None
        if document.get('status') != 'requested':
            return None
        self._generation = generation
        return document


def circle_positions(center, rects, rng):
    """Five slots at 72° on a shallow ellipse (isometric floor)."""
    cx, cy = center
    slots = []
    offset = rng.uniform(0, 2 * math.pi)
    for index in range(GUEST_COUNT):
        angle = offset + index * (2 * math.pi / GUEST_COUNT)
        x = int(round(cx + RADIUS_X * math.cos(angle)))
        y = int(round(cy + RADIUS_Y * math.sin(angle)))
        if not any(rect.contains(x, y) for rect in rects):
            x = int(round(cx + (RADIUS_X - 12) * math.cos(angle)))
            y = int(round(cy + (RADIUS_Y - 8) * math.sin(angle)))
        slots.append((x, y))
    return slots


class PartyScene:
    """Runs one party lifecycle for a single invoker."""

    def __init__(self, manager, signal, document, clock, rng):
        self.manager = manager
        self.document = dict(document)
        self.clock = clock
        self.rng = rng
        self.signal = signal
        self.catalog = manager.config.catalog or load_catalog()
        self.room = self.catalog.rooms.get(DISCO_ROOM)
        self.tasks = []
        self._closed = False
        self._dancing = False
        self._invoker_dance_generation = 0
        self._processed_dance_gen = 0
        self._invoker_pos = (int(document['x']), int(document['y']))
        self._emote_turn = 0
        self._emote_task = None
        self._emote_visible_until = 0.0
        self._guest_sessions = []

    @property
    def invoker_id(self):
        return int(self.document['invoker_id'])

    def guests(self):
        by_name = {s.bot.username.casefold(): s for s in self.manager.sessions}
        sessions = []
        for spec in GUESTS:
            session = by_name.get(spec['username'])
            if session is not None:
                sessions.append((spec, session))
        return sessions

    def snapshot_invoker(self):
        member = next((m for m in self.manager.world.members(DISCO_ROOM) if m.id == self.invoker_id), None)
        if member is None or member.x is None or member.y is None:
            return self._invoker_pos
        self._invoker_pos = (member.x, member.y)
        return self._invoker_pos

    async def run(self):
        try:
            self.signal.write({'status': 'arriving', 'scene_id': self.document['scene_id']})
            if not await self._spawn_guests():
                await self._fail('spawn')
                return
            await self._flirt_phase()
            self.signal.write({'status': 'ready'})
            await self._watch_invoker()
        except asyncio.CancelledError:
            raise
        finally:
            await self._cleanup()

    async def _spawn_guests(self):
        guests = self.guests()
        if len(guests) < GUEST_COUNT:
            return False
        invoker = self.snapshot_invoker()
        spawn_tasks = []
        for index, (spec, session) in enumerate(guests):
            spawn_tasks.append(self._bring_guest(session, spec, invoker, index))
        results = await asyncio.gather(*spawn_tasks, return_exceptions=True)
        self._guest_sessions = [session for (_, session) in guests]
        return all(result is True for result in results)

    async def _bring_guest(self, session, spec, invoker, index):
        await self._pause_autonomy(session)
        if session.room_id != DISCO_ROOM:
            room = self.room
            spawn = room.walk_point(self.rng) if room else (330, 247)
            try:
                await session.change_room(DISCO_ROOM, spawn[0], spawn[1])
            except BaseException:
                return False
        floor = list(self.room.walk) if self.room else []
        origin = self._guest_xy(session)
        near = spacing.ring_spot(invoker, floor, [], self.rng, personal=50, origin=origin)
        target = near or spacing.pick_spot(floor, [], self.rng, personal=45, near=invoker, radius=90)
        if target is None:
            target = invoker
        await session.send_move(*target)
        await asyncio.sleep(min(4.0, math.hypot(target[0] - invoker[0], target[1] - invoker[1]) / 40 + 0.6))
        line = spec['lines'][index % len(spec['lines'])]
        await session.send_chat(session.player_id, line)
        return True

    def _guest_xy(self, session):
        member = next((m for m in self.manager.world.members(DISCO_ROOM)
                       if m.id == session.player_id), None)
        if member and member.x is not None:
            return (member.x, member.y)
        return self.room.spawn if self.room else (330, 247)

    async def _flirt_phase(self):
        guests = self.guests()
        for spec, session in guests:
            if self._closed:
                return
            await asyncio.sleep(self.rng.uniform(2.5, 4.5))
            await session.send_chat(session.player_id,
                                    self.rng.choice(spec['lines']))

    async def _watch_invoker(self):
        while not self._closed:
            if self.manager.world.human_room(self.invoker_id) != DISCO_ROOM:
                self.signal.write({'status': 'cancelled', 'cancel_reason': 'invoker_left'})
                return
            generation = self.manager.world.human_dance_generation(self.invoker_id)
            if generation > self._processed_dance_gen:
                self._processed_dance_gen = generation
                await self._form_and_dance()
            await asyncio.sleep(0.25)

    async def _form_and_dance(self):
        center = self.snapshot_invoker()
        rects = list(self.room.walk) if self.room else []
        positions = circle_positions(center, rects, self.rng)
        if not all(any(rect.contains(x, y) for rect in rects) for x, y in positions):
            glam = next((s for s in self._guest_sessions if s.bot.username == 'fiestaglam'), None)
            if glam is not None:
                await glam.send_chat(glam.player_id, 'Acércate un poco más al centro, guapo.')
        moves = []
        for session, spot in zip(self._guest_sessions, positions):
            moves.append(self._walk_and_dance(session, spot))
        await asyncio.gather(*moves)
        self._dancing = True
        self.signal.write({'status': 'dancing'})
        if self._emote_task is None or self._emote_task.done():
            self._emote_task = asyncio.create_task(self._emote_loop(), name='party-emotes')

    async def _walk_and_dance(self, session, spot):
        await session.send_move(spot[0], spot[1])
        await asyncio.sleep(min(3.5, math.hypot(spot[0] - self._invoker_pos[0],
                                                spot[1] - self._invoker_pos[1]) / 40 + 0.5))
        await session.send_frame(DANCE_FRAME)

    async def _emote_loop(self):
        turn = 0
        while self._dancing and not self._closed:
            if not self.manager.world.human_is_dancing(self.invoker_id):
                self._dancing = False
                return
            if self.manager.world.human_room(self.invoker_id) != DISCO_ROOM:
                return
            now = self.clock()
            if now < self._emote_visible_until:
                await asyncio.sleep(0.2)
                continue
            session = self._guest_sessions[turn % len(self._guest_sessions)]
            emote = AFFECTION_EMOTES[turn % len(AFFECTION_EMOTES)]
            await session.send_emote(emote)
            await session.send_frame(DANCE_FRAME)
            self._emote_visible_until = now + EMOTE_VISIBLE_SECONDS
            gap = self.rng.uniform(*EMOTE_GAP)
            await asyncio.sleep(gap)
            turn += 1

    async def _fail(self, reason):
        self.signal.write({'status': 'failed', 'failed_reason': reason})
        await self._cleanup()

    async def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        self._dancing = False
        if self._emote_task is not None:
            self._emote_task.cancel()
            await asyncio.gather(self._emote_task, return_exceptions=True)
        for session in self._guest_sessions:
            await self._resume_autonomy(session)
        self.signal.write({'status': 'done', 'ended_at': self.clock()})

    async def cancel(self):
        await self._cleanup()
        self.signal.write({'status': 'cancelled', 'cancel_reason': 'shutdown'})

    async def _pause_autonomy(self, session):
        if session.behavior.life is not None:
            await session.behavior.life.pause_party()
        session.behavior.party_hold = True

    async def _resume_autonomy(self, session):
        session.behavior.party_hold = False
        if session.behavior.life is not None:
            await session.behavior.life.resume_party()
        if session.room_id != session.bot.room and session.bot.room:
            if session.bot.spawn is not None:
                x, y = session.bot.spawn
            else:
                min_x, max_x, min_y, max_y = session.bot.area
                x = (min_x + max_x) // 2
                y = (min_y + max_y) // 2
            try:
                await session.change_room(session.bot.room, x, y)
            except BaseException:
                pass

    def on_invoker_move(self):
        if self._dancing:
            self._dancing = False
            if self._emote_task is not None:
                self._emote_task.cancel()
            self._emote_visible_until = 0.0

    def on_invoker_frame(self, frame):
        if frame == DANCE_FRAME:
            pass  # generation tracked on world model
        elif self._dancing:
            self.on_invoker_move()


class PartyCoordinator:
    """Single active scene per disco."""

    def __init__(self, manager, clock, rng):
        self.manager = manager
        self.clock = clock
        self.rng = rng
        path = os.environ.get('LAB_PARTY_BUS_PATH', '/var/lib/lab-party/scene.json')
        self.party_signal = PartySignal(path, clock)
        self._scene = None
        self._task = None
        self._poll = None

    def start(self):
        if os.environ.get('LAB_PARTY_ENABLED', '').lower() not in ('1', 'true', 'yes'):
            return
        self._poll = asyncio.create_task(self._poll_loop(), name='party-poll')

    async def close(self):
        if self._poll is not None:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
        if self._scene is not None:
            await self._scene.cancel()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _poll_loop(self):
        while True:
            document = self.party_signal.fresh_request()
            if document is not None and self._task is None:
                self._scene = PartyScene(self.manager, self.party_signal, document, self.clock, self.rng)
                self._task = asyncio.create_task(self._run_scene(), name='party-scene')
            await asyncio.sleep(0.35)

    async def _run_scene(self):
        try:
            await self._scene.run()
        finally:
            self._scene = None
            self._task = None

    def on_human_move(self, player_id):
        scene = self._scene
        if scene is not None and player_id == scene.invoker_id:
            scene.on_invoker_move()

    def on_human_frame(self, player_id, frame):
        scene = self._scene
        if scene is not None and player_id == scene.invoker_id:
            scene.on_invoker_frame(frame)

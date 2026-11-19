"""Tests for the dev ``/party`` disco scene."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'containers/bots'))
sys.path.insert(0, str(ROOT))

from bots.party import (  # noqa: E402
    AFFECTION_EMOTES, EMOTE_GAP, EMOTE_VISIBLE_SECONDS, GUEST_COUNT,
    PartyCoordinator, PartyScene, PartySignal, circle_positions,
)
from bots.world import World  # noqa: E402
from production.lab import party_bus, party_world  # noqa: E402


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeRoom:
    def __init__(self, bots=None):
        self.id = 120
        self.penguins_by_id = {b.id: b for b in (bots or [])}


class FakePenguin:
    def __init__(self, pid, username='human', nickname='Human', x=330, y=247):
        self.id = pid
        self.username = username
        self.nickname = nickname
        self.x, self.y = x, y
        self.room = None
        self.moderator = False
        self.server = None
        self.last_xt = None

    async def send_xt(self, *args):
        self.last_xt = args
        return None


class PartyWorldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus_path = Path(self.tmp.name) / 'scene.json'
        self.clock = FakeClock()
        self.server = SimpleNamespace(logger=mock.Mock())
        self.env = mock.patch.dict('os.environ', {
            'LAB_PARTY_ENABLED': 'true',
            'LAB_PARTY_BUS_PATH': str(self.bus_path),
        })
        self.env.start()
        bus = party_bus.PartyBus(self.bus_path, self.clock)
        self.bridge = party_world.PartyBridge(self.server, bus=bus, clock=self.clock)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_authorized_invoker_in_disco(self):
        room = FakeRoom()
        player = FakePenguin(42, username='devuser')
        player.room = room
        player.server = self.server
        self.server.lab_party = self.bridge
        ok = await self.bridge.try_chat(player, 42, '/party')
        self.assertTrue(ok)
        data = json.loads(self.bus_path.read_text())
        self.assertEqual(data['invoker_id'], 42)
        self.assertEqual(data['status'], 'requested')

    async def test_rejects_when_no_capacity(self):
        bots = [FakePenguin(i, username='djia') for i in range(1, 4)]
        room = FakeRoom(bots)
        player = FakePenguin(99)
        player.room = room
        await self.bridge.try_chat(player, 99, '/party')
        self.assertIn('cupo', player.last_xt[1].lower())
        self.assertFalse(self.bus_path.exists())

    async def test_duplicate_scene_blocked(self):
        self.bus_path.write_text(json.dumps({
            'status': 'arriving', 'generation': 1, 'invoker_id': 1}), encoding='utf-8')
        room = FakeRoom()
        player = FakePenguin(2)
        player.room = room
        await self.bridge.try_chat(player, 2, '/party')
        self.assertIn('activa', player.last_xt[1].lower())


class WorldFrameTests(unittest.TestCase):
    def test_dance_generation_once_per_transition(self):
        world = World(clock=FakeClock())
        world.snapshot(120, [])
        for _ in range(3):
            world.member_frame(120, 7, 26)
        self.assertEqual(world.human_dance_generation(7), 1)
        world.member_moved(120, 7, 340, 250)
        world.member_frame(120, 7, 26)
        self.assertEqual(world.human_dance_generation(7), 2)


class EmoteScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_emotes_rotate_without_overlap(self):
        clock = FakeClock()
        sent = []
        sessions = []
        for index in range(GUEST_COUNT):
            session = mock.Mock()
            session.player_id = 100 + index
            session.bot = SimpleNamespace(username=f'guest{index}')
            session.send_emote = mock.AsyncMock(side_effect=lambda e, s=session: sent.append((clock(), e, s.bot.username)))
            session.send_frame = mock.AsyncMock()
            sessions.append(session)
        manager = mock.Mock()
        manager.world = World(clock=clock)
        manager.world._frames[50] = {'frame': 26, 'dance_gen': 1}
        document = {'scene_id': 't', 'invoker_id': 50, 'x': 330, 'y': 247, 'room_id': 120}
        scene = PartyScene(manager, document, clock, __import__('random').Random(0))
        scene._guest_sessions = sessions
        scene._dancing = True
        task = asyncio.create_task(scene._emote_loop())
        for _ in range(5):
            clock.advance(EMOTE_VISIBLE_SECONDS + max(EMOTE_GAP) + 0.5)
            await asyncio.sleep(0)
        scene._dancing = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        usernames = [name for _, _, name in sent]
        self.assertEqual(len(usernames), len(set(usernames)))
        for index in range(1, len(sent)):
            gap = sent[index][0] - sent[index - 1][0]
            self.assertGreaterEqual(gap, EMOTE_VISIBLE_SECONDS)

    async def test_no_emote_after_cancel(self):
        clock = FakeClock()
        session = mock.Mock()
        session.send_emote = mock.AsyncMock()
        session.send_frame = mock.AsyncMock()
        manager = mock.Mock()
        manager.world = World(clock=clock)
        document = {'scene_id': 't', 'invoker_id': 50, 'x': 330, 'y': 247, 'room_id': 120}
        scene = PartyScene(manager, document, clock, __import__('random').Random(1))
        scene._guest_sessions = [session]
        scene._dancing = True
        task = asyncio.create_task(scene._emote_loop())
        await asyncio.sleep(0)
        scene._dancing = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session.send_emote.assert_not_called()


class FormationTests(unittest.TestCase):
    def test_circle_has_five_slots(self):
        rects = [SimpleNamespace(contains=lambda x, y: 280 <= x <= 380 and 230 <= y <= 265)]
        points = circle_positions((330, 247), rects, __import__('random').Random(2))
        self.assertEqual(len(points), GUEST_COUNT)


if __name__ == '__main__':
    unittest.main()
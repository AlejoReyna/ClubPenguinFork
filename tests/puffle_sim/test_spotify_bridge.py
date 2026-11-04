"""Spotify through the real Houdini chat handler: only the owner, in his igloo."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from houdini.data.pet import PenguinPuffle
from houdini.data.room import PenguinIglooRoom, Room
from experiments.puffle_sim.bridge import PuffleBridge
from experiments.puffle_sim.houdini_patch import transform
from experiments.puffle_sim.spotify import SimulatedSpotify, SpotifyAgent

from test_houdini_bridge import Player, Provider


class SlowSpotify(SimulatedSpotify):
    def __init__(self):
        super().__init__()
        self.gate = None
        self.entered = asyncio.Event()

    async def run(self, intent):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return await super().run(intent)


class SpotifyBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = (Path(os.environ.get('PUFFLE_UPSTREAM', '/app/houdini/houdini')) / 'handlers/play/message.py').read_bytes()
        namespace = {'__name__': 'puffle_synthetic_message_spotify'}
        exec(compile(transform('handlers/play/message.py', source), 'message.py', 'exec'), namespace)
        self.handler = namespace['handle_send_message'].callback
        self.server = SimpleNamespace(penguins_by_id={}, chat_filter_words={},
                                      config=SimpleNamespace(command_prefix=('!',)), commands={})
        self.room = PenguinIglooRoom(id=44, penguin_id=1)
        self.owner = Player(self.server, self.room, 1)
        self.owner.puffles[6] = PenguinPuffle(id=6, penguin_id=1, puffle_id=0, backyard=False)
        self.visitor = Player(self.server, self.room, 2)
        self.bot = Player(self.server, self.room, 3, human=False)
        self.accounts = {1: SlowSpotify(), 2: SlowSpotify(), 3: SlowSpotify()}
        self.inner = Provider()
        self.agent = SpotifyAgent(self.inner, self.accounts.get, min_interval=0)
        self.bridge = PuffleBridge(self.server, self.agent, lambda p: p.human,
                                   batch_seconds=0.01, min_interval=0, deadline=1)
        self.server.puffle_conversation = self.bridge
        self.assertTrue(self.bridge.enable(self.owner))

    async def asyncTearDown(self):
        for player, _ in list(self.bridge.bindings.values()):
            self.bridge.disable(player)
        await asyncio.sleep(0)

    async def send(self, p, message):
        await self.handler(p, p.id, message)

    def talks(self, p):
        return [x for x in p.received if x[0] == 'puffletalk']

    async def test_visitor_bot_and_other_room_cannot_drive_spotify(self):
        # Even an enabled visitor with his own puffle cannot act in someone else's igloo.
        self.bridge.enable(self.visitor)
        self.visitor.puffles[7] = PenguinPuffle(id=7, penguin_id=2, puffle_id=0, backyard=False)
        await self.send(self.visitor, 'pon despacito en spotify')
        await self.send(self.bot, 'siguiente')
        self.owner.room = Room(id=100)
        await self.send(self.owner, 'siguiente')
        await asyncio.sleep(0.05)
        self.assertEqual([a.calls for a in self.accounts.values()], [[], [], []])
        self.assertEqual(self.inner.calls, [])
        self.assertEqual(self.talks(self.visitor), [])

    async def test_owner_controls_only_his_account(self):
        await self.send(self.owner, 'pon despacito en spotify')
        await asyncio.wait_for(self.accounts[1].entered.wait(), 1)
        await asyncio.sleep(0.03)
        self.assertEqual(self.accounts[1].calls, [{'action': 'play', 'query': 'despacito'}])
        self.assertEqual(self.accounts[2].calls, [])
        self.assertEqual(self.inner.calls, [])
        expected = ('puffletalk', 2001, 6, 'Puse «Despacito» (simulado).')
        for p in (self.owner, self.visitor):
            self.assertEqual(self.talks(p), [expected])

    async def test_leave_cancels_before_confirmation(self):
        self.accounts[1].gate = asyncio.Event()
        await self.send(self.owner, 'siguiente')
        await asyncio.wait_for(self.accounts[1].entered.wait(), 1)
        self.bridge.leave(self.owner)
        self.accounts[1].gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.talks(self.visitor), [])
        self.assertNotIn(self.owner.id, self.bridge.bindings)

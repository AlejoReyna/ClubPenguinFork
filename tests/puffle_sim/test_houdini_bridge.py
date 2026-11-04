"""Real Houdini handler/model logic with isolated synthetic connections."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from houdini.data.pet import PenguinPuffle
from houdini.data.room import PenguinIglooRoom, Room
from experiments.puffle_sim.bridge import PuffleBridge
from experiments.puffle_sim.houdini_patch import transform


class Logger:
    def __init__(self):
        self.entries = []

    def error(self, msg):
        self.entries.append(msg)

    def info(self, msg):
        self.entries.append(msg)

    def warn(self, msg):
        self.entries.append(msg)


class Player:
    def __init__(self, server, room, player_id, human=True):
        self.server, self.room, self.id, self.human = server, room, player_id, human
        self.username = 'synthetic'
        self.joined_world, self.muted, self.moderator = True, False, False
        self.is_legacy_client, self.walking = True, None
        self.puffles, self.received, self.logger = {}, [], Logger()
        self.closed = False
        server.penguins_by_id[player_id] = self
        room.penguins_by_id[player_id] = self

    async def send_xt(self, *args):
        self.received.append(args)

    async def close(self):
        self.closed = True


class Provider:
    def __init__(self):
        self.calls = []
        self.called = asyncio.Event()
        self.gate = None

    async def evaluate(self, context):
        self.calls.append(context)
        self.called.set()
        if self.gate is not None:
            await self.gate.wait()
        return True, '¡Hola!'


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = (Path(os.environ.get('PUFFLE_UPSTREAM', '/app/houdini/houdini')) / 'handlers/play/message.py').read_bytes()
        namespace = {'__name__': 'puffle_synthetic_message'}
        exec(compile(transform('handlers/play/message.py', source), 'message.py', 'exec'), namespace)
        self.handler = namespace['handle_send_message'].callback
        self.namespace = namespace
        self.server = SimpleNamespace(penguins_by_id={}, chat_filter_words={},
                                      config=SimpleNamespace(command_prefix=('!',)), commands={})
        self.room = PenguinIglooRoom(id=44, penguin_id=1)
        self.owner = Player(self.server, self.room, 1)
        self.owner.puffles[6] = PenguinPuffle(id=6, penguin_id=1, puffle_id=0, backyard=False)
        self.visitor = Player(self.server, self.room, 2)
        self.bot = Player(self.server, self.room, 3, human=False)
        self.provider = Provider()
        self.bridge = PuffleBridge(self.server, self.provider, lambda p: p.human,
                                   batch_seconds=0.01, min_interval=0, deadline=1)
        self.server.puffle_conversation = self.bridge
        self.assertTrue(self.bridge.enable(self.owner))
        self.assertFalse(self.bridge.enable(self.bot))

    async def asyncTearDown(self):
        for player, _ in list(self.bridge.bindings.values()):
            self.bridge.disable(player)
        await asyncio.sleep(0)

    async def send(self, p, message):
        await self.handler(p, p.id, message)

    async def test_only_accepted_owner_chat_reaches_simulator(self):
        await self.send(self.visitor, 'visitante')
        await self.send(self.bot, 'bot')
        self.owner.muted = True
        await self.send(self.owner, 'silenciado')
        self.owner.muted = False
        self.server.chat_filter_words['prohibido'] = SimpleNamespace(ban=False, warn=False, filter=True)
        await self.send(self.owner, 'prohibido')
        await self.send(self.owner, '¿jugamos?')
        await asyncio.wait_for(self.provider.called.wait(), 1)
        await asyncio.sleep(0.03)
        self.assertEqual(self.provider.calls, [(('player', '¿jugamos?'),)])
        expected = ('puffletalk', 2001, 6, '¡Hola!')
        for p in (self.owner, self.visitor, self.bot):
            self.assertEqual([x for x in p.received if x[0] == 'puffletalk'], [expected])
        self.assertEqual(self.owner.logger.entries, [])

    async def test_commands_and_other_rooms_never_reach_simulator(self):
        async def command(*args):
            return None
        self.namespace['invoke_command_string'] = command
        await self.send(self.owner, '!comando')
        self.owner.room = Room(id=100)
        await self.send(self.owner, 'fuera')
        await asyncio.sleep(0.04)
        self.assertEqual(self.provider.calls, [])

    async def test_leave_cancels_slow_work_before_delivery(self):
        self.provider.gate = asyncio.Event()
        await self.send(self.owner, '¿hola?')
        await asyncio.wait_for(self.provider.called.wait(), 1)
        old = self.bridge.bindings[self.owner.id][1]
        session = self.bridge.engine.sessions[self.owner.id]
        self.bridge.leave(self.owner)
        self.provider.gate.set()
        await asyncio.sleep(0.04)
        self.assertEqual(len(session.history), 0)
        self.assertNotIn(old.player, self.bridge.bindings)
        self.assertFalse(any(x[0] == 'puffletalk' for x in self.visitor.received))

    async def test_spoofed_id_closes_connection_without_capture(self):
        await self.handler(self.owner, 99, 'suplantado')
        self.assertTrue(self.owner.closed)
        self.assertFalse(self.provider.calls)

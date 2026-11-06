"""World frame feeds, guarded room changes, safety and cast life over fake AS2."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from bots.config import build_config
from bots.manager import BotManager
from bots.metrics import Registry
from bots.provider import ProviderError, SimulatedProvider
from bots.safety import OutputFilter
from bots.session import RoomChangeError, _Heartbeat
from bots.world import World
from support import FakeWorld, config_document_world, room_broadcast
from test_session import make_session, start_running, stop_running, wait_until, chats_from
from test_world import record


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cast_heartbeat_precedes_the_server_sweep_and_legacy_is_unchanged(self):
        from bots.as2 import Disconnected
        for catalog, minimum, maximum in ((None, 60, 65), (object(), 59.5, 60)):
            effects = [Disconnected('test end')] if catalog is None else [None, Disconnected('test end')]
            session = SimpleNamespace(catalog=catalog,
                                      send_heartbeat=AsyncMock(side_effect=effects))
            with patch('bots.session.asyncio.sleep', new=AsyncMock()) as sleep:
                await _Heartbeat(session)._loop()
            delay = sleep.await_args.args[0]
            self.assertTrue(minimum <= delay <= maximum)
            if catalog is not None:
                self.assertEqual(session.send_heartbeat.await_count, 2)
                self.assertGreater(delay, 59)  # Houdini cooldown
                self.assertLess(delay, 61)     # server sweep

    async def test_world_tracks_ap_sp_rp_grs_and_server_jr(self):
        session = make_session(SimpleNamespace(port=6112), SimulatedProvider())
        session.world, session.player_id = World(), 1
        session.world.register_bot(1)
        session._enter_room(110, ['110', record(1)])
        await session._handle_xt(f'%xt%ap%-1%{record(9, "Human")}%')
        self.assertTrue(session.world.has_humans(110))
        await session._handle_xt('%xt%sp%-1%9%350%340%')
        self.assertEqual(session.world.humans_in(110)[0].x, 350)
        await session._handle_xt('%xt%rp%-1%9%')
        self.assertFalse(session.world.has_humans(110))
        await session._handle_xt(f'%xt%grs%-1%110%{record(1)}%{record(10)}%')
        self.assertEqual([m.id for m in session.world.humans_in(110)], [10])
        await session._handle_xt(f'%xt%jr%-1%330%{record(1)}%')
        self.assertEqual(session.world.room_of(1), 330)
        self.assertNotIn(10, session.roster)
        self.assertEqual(session.world.members(110), [])

    async def test_change_room_retries_210_updates_world_and_keeps_the_reader(self):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        session = make_session(world, SimulatedProvider())
        session.world = World()
        stop, task = await start_running(session)
        try:
            await wait_until(lambda: session.conn is not None)
            world.join_errors[330] = [210]
            await session.change_room(330, 400, 278)
            self.assertEqual(session.room_id, 330)
            self.assertEqual(session.world.room_of(session.player_id), 330)
            self.assertNotIn(session.player_id, world.rooms[100])
            await session.send_heartbeat()
            await wait_until(lambda: world.heartbeats > 0)
            self.assertIn('bots_travel_total{result="ok"} 1', session.metrics.registry.render())
            world.join_errors[120] = [210, 210]
            with self.assertRaises(RoomChangeError) as refused:
                await session.change_room(120, 330, 247)
            self.assertEqual(refused.exception.kind, 'full')
            self.assertEqual(session.room_id, 330)
            self.assertFalse(session.conn.closed)
        finally:
            await stop_running(stop, task)
            await world.stop()

    async def test_room_change_timeout_closes_unknown_session(self):
        session = make_session(SimpleNamespace(port=6112), SimulatedProvider())
        session.conn = AsyncMock()
        session.conn.closed = False
        session.conn.join_room.side_effect = asyncio.TimeoutError
        with self.assertRaises(RoomChangeError) as caught:
            await session.change_room(330, 400, 278, timeout=0.1)
        self.assertEqual(caught.exception.kind, 'timeout')
        session.conn.close.assert_awaited_once()

    async def test_cancelled_room_join_closes_an_unknown_connection(self):
        session = make_session(SimpleNamespace(port=6112), SimulatedProvider())
        session.conn = AsyncMock()
        session.conn.closed = False
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Future()
        session.conn.join_room.side_effect = blocked
        task = asyncio.create_task(session.change_room(330, 400, 278))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        session.conn.close.assert_awaited_once()

    async def test_cast_starts_life_and_anchors_pending_rooms(self):
        world = FakeWorld({'bruma': 'Bruma'})
        await world.start()
        doc = config_document_world(world.port)
        doc['bots'] = [{'username': 'bruma', 'password': 'synthetic'}]
        doc['behavior'].update(first_move_delay_min=0, first_move_delay_max=0,
                               life_min_delay=0.05, life_max_delay=0.1)
        cast = Path(__file__).resolve().parents[1] / 'containers/bots/cast'
        manager = BotManager(build_config(doc, {'BOTS_CAST_DIR': str(cast)}), Registry())
        session = manager.sessions[0]
        stop, task = await start_running(session)
        try:
            await wait_until(lambda: session.behavior.life is not None)
            await asyncio.sleep(0.3)
            self.assertEqual(world.moves, [(320, '200', '340')])
            self.assertIsNone(manager.ledger)
            self.assertEqual(manager.provider.calls, 0)
        finally:
            await stop_running(stop, task)
            await world.stop()

    async def test_filtered_reply_uses_persona_line_and_records_local_source(self):
        session = make_session(SimpleNamespace(port=6112), SimulatedProvider(replies=['Ve a example.com']))
        session.player_id = 1
        session.send_chat = AsyncMock()
        behavior = session.behavior
        behavior.fallback_replies = ['Estoy practicando mi baile.']
        with patch('bots.behavior._POST_SEND_PACE', (0, 0)):
            await behavior._reply(9, 'Guia hola')
        session.send_chat.assert_awaited_once_with(1, 'Estoy practicando mi baile.')
        metrics = session.metrics.registry.render()
        self.assertIn('bots_output_filtered_total{reason="url"} 1', metrics)
        self.assertIn('bots_replies_total{priority="P0",source="local"} 1', metrics)
        self.assertIn('admite que eres IA', behavior.system_prompt)

    async def test_unsafe_fallback_is_never_sent_and_bot_priority_is_p2(self):
        provider = AsyncMock()
        provider.complete.side_effect = ProviderError('budget')
        session = make_session(SimpleNamespace(port=6112), provider)
        session.player_id = 1
        session.roster[9] = 'AnotherBot'
        session.send_chat = AsyncMock()
        behavior = session.behavior
        behavior.peer_bot_names = frozenset(['AnotherBot'])
        behavior.fallback_replies = ['blocked']
        behavior.output_filter = OutputFilter(['blocked'])
        await behavior._reply(9, 'Guia hola')
        self.assertEqual(provider.complete.await_args.kwargs, {'priority': 'P2'})
        session.send_chat.assert_not_awaited()

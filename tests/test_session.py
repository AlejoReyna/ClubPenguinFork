"""Full bot session against the protocol-level fake world (no real network).

Covers the flows the requirement list names: login+room join, one reply per
properly addressed human message, valid movement, reconnect with the same
identity, auth failures as operator-facing errors, rate limiting and the
bounded, opt-in bot-to-bot exchange.
"""
import asyncio
import unittest
from unittest.mock import patch

from bots import behavior as behavior_module
from bots.config import build_config
from bots.metrics import BotMetrics, Registry
from bots.provider import SimulatedProvider
from bots.session import AuthFailure, BotSession

from support import FakeWorld, config_document_world, room_broadcast

# Small movement and pacing values so the tests finish quickly.
FAST = {
    'behavior.first_move_delay_min': 0.05,
    'behavior.first_move_delay_max': 0.15,
    'behavior.walk_min_delay': 0.15,
    'behavior.walk_max_delay': 0.3,
}


async def wait_until(predicate, timeout=8.0, interval=0.05, message='condition'):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f'timed out waiting for: {message}')
        await asyncio.sleep(interval)


def make_session(world, provider, bot_index=0, overrides=None):
    """A BotSession wired exactly like BotManager wires its sessions."""
    settings = dict(FAST)
    settings.update(overrides or {})
    config = build_config(config_document_world(world.port, **settings))
    bot = config.bots[bot_index]
    session = BotSession(
        index=bot_index, bot=bot, server=config.server, provider=provider,
        policy=config.policy, behavior=config.behavior,
        metrics=BotMetrics(Registry()),
        fallback_replies=config.provider.fallback_replies)
    peers = [other.nickname for other in config.bots if other is not bot]
    session.behavior.peer_bot_names = frozenset(peers)
    return session


def chats_from(world, player_id):
    """Messages the fake world saw from one player id."""
    return [message for _room, sender, message in world.chats
            if sender == str(player_id)]


async def start_running(session, name='session'):
    stop_event = asyncio.Event()
    task = asyncio.create_task(session.run(stop_event), name=name)
    return stop_event, task


async def stop_running(stop_event, task, timeout=6.0):
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        raise AssertionError('the session loop did not stop cleanly') from None


class LoginAndChatTests(unittest.IsolatedAsyncioTestCase):

    async def test_reader_and_movement_continue_while_provider_is_slow(self):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        provider = SimulatedProvider(replies=['Hola'], latency=1.5)
        session = make_session(world, provider)
        stop_event, task = await start_running(session, 'slow-provider')
        try:
            await wait_until(lambda: session.joined_room)
            await world.broadcast_human_chat(100, 171, 'GuiaIA hola')
            await wait_until(lambda: provider.calls == 1)
            before = len(world.moves)
            await world.broadcast_human_chat(100, 172, 'mensaje sin mencion')
            await wait_until(lambda: 172 in session.roster, timeout=0.5)
            await session.send_heartbeat()
            await wait_until(lambda: 'bots_heartbeats_total{bot="0"} 1' in session.metrics.registry.render(), timeout=0.5)
            await wait_until(lambda: len(world.moves) > before, timeout=0.5)
            self.assertFalse(chats_from(world, session.player_id))
        finally:
            await stop_running(stop_event, task)
            await world.stop()

    async def test_joins_the_room_and_replies_to_the_mention_only(self):
        world = FakeWorld({'guiaia': 'GuiaIA', 'pescadoria': 'PescadorIA'})
        await world.start()
        provider = SimulatedProvider(
            replies=['Hola, este es el muelle del lab.'])
        session = make_session(world, provider)
        stop_event, task = await start_running(session, 'guia')
        try:
            await wait_until(lambda: session.joined_room, message='room join')
            self.assertGreaterEqual(session.player_id, 100)
            self.assertEqual(world.account_ids.get('guiaia'), session.player_id)

            await world.broadcast_human_chat(100, 171,
                                             'ola GuiaIA, donde se pesca')
            await wait_until(lambda: chats_from(world, session.player_id),
                             message='first bot reply')
            self.assertEqual(
                chats_from(world, session.player_id),
                ['Hola, este es el muelle del lab.'])

            # The model context names the human from the room roster.
            last_call = provider.messages_received[-1]
            self.assertEqual(last_call[0]['role'], 'system')
            self.assertIn('GuiaIA', last_call[0]['content'])
            user_turn = last_call[-1]
            self.assertEqual(user_turn['role'], 'user')
            self.assertTrue(user_turn['content'].startswith('Humano171 dice: ola'))
        finally:
            await stop_running(stop_event, task)

        # The session never chats, even after seeing its own words echoed.
        provider.calls = 0
        echo = f'%xt%sm%-1%{session.player_id}%ola GuiaIA de verdad%'
        await room_broadcast(world.rooms[100], echo)
        await asyncio.sleep(0.8)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(chats_from(world, session.player_id)), 1)
        text = session.metrics.registry.render()
        self.assertIn('bots_replies_skipped_total{bot="0",reason="self"} 1', text)

    async def test_movement_walks_inside_the_configured_area(self):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        provider = SimulatedProvider(replies=['una frase'])
        session = make_session(world, provider)
        stop_event, task = await start_running(session, 'guia')
        try:
            await wait_until(lambda: len(world.moves) >= 4, message='moves')
            min_x, max_x, min_y, max_y = session.bot.area
            for room, x, y in world.moves:
                self.assertEqual(room, 100)
                self.assertIsInstance(int(x), int)
                self.assertTrue(min_x <= int(x) <= max_x, world.moves)
                self.assertTrue(min_y <= int(y) <= max_y, world.moves)
        finally:
            await stop_running(stop_event, task)

    async def test_stationary_bot_repeats_dance_without_walking(self):
        world = FakeWorld({'djia': 'DJ Cobra'})
        await world.start()
        document = config_document_world(world.port, **FAST)
        document['bots'] = [{
            'username': 'djia', 'password': 'secreto1', 'nickname': 'DJ Cobra',
            'room': 100, 'aliases': ['DJ'], 'stationary': True,
            'anchor': {'x': 40, 'y': 40}, 'repeat_action': 11,
        }]
        config = build_config(document)
        bot = config.bots[0]
        session = BotSession(
            index=0, bot=bot, server=config.server,
            provider=SimulatedProvider(replies=['ok']),
            policy=config.policy, behavior=config.behavior,
            metrics=BotMetrics(Registry()),
            fallback_replies=config.provider.fallback_replies)
        stop_event, task = await start_running(session, 'djia')
        try:
            await wait_until(lambda: len(world.actions) >= 2, message='dance actions')
            self.assertEqual(world.moves, [])
            for _room, player_id, action_id in world.actions:
                self.assertEqual(str(player_id), str(session.player_id))
                self.assertEqual(action_id, '11')
        finally:
            await stop_running(stop_event, task)

    async def test_reconnect_keeps_the_same_identity(self):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        provider = SimulatedProvider(replies=['aqui estoy'])
        session = make_session(world, provider)
        stop_event, task = await start_running(session, 'guia')
        try:
            await wait_until(lambda: session.joined_room, message='first join')
            original_id = session.player_id
            world.kick(original_id)
            await wait_until(
                lambda: session.joined_room and world.connections >= 4
                and original_id in world.rooms.get(100, {}),
                timeout=10, message='reconnected session')
            # Connections: login+world for the first session, then the retry.
            self.assertGreaterEqual(world.connections, 4)
            self.assertEqual(world.account_ids.get('guiaia'), original_id)
            self.assertIn(original_id, world.rooms[100])
            text = session.metrics.registry.render()
            self.assertIn('bots_reconnects_total{bot="0"', text)
        finally:
            await stop_running(stop_event, task)

    async def test_wrong_credentials_raise_authfailure(self):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        world.reject_codes['guiaia'] = 101
        provider = SimulatedProvider(replies=['nunca llego'])
        session = make_session(world, provider)
        stop_event = asyncio.Event()
        with self.assertRaises(AuthFailure) as caught:
            await asyncio.wait_for(session.run(stop_event), timeout=10)
        self.assertIn('code 101', str(caught.exception))
        self.assertGreater(caught.exception.retry_seconds, 0)
        text = session.metrics.registry.render()
        self.assertIn('bots_auth_failures_total{bot="0"} 1', text)


class RateAndRosterTests(unittest.IsolatedAsyncioTestCase):

    async def test_rate_limit_admits_only_one_reply_in_the_window(self):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        provider = SimulatedProvider(replies=['hola uno', 'hola dos'])
        session = make_session(world, provider, overrides={
            'policy.requests_per_minute': 1,
            'policy.player_cooldown': 0.0,
            'policy.bot_min_gap': 0.0,
        })
        stop_event, task = await start_running(session, 'guia')
        try:
            await wait_until(lambda: session.joined_room, message='room join')
            await world.broadcast_human_chat(100, 60, 'hola GuiaIA primero')
            await wait_until(lambda: chats_from(world, session.player_id),
                             message='first reply')
            await world.broadcast_human_chat(100, 61, 'hola GuiaIA segundo')
            await asyncio.sleep(1.2)
            self.assertEqual(len(chats_from(world, session.player_id)), 1)
            self.assertEqual(provider.calls, 1)
            text = session.metrics.registry.render()
            self.assertIn('bots_replies_skipped_total{bot="0",reason="rate_limit"} 1',
                          text)
        finally:
            await stop_running(stop_event, task)

    async def test_bots_stay_silent_to_each_other_when_disabled(self):
        world = FakeWorld({'guiaia': 'GuiaIA', 'pescadoria': 'PescadorIA'})
        await world.start()
        guia_provider = SimulatedProvider(replies=['que va PescadorIA'])
        pescador_provider = SimulatedProvider(replies=['claro GuiaIA'])
        guia = make_session(world, guia_provider, bot_index=0)
        pescador = make_session(world, pescador_provider, bot_index=1)
        stop_guia, guia_task = await start_running(guia, 'guia')
        stop_pescador, pescador_task = await start_running(pescador, 'pescador')
        try:
            await wait_until(
                lambda: guia.joined_room and pescador.joined_room,
                message='both bots online')
            # PescadorIA (another bot) addresses GuiaIA directly.
            addressed = (f'%xt%sm%-1%{pescador.player_id}'
                         '%hola GuiaIA pasamos buenos ratos%')
            await room_broadcast(world.rooms[100], addressed)
            await asyncio.sleep(1.0)
            self.assertEqual(guia_provider.calls, 0)
            self.assertEqual(chats_from(world, guia.player_id), [])
            text = guia.metrics.registry.render()
            self.assertIn('bots_replies_skipped_total{bot="0",reason="bot"} 1',
                          text)
        finally:
            await stop_running(stop_guia, guia_task)
            await stop_running(stop_pescador, pescador_task)


class BotLoopBoundedTests(unittest.IsolatedAsyncioTestCase):

    async def test_allowed_bot_exchange_is_strictly_bounded(self):
        with patch.object(behavior_module, '_POST_SEND_PACE', (0.01, 0.03)):
            world = FakeWorld({'guiaia': 'GuiaIA', 'pescadoria': 'PescadorIA'})
            await world.start()
            guia_provider = SimulatedProvider(
                replies=['Claro PescadorIA, por el muelle.'])
            pescador_provider = SimulatedProvider(
                replies=['De acuerdo GuiaIA, vamos juntos.'])
            guia = make_session(world, guia_provider, bot_index=0, overrides={
                'policy.player_cooldown': 0.0,
                'policy.bot_min_gap': 0.0,
                'policy.requests_per_minute': 50,
                'behavior.respond_to_bots': True,
                'behavior.bot_exchange_limit': 2,
            })
            pescador = make_session(world, pescador_provider, bot_index=1,
                                    overrides={
                                         'policy.player_cooldown': 0.0,
                                        'policy.bot_min_gap': 0.0,
                                        'policy.requests_per_minute': 50,
                                        'behavior.respond_to_bots': True,
                                        'behavior.bot_exchange_limit': 2,
                                    })
            for session in (guia, pescador):
                # Tests shrink only the two internal quiet periods; the
                # service keeps its defaults (5s minimum) in production.
                session.bot_gap.seconds = 0.0
                session.gate.bot_gap.seconds = 0.0
            stop_guia, guia_task = await start_running(guia, 'guia')
            stop_pescador, pescador_task = await start_running(pescador,
                                                               'pescador')
            try:
                await wait_until(
                    lambda: guia.joined_room and pescador.joined_room,
                    message='both bots online')
                first = (f'%xt%sm%-1%{pescador.player_id}'
                         '%hola GuiaIA demostrame algo%')
                await room_broadcast(world.rooms[100], first)
                # Wait until the conversation goes quiet at the bounded cap.
                async def stable():
                    count = len(world.chats)
                    await asyncio.sleep(0.6)
                    return count > 0 and count == len(world.chats)
                for _ in range(20):
                    if await stable():
                        break
                await asyncio.sleep(1.0)
                guia_chats = chats_from(world, guia.player_id)
                pescador_chats = chats_from(world, pescador.player_id)
                # Two exchanges each, then the limit cut the thread.
                self.assertEqual(len(guia_chats), 2, guia_chats)
                self.assertEqual(len(pescador_chats), 2, pescador_chats)
                for message in guia_chats + pescador_chats:
                    self.assertTrue(message)
            finally:
                await stop_running(stop_guia, guia_task)
                await stop_running(stop_pescador, pescador_task)


if __name__ == '__main__':
    unittest.main()

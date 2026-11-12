"""One room voice, real presence guards, greetings, scenes and player quotas."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bots.provider import ProviderError

from test_brain import FakeVoice, fixture
from test_session import wait_until
from test_world import record


async def drain(director, room=120):
    if room in director.rooms:
        await asyncio.wait_for(director.rooms[room].queue.join(), 3)


def sent(manager):
    return [(session.bot.username, call.args[1]) for session in manager.sessions
            for call in session.send_chat.await_args_list]


class DirectorTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, **kwargs):
        provider = kwargs.pop('provider', None) or FakeVoice()
        manager = fixture(provider=provider, **kwargs)
        self.addAsyncCleanup(manager.close)
        return manager, provider, manager.director

    async def test_all_observers_of_a_frame_produce_one_reply_and_no_echo_loop(self):
        manager, provider, director = await self.make()
        await asyncio.gather(*(s.behavior.on_chat(999, 'Lola, hola') for s in manager.sessions))
        await drain(director)
        self.assertEqual([name for name, _line in sent(manager)], ['lolaritmo'])
        self.assertEqual(len(provider.calls), 1)
        # Two names in one frame: one reply per named penguin, not one per observer.
        await asyncio.gather(*(s.behavior.on_chat(999, 'Lola y Tito, que tal') for s in manager.sessions))
        await drain(director)
        self.assertEqual([name for name, _line in sent(manager)], ['lolaritmo', 'lolaritmo', 'titogroove'])
        self.assertEqual(len(provider.calls), 3)
        for session in manager.sessions:
            await session.behavior.on_chat(manager.sessions[0].player_id, 'Tito hola')
        await drain(director)
        self.assertEqual(len(provider.calls), 3)

    async def test_topic_then_distance_then_continuity_and_expiry(self):
        manager, _provider, director = await self.make()
        self.assertEqual(director.choose(120, 999, 'hablemos de fiesta').bot.username, 'lolaritmo')
        self.assertEqual(director.choose(120, 999, 'hola').bot.username, 'mambo')
        await director.on_chat(manager.sessions[0], 999, 'Lola, hola')
        await drain(director)
        self.assertEqual(director.choose(120, 999, 'hola').bot.username, 'lolaritmo')
        now = director.clock()
        director.clock = lambda: now + 121
        self.assertEqual(director.choose(120, 999, 'hola').bot.username, 'mambo')

    async def test_slow_inference_keeps_room_serial_and_last_human_departure_cancels_it(self):
        provider = FakeVoice(blocked=True)
        manager, _provider, director = await self.make(provider=provider)
        observer = manager.sessions[0]
        await director.on_chat(observer, 999, 'Lola, hola lento')
        await asyncio.wait_for(provider.entered.wait(), 1)
        await director.on_chat(observer, 999, 'Tito, hola segundo')
        await asyncio.sleep(0)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.maximum_active, 1)
        manager.world.member_removed(120, 999)
        director.presence_changed(120)
        await drain(director)
        self.assertEqual(provider.cancelled, 1)
        self.assertEqual(sent(manager), [])
        manager.world.member_added(120, record(999))
        provider.release.set()
        await director.on_chat(observer, 999, 'Mambo, hola de nuevo')
        await drain(director)
        self.assertEqual([name for name, _line in sent(manager)], ['mambo'])

    async def test_empty_rooms_never_invoke_a_provider_or_scene(self):
        manager, provider, director = await self.make()
        manager.world.member_removed(120, 999)
        await director.on_chat(manager.sessions[0], 999, 'Lola hola')
        self.assertFalse(director.scene(120))
        director.member_added(120, 999)
        await asyncio.sleep(0)
        self.assertEqual(provider.calls, [])

    async def test_daily_player_quota_is_shared_across_bots_and_resets_next_day(self):
        manager, provider, director = await self.make(player_daily_limit=2)
        now = [dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)]
        director.utcnow = lambda: now[0]
        for message in ('Lola hola', 'Tito hola', 'Mambo hola'):
            await director.on_chat(manager.sessions[0], 999, message)
            await drain(director)
        self.assertEqual(len(provider.calls), 2)
        self.assertIn(sent(manager)[-1][1], manager.sessions[2].bot.fallbacks)
        now[0] += dt.timedelta(days=1)
        await director.on_chat(manager.sessions[0], 999, 'Lola otro dia')
        await drain(director)
        self.assertEqual(len(provider.calls), 3)

    async def test_local_fallbacks_do_not_use_the_player_model_quota(self):
        provider = FakeVoice([ProviderError('budget'), '{"say":"visita example.com"}',
                              '{"say":"Hola de nuevo"}'])
        manager, _provider, director = await self.make(provider=provider, player_daily_limit=1)
        for message in ('Lola una', 'Lola dos', 'Lola tres', 'Lola cuatro'):
            await director.on_chat(manager.sessions[0], 999, message)
            await drain(director)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(director.daily[999], 1)
        self.assertIn(sent(manager)[-1][1], manager.sessions[0].bot.fallbacks)

    async def test_persistent_quota_survives_a_director_restart_and_failed_save_stays_local(self):
        from bots.spend import SpendLedger
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'budget.json'
            manager, provider, director = await self.make(player_daily_limit=1)
            director.ledger = SpendLedger(path)
            await director.on_chat(manager.sessions[0], 999, 'Lola primero')
            await drain(director)
            await manager.close()
            second, second_provider, new = await self.make(player_daily_limit=1)
            new.ledger = SpendLedger(path)
            await new.on_chat(second.sessions[0], 999, 'Lola despues del reinicio')
            await drain(new)
            self.assertEqual(second_provider.calls, [])
            self.assertEqual(len(provider.calls), 1)
            with patch.object(new.ledger, '_persist', side_effect=OSError('read only')):
                second.world.member_added(120, record(998))
                await new.on_chat(second.sessions[0], 998, 'Lola otro jugador')
                await drain(new)
            self.assertEqual(second_provider.calls, [])

    async def test_a_reconnected_session_does_not_publish_an_old_inference(self):
        provider = FakeVoice(blocked=True)
        manager, _provider, director = await self.make(provider=provider)
        session = manager.sessions[0]
        await director.on_chat(session, 999, 'Lola una peticion')
        await asyncio.wait_for(provider.entered.wait(), 1)
        from types import SimpleNamespace
        session.conn = SimpleNamespace(closed=False)
        provider.release.set()
        await drain(director)
        self.assertEqual(sent(manager), [])
        self.assertEqual(director.daily[999], 0)
        session.conn = None  # no fake close method needed at teardown

    async def test_probabilities_are_applied_once_and_explicit_mentions_always_win(self):
        manager, _provider, director = await self.make()
        director.settings.hello_chance, director.settings.question_chance, director.settings.other_chance = .6, .5, .25
        for message, allowed, denied in (('hola', .59, .61), ('como va?', .49, .51), ('que bonito', .24, .26)):
            # Use an unambiguous plain statement for the 25% branch.
            if denied == .26:
                message = 'bonito paisaje'
            with patch.object(director.rng, 'random', return_value=allowed):
                self.assertIsNotNone(director.choose(120, 999, message))
            with patch.object(director.rng, 'random', return_value=denied):
                self.assertIsNone(director.choose(120, 999, message))
        with patch.object(director.rng, 'random', return_value=.99):
            self.assertEqual(director.choose(120, 999, 'Lola hola').bot.username, 'lolaritmo')

    async def test_greeting_is_once_per_human_room_and_cancelled_if_human_leaves(self):
        manager, provider, director = await self.make(greeting_chance=1, greeting_model_share=0,
                                                     greeting_delay_min=0.01, greeting_delay_max=0.01)
        for _ in range(3):
            director.member_added(120, 999)
        await wait_until(lambda: len(sent(manager)) == 1)
        await drain(director)
        self.assertEqual(provider.calls, [])
        manager.world.member_added(120, record(998))
        director.member_added(120, 998)
        manager.world.member_removed(120, 998)
        await asyncio.sleep(0.05)
        self.assertEqual(len(sent(manager)), 1)
        metrics = manager.registry.render()
        self.assertIn('bots_replies_total{priority="P1",source="local"} 1', metrics)

    async def test_model_greeting_uses_p1_and_dialogue_uses_p0(self):
        manager, provider, director = await self.make(greeting_chance=1, greeting_model_share=1,
                                                     greeting_delay_min=0, greeting_delay_max=0)
        director.member_added(120, 999)
        await wait_until(lambda: len(sent(manager)) == 1)
        self.assertEqual(provider.calls[0][1], 'P1')
        await director.on_chat(manager.sessions[0], 999, 'Lola un mensaje')
        await drain(director)
        self.assertEqual(provider.calls[-1][1], 'P0')

    async def test_host_walks_up_to_the_newcomer_greets_with_the_model_and_returns_to_its_post(self):
        manager, provider, director = await self.make(greeting_chance=0, greeting_delay_min=0, greeting_delay_max=0)
        host = manager.sessions[0]
        host.bot.greeter = True
        host.send_move = AsyncMock()
        manager.world.member_added(120, record(998, 'Nuevo', 340, 250))
        director.host_settle, director.host_walk_cap, director.host_idle, director.host_poll = (0, 0), 0, 0.05, 0.02
        director.member_added(120, 998)
        await wait_until(lambda: len(sent(manager)) == 1)
        await drain(director)
        self.assertEqual(sent(manager)[0][0], host.bot.username)
        self.assertEqual(provider.calls[0][1], 'P1')  # greeting goes through the model
        host.send_move.assert_awaited()
        x, y = host.send_move.await_args_list[0].args
        room = manager.config.catalog.require(120)
        self.assertTrue(any(rect.contains(x, y) for rect in room.wander_rects(approximate=True)))
        self.assertLessEqual(((x - 340) ** 2 + (y - 250) ** 2) ** .5, 60)
        await wait_until(lambda: host.send_move.await_count == 2)  # back to the post
        self.assertEqual(host.send_move.await_args_list[-1].args, tuple(host.bot.spawn))

    async def test_greeter_instruction_only_reaches_a_host(self):
        manager, _provider, director = await self.make()
        brain = director.brains[0]
        plain = json.dumps(brain.messages(999, '', greeting=True), ensure_ascii=False)
        self.assertNotIn('instruccion_del_puesto', plain)
        manager.sessions[0].bot.greeter = True
        self.assertIn('instruccion_del_puesto', json.dumps(brain.messages(999, '', greeting=True), ensure_ascii=False))

    async def test_scene_is_one_p2_call_for_two_or_three_validated_lines(self):
        def scene(messages, priority):
            cast = json.loads(messages[1]['content'].split(': ', 1)[1])
            return json.dumps({'lines': [{'bot': row['bot'], 'say': 'Escena de prueba'} for row in cast]})
        manager, provider, director = await self.make(provider=FakeVoice([scene]))
        self.assertTrue(director.scene(120))
        self.assertFalse(director.scene(120))
        await drain(director)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][1], 'P2')
        self.assertEqual(len(sent(manager)), 3)

    async def test_scene_http_usage_is_billed_once_and_p2_cutoff_sends_no_more_http(self):
        from bots.provider import MoonshotClient
        from bots.spend import SpendLedger
        from bots.metrics import BotMetrics, Registry
        from test_provider import FakeKimi
        server = FakeKimi()
        await server.start()
        self.addAsyncCleanup(server.stop)
        manager, _provider, director = await self.make(conversation_cooldown=0)
        lines = {'lines': [{'bot': i, 'say': f'Escena {i}'} for i in range(3)]}
        server.responses = [(200, json.dumps({'choices': [{'message': {'content': json.dumps(lines)}}],
                                              'usage': {'prompt_tokens': 1000, 'completion_tokens': 40}}).encode())]
        ledger = SpendLedger(None, per_minute=100)
        registry = Registry()
        client = MoonshotClient(f'http://127.0.0.1:{server.port}/v1', 'synthetic-only',
                                ledger=ledger, metrics=BotMetrics(registry))
        for session in manager.sessions:
            session.provider = client
        director.scene(120)
        await drain(director)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(ledger.day_micros, 1110)
        self.assertIn('bots_spend_usd_micros_total{priority="P2"} 1110', registry.render())
        ledger.reserve('P0', 800000)
        director.scene(120)
        await drain(director)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(len(sent(manager)), 6)

    async def test_conversation_cooldown_blocks_a_second_bot_started_scene(self):
        def scene(messages, priority):
            cast = json.loads(messages[1]['content'].split(': ', 1)[1])
            return json.dumps({'lines': [{'bot': row['bot'], 'say': 'Hola'} for row in cast]})
        manager, _provider, director = await self.make(provider=FakeVoice([scene]))
        self.assertTrue(director.scene(120))
        await drain(director)
        self.assertEqual(len(sent(manager)), 3)
        self.assertFalse(director.scene(120))
        now = director.clock()
        director.clock = lambda: now + 601
        self.assertTrue(director.scene(120))

    async def test_unknown_scene_speaker_falls_back_and_never_speaks_as_another_account(self):
        manager, provider, director = await self.make(provider=FakeVoice([
            '{"lines":[{"bot":500,"say":"bad"},{"bot":1,"say":"bad"},{"bot":2,"say":"bad"}]}']))
        director.scene(120)
        await drain(director)
        self.assertEqual(len(sent(manager)), 3)
        self.assertTrue(all(line != 'bad' for _name, line in sent(manager)))

    async def test_paco_location_is_observed_not_invented_and_friends_have_limited_knowledge(self):
        manager, provider, director = await self.make(usernames=('pacopregon', 'rayo', 'pixel'))
        observer = manager.sessions[0]
        await director.on_chat(observer, 999, 'Paco, donde esta Rayo?')
        await drain(director, 300)
        self.assertEqual(sent(manager), [('pacopregon', 'Rayo esta en Estadio.')])
        self.assertEqual(provider.calls, [])
        pixel, rayo = manager.sessions[2], manager.sessions[1]
        self.assertTrue(director.knows(pixel, rayo, 121))
        self.assertFalse(director.knows(rayo, observer, 802))
        manager.world.bot_left(rayo.player_id)
        await director.on_chat(observer, 999, 'Paco, donde anda Rayo?')
        await drain(director, 300)
        self.assertIn('no se', sent(manager)[-1][1])

    async def test_recall_summarizes_after_three_turns_and_forget_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recall.sqlite'
            manager, _provider, director = await self.make(
                recall={'enabled': True, 'path': str(path), 'min_turns': 3})
            observer = manager.sessions[0]
            for line in ('Lola, hola uno', 'Lola, hola dos', 'Lola, hola tres'):
                await director.on_chat(observer, 999, line)
                await drain(director)
            await asyncio.sleep(0.05)
            self.assertIsNotNone(manager.recall.get(observer.index, 999))
            await director.on_chat(observer, 999, 'Lola, borra mi memoria')
            await drain(director)
            self.assertIsNone(manager.recall.get(observer.index, 999))
            self.assertIn('borre', sent(manager)[-1][1].lower())


def numbered(messages, priority):
    """A distinct model line per call, so each voice can be told apart."""
    numbered.count += 1
    return json.dumps({'say': f'linea {numbered.count}', 'do': None})


class GroupChatTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, provider=None, **kwargs):
        numbered.count = 0
        provider = provider or FakeVoice([numbered])
        manager = fixture(provider=provider, **kwargs)
        self.addAsyncCleanup(manager.close)
        self.said = []  # every room line in the order it was sent
        for session in manager.sessions:
            session.send_chat.side_effect = (
                lambda _id, line, name=session.bot.username: self.said.append((name, line)))
        return manager, provider, manager.director

    def test_group_lines_are_told_apart_from_lines_for_one_penguin(self):
        from bots.director import addresses_group
        for line in ('que hacen', 'hola chicos!', 'alguien quiere jugar?', '¿Cómo están?',
                     'y ustedes?', 'que haceis', 'hola a todos', 'que onda gente'):
            self.assertTrue(addresses_group(line), line)
        for line in ('que haces', 'como estas?', 'hola', 'bonito paisaje', 'como va?', 'Lola, bailas?'):
            self.assertFalse(addresses_group(line), line)

    async def test_a_question_to_the_group_gets_several_voices_that_hear_each_other(self):
        manager, provider, director = await self.make()
        await director.on_chat(manager.sessions[0], 999, 'que hacen')
        await drain(director)
        # Nearest first (Mambo 10 px, Tito 30 px, Lola 50 px), each with its own line.
        self.assertEqual(self.said, [('mambo', 'linea 1'), ('titogroove', 'linea 2'),
                                         ('lolaritmo', 'linea 3')])
        self.assertEqual([priority for _messages, priority in provider.calls], ['P0', 'P1', 'P1'])
        turns = [json.loads(messages[-1]['content']) for messages, _priority in provider.calls]
        self.assertEqual({turn['tipo'] for turn in turns}, {'mensaje_al_grupo'})
        self.assertNotIn('ya_respondieron', turns[0])
        mambo = manager.sessions[2].bot.nickname
        self.assertEqual(turns[1]['ya_respondieron'], [{'nombre': mambo, 'dijo': 'linea 1'}])
        self.assertEqual(len(turns[2]['ya_respondieron']), 2)
        # Every voice remembers its own exchange; only the lead keeps the conversation.
        for index in range(3):
            self.assertEqual(len(director.brains[index].memory.context(999)), 2)
        self.assertEqual(director.continuity[(120, 999)][0], 2)
        self.assertEqual([s.bot.username for s in director.responders(120, 999, 'jaja')], ['mambo'])

    async def test_group_max_caps_voices_and_a_second_voice_can_chime_in(self):
        manager, _provider, director = await self.make(group_max=2)
        await director.on_chat(manager.sessions[0], 999, 'hola chicos')
        await drain(director)
        self.assertEqual([name for name, _line in self.said], ['mambo', 'titogroove'])
        director.continuity.clear()
        director.settings.chime_chance = 1
        self.assertEqual([s.bot.username for s in director.responders(120, 999, 'bonito paisaje')],
                         ['mambo', 'titogroove'])
        director.settings.group_max = 1
        self.assertEqual(len(director.responders(120, 999, 'bonito paisaje')), 1)
        self.assertEqual(len(director.responders(120, 999, 'que hacen')), 1)
        self.assertEqual(len(director.responders(120, 999, 'Lola y Tito, hola')), 1)

    async def test_each_penguin_decides_on_its_own_and_the_partner_goes_first(self):
        manager, _provider, director = await self.make(group_chance=0.5, question_chance=0.25,
                                                       other_chance=0.25)
        rolls = iter([0.9, 0.1, 0.1])  # Mambo passes, Tito and Lola answer
        director.rng = SimpleNamespace(random=lambda: next(rolls))
        self.assertEqual([s.bot.username for s in director.responders(120, 999, 'que hacen')],
                         ['titogroove', 'lolaritmo'])
        director.continuity[(120, 999)] = (0, director.clock())  # already talking with Lola
        director.rng = SimpleNamespace(random=lambda: 0.1)
        self.assertEqual([s.bot.username for s in director.responders(120, 999, 'que hacen')],
                         ['lolaritmo', 'mambo', 'titogroove'])
        # Not aimed at the group: the partner answers alone.
        self.assertEqual([s.bot.username for s in director.responders(120, 999, 'jaja')], ['lolaritmo'])

    async def test_a_newer_line_makes_waiting_voices_stand_down(self):
        provider = FakeVoice([numbered], blocked=True)
        manager, _provider, director = await self.make(provider=provider)
        await director.on_chat(manager.sessions[0], 999, 'que hacen')
        await asyncio.wait_for(provider.entered.wait(), 1)
        await director.on_chat(manager.sessions[0], 999, 'Mambo, y tu?')
        provider.release.set()
        await drain(director)
        self.assertEqual([name for name, _line in self.said], ['mambo', 'mambo'])
        self.assertEqual(len(provider.calls), 2)

    async def test_extra_voices_stay_quiet_instead_of_using_a_canned_line(self):
        provider = FakeVoice([json.dumps({'say': 'Mirando nubes', 'do': None}), ProviderError('http_5xx'),
                              json.dumps({'say': 'Yo tambien', 'do': 'go:330'})])
        manager, _provider, director = await self.make(provider=provider)
        await director.on_chat(manager.sessions[0], 999, 'que hacen')
        await drain(director)
        self.assertEqual(self.said, [('mambo', 'Mirando nubes'), ('lolaritmo', 'Yo tambien')])
        # The failed voice gave its player-quota slot back; nobody travels on a chorus line.
        self.assertEqual(director.daily[999], 2)
        self.assertEqual(manager.sessions[0].send_frame.await_count + manager.sessions[0].send_action.await_count, 0)

    async def test_ai_question_to_the_group_is_answered_honestly_by_each_voice(self):
        manager, provider, director = await self.make()
        await director.on_chat(manager.sessions[0], 999, 'ustedes son bots?')
        await drain(director)
        self.assertEqual(len(self.said), 3)
        self.assertTrue(all('IA' in line for _name, line in self.said))
        self.assertEqual(provider.calls, [])

    def test_group_settings_are_validated(self):
        from bots.config import build_config, ConfigError

        def build(**dialogue):
            return build_config({'bots': [{'username': 'nevado', 'password': 'synthetic'}],
                                 'dialogue': dialogue}, {})
        self.assertEqual((build().dialogue.group_max, build().dialogue.chime_chance), (3, 0.15))
        for dialogue in ({'group_max': 0}, {'group_max': 1.5}, {'group_chance': 1.2}, {'chime_chance': 2}):
            with self.assertRaises(ConfigError):
                build(**dialogue)

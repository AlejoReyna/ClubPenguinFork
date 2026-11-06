"""Local calendar, bounded scripts, empty-room policy and durable generation claims."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bots.config import build_config, ConfigError
from bots.spend import SpendLedger
from test_brain import FakeVoice, fixture
from test_director import drain, sent
from test_travel import activate_travel
from test_world import record

STAGE = ('dontelon', 'divaperla', 'bruno', 'kiki')
WHEN = dt.datetime(2026, 11, 6, 20, tzinfo=dt.timezone.utc)


def script(messages, priority):
    cast = json.loads(messages[1]['content'].split(': ', 1)[1])
    spec = json.loads(messages[-1]['content'])
    return json.dumps({'lines': [{'bot': cast[i % len(cast)]['bot'], 'say': 'Accion, empieza la funcion'}
                                for i in range(spec['min_lineas'])]})


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, provider=None, names=STAGE):
        provider = provider or FakeVoice([script])
        manager = fixture(usernames=names, provider=provider,
                           life={'enabled': True, 'events': True, 'departure_min': 0, 'departure_max': 0})
        activate_travel(manager)
        self.addAsyncCleanup(manager.close)
        return manager, provider, manager.director.events

    async def test_calendar_uses_configured_local_time_and_has_four_event_types(self):
        manager, _provider, events = await self.make()
        manager.config.service.tz = dt.timezone(dt.timedelta(hours=-6))
        self.assertIn(('theatre', 340, '2026-11-06T20:00'), events.due(dt.datetime(2026, 11, 7, 2, tzinfo=dt.timezone.utc)))
        self.assertIn(('party', 120, '2026-11-06T21:00'), events.due(dt.datetime(2026, 11, 7, 3, tzinfo=dt.timezone.utc)))
        self.assertTrue(any(kind == 'dojo' for kind, _room, _key in events.due(dt.datetime(2026, 11, 8, 0, tzinfo=dt.timezone.utc))))
        self.assertTrue(any(kind == 'race' for kind, _room, _key in events.due(dt.datetime(2026, 11, 7, 2, tzinfo=dt.timezone.utc))))

    async def test_theatre_generates_one_p1_completion_for_eight_to_twelve_lines(self):
        manager, provider, events = await self.make()
        manager.world.member_added(340, record(999))
        await events.tick(WHEN)
        await drain(manager.director, 340)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][1], 'P1')
        self.assertEqual(len(sent(manager)), 8)
        self.assertEqual(len({name for name, _ in sent(manager)}), 4)
        await events.tick(WHEN)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(events.current, {})

    async def test_script_completion_cap_is_reserved_per_call_and_p1_cutoff_is_local(self):
        from bots.provider import MoonshotClient
        from test_provider import FakeKimi
        server = FakeKimi()
        await server.start()
        self.addAsyncCleanup(server.stop)
        ledger = SpendLedger(None, per_minute=100)
        client = MoonshotClient(f'http://127.0.0.1:{server.port}/v1', 'synthetic-only', ledger=ledger)
        lines = {'lines': [{'bot': i % 4, 'say': 'Accion, se abre el telon'} for i in range(8)]}
        server.responses = [(200, json.dumps({'choices': [{'message': {'content': json.dumps(lines)}}],
                                              'usage': {'prompt_tokens': 1000, 'completion_tokens': 100}}).encode())]
        manager, _provider, events = await self.make(provider=client)
        manager.world.member_added(340, record(999))
        await events.tick(WHEN)
        await drain(manager.director, 340)
        self.assertEqual(server.requests[0][3]['max_completion_tokens'], 1024)
        self.assertEqual(ledger.day_micros, 1350)
        await client.complete([{'role': 'user', 'content': 'synthetic'}])
        self.assertEqual(server.requests[1][3]['max_completion_tokens'], 120)
        self.assertEqual(client.max_completion_tokens, 120)
        ledger.reserve('P0', 950000)
        await events.tick(WHEN + dt.timedelta(days=1))
        await drain(manager.director, 340)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(len(sent(manager)), 16)

    async def test_no_audience_means_no_generation_and_invalid_script_falls_back(self):
        manager, provider, events = await self.make(provider=FakeVoice(['{"lines":[{"bot":999,"say":"bad"}]}']))
        await events.tick(WHEN)
        self.assertEqual(provider.calls, [])
        manager.world.member_added(340, record(999))
        await events.tick(WHEN)
        await drain(manager.director, 340)
        self.assertEqual(len(sent(manager)), 8)
        self.assertTrue(all(line != 'bad' for _name, line in sent(manager)))

    async def test_generation_claim_survives_restart_and_failed_claim_stays_local(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'budget.json'
            first, provider, events = await self.make()
            first.director.ledger = SpendLedger(path)
            first.world.member_added(340, record(999))
            await events.tick(WHEN)
            await drain(first.director, 340)
            self.assertEqual(len(provider.calls), 1)
            await first.close()
            second, new_provider, new_events = await self.make()
            second.director.ledger = SpendLedger(path)
            second.world.member_added(340, record(999))
            await new_events.tick(WHEN)
            await drain(second.director, 340)
            self.assertEqual(new_provider.calls, [])
            with patch.object(second.director.ledger, '_persist', side_effect=OSError('disk full')):
                await new_events.tick(WHEN + dt.timedelta(days=1))
                await drain(second.director, 340)
            self.assertEqual(new_provider.calls, [])
            self.assertEqual(len(sent(second)), 8)

    async def test_last_human_leaving_cancels_event_and_clears_prompt_context(self):
        provider = FakeVoice(blocked=True)
        manager, _provider, events = await self.make(provider=provider)
        manager.world.member_added(340, record(999))
        await events.tick(WHEN)
        await asyncio.wait_for(provider.entered.wait(), 1)
        manager.world.member_removed(340, 999)
        manager.director.presence_changed(340)
        await drain(manager.director, 340)
        self.assertEqual(sent(manager), [])
        self.assertEqual(events.current, {})

    async def test_party_travel_is_local_and_guarded(self):
        names = ('lolaritmo', 'titogroove', 'mambo', 'ventisca', 'bolazo', 'avalancha', 'pixel', 'nube',
                 'dontelon', 'divaperla', 'bruno', 'kiki', 'bruma', 'sombra', 'tormenta', 'rayo', 'capigol')
        manager, provider, events = await self.make(names=names)
        when = dt.datetime(2026, 11, 6, 21, tzinfo=dt.timezone.utc)
        events.now = lambda: when
        await events.tick(when)
        jobs = list(manager.director.travel.jobs.values())
        await asyncio.gather(*jobs)
        self.assertEqual(provider.calls, [])
        visitors = [s for s in manager.sessions if s.bot.room not in (120, 121) and s.room_id in (120, 121)]
        self.assertEqual(len(visitors), 4)
        self.assertEqual({s.room_id for s in visitors}, {120, 121})
        self.assertGreaterEqual(manager.director.travel.projected()[230], 2)


class LifeConfigTests(unittest.TestCase):
    def test_defaults_off_and_flags_are_independent_of_provider_mode(self):
        document = {'bots': [{'username': 'testbot', 'password': 'synthetic'}]}
        self.assertFalse(build_config(document, {}).life.enabled)
        for raw in ({'enabled': True}, {'enabled': 'yes'}, {'minimum_home_share': .5},
                    {'follow_seconds': 601}, {'departure_min': 2, 'departure_max': 1}, {'dojo_hour': 24}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                build_config(dict(document, life=raw), {})

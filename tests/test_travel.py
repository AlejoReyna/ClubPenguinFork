"""Atomic reservations, safe map travel, following and a virtual 48-hour run."""
import asyncio
import datetime as dt
import unittest
from unittest.mock import AsyncMock

from bots.life import Life
from bots.session import RoomChangeError
from bots.travel import TravelDenied, invitation
from test_brain import fixture
from test_director import drain, sent
from test_world import record


def activate_travel(manager):
    travel = manager.director.travel
    now = [0.0]
    travel.clock = manager.director.clock = manager.world._clock = lambda: now[0]
    travel.last_update = 0
    for room in manager.config.catalog.rooms:
        manager.world.member_removed(room, 999)
    manager.world.member_added(120, record(999, 'SyntheticHuman', 350, 250))
    for session in manager.sessions:
        session.behavior.life = Life(session)
        session.send_snowball = AsyncMock()
        session.send_move = AsyncMock()

        async def change(room, x, y, own=session):
            own.room_id = room
            members = [record(m.id, m.nickname, m.x or x, m.y or y)
                       for m in manager.world.members(room) if not manager.world.is_bot(m.id)]
            members += [record(s.player_id, s.bot.nickname, x, y)
                        for s in manager.sessions if s.room_id == room]
            manager.world.bot_entered(own.player_id, room, members)

        session.change_room = AsyncMock(side_effect=change)
    return travel, now


def travel_fixture(usernames=('lolaritmo', 'titogroove', 'mambo'), provider=None, **settings):
    defaults = {'enabled': True, 'events': False, 'departure_min': 0,
                'departure_max': 0, 'visit_seconds': 120, 'wander_chance': 0}
    defaults.update(settings)
    manager = fixture(usernames=usernames, life=defaults, provider=provider)
    travel, now = activate_travel(manager)
    return manager, travel, now


class TravelTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, **kwargs):
        manager, travel, now = travel_fixture(**kwargs)
        self.addAsyncCleanup(manager.close)
        return manager, travel, now

    async def test_one_departure_reservation_prevents_a_racing_second_departure(self):
        manager, travel, _now = await self.make()
        first = travel.reserve(manager.sessions[0], 330)
        with self.assertRaises(TravelDenied) as rejected:
            travel.reserve(manager.sessions[1], 121)
        self.assertEqual(rejected.exception.reason, 'minimum')
        self.assertEqual(travel.projected()[120], 2)
        self.assertTrue(await travel.perform(first))
        self.assertEqual(manager.sessions[0].room_id, 330)
        self.assertNotIn(0, travel.reservations)

    async def test_travel_uses_only_catalog_spawn_and_pending_room_never_walks(self):
        manager, travel, _now = await self.make()
        session = manager.sessions[0]
        self.assertTrue(await travel.perform(travel.reserve(session, 121)))
        session.change_room.assert_awaited_once_with(121, 560, 320)
        life = session.behavior.life
        self.assertEqual(life.position, (560, 320))
        await life.step()
        session.send_move.assert_not_awaited()

    async def test_full_room_failure_releases_reservation_and_keeps_home(self):
        manager, travel, _now = await self.make()
        session = manager.sessions[0]
        session.change_room.side_effect = RoomChangeError('full')
        self.assertFalse(await travel.perform(travel.reserve(session, 330)))
        self.assertEqual(session.room_id, 120)
        self.assertFalse(travel.reservations)
        self.assertFalse(travel.leases)
        self.assertFalse(session.traveling)

    async def test_unknown_fixed_personality_conversation_and_busy_guards(self):
        manager, travel, now = await self.make(usernames=('lolaritmo', 'titogroove', 'mambo', 'bruma'))
        lola, bruma = manager.sessions[0], manager.sessions[3]
        for session, room, reason in ((lola, 999, 'unknown'), (bruma, 120, 'fixed'), (lola, 802, 'personality')):
            with self.subTest(reason=reason), self.assertRaises(TravelDenied) as error:
                travel.reserve(session, room)
            self.assertEqual(error.exception.reason, reason)
        manager.world.talked(lola.player_id, 999, 180)
        with self.assertRaises(TravelDenied):
            travel.reserve(lola, 330)
        ticket = travel.reserve(lola, 330, requester=999, kind='invitation')
        travel.reservations.clear()
        self.assertIsNotNone(ticket)
        now[0] = 181
        self.assertIsNotNone(travel.reserve(lola, 330))

    async def test_singleton_home_stays_observed_and_cannot_leave_humans(self):
        manager, travel, _now = await self.make(usernames=('lolaritmo', 'titogroove', 'mambo', 'peperina'))
        peperina = manager.sessions[3]
        with self.assertRaises(TravelDenied):
            travel.reserve(peperina, 230)
        arrival = travel.reserve(manager.sessions[0], 330)
        with self.assertRaises(TravelDenied):
            travel.reserve(peperina, 230)  # promised arrival is not physical coverage yet
        await travel.perform(arrival)
        manager.world.member_added(330, record(998))
        with self.assertRaises(TravelDenied):
            travel.reserve(peperina, 230)
        manager.world.member_removed(330, 998)
        self.assertIsNotNone(travel.reserve(peperina, 230))

    async def test_incoming_reservations_enforce_visitor_cap_with_absent_home_slots(self):
        names = ('lolaritmo', 'titogroove', 'mambo', 'ventisca', 'bolazo', 'avalancha',
                 'dontelon', 'bruno', 'kiki', 'pixel', 'nube')
        manager, travel, _now = await self.make(usernames=names)
        travel.reserve(manager.sessions[0], 121)
        travel.reserve(manager.sessions[3], 121)
        # A test persona wants to visit the same destination from a third home.
        manager.sessions[6].bot.card.wander_favorites = (121,)
        with self.assertRaises(TravelDenied) as blocked:
            travel.reserve(manager.sessions[6], 121)
        self.assertEqual(blocked.exception.reason, 'capacity')
        self.assertEqual(travel.projected()[121], 4)

    async def test_an_outgoing_visitor_does_not_free_capacity_until_it_has_left(self):
        names = ('lolaritmo', 'titogroove', 'mambo', 'ventisca', 'bolazo', 'avalancha',
                 'dontelon', 'bruno', 'kiki', 'pixel', 'nube')
        manager, travel, _now = await self.make(usernames=names)
        lola, ventisca, director = manager.sessions[0], manager.sessions[3], manager.sessions[6]
        director.bot.card.wander_favorites = (121,)
        await travel.perform(travel.reserve(lola, 121))
        await travel.perform(travel.reserve(ventisca, 121))
        leaving = travel.reserve(lola, 120)
        with self.assertRaises(TravelDenied) as error:
            travel.reserve(director, 121)
        self.assertEqual(error.exception.reason, 'capacity')
        await travel.perform(leaving)
        self.assertIsNotNone(travel.reserve(director, 121))

    async def test_reservation_is_invalid_if_the_server_changed_its_source_room(self):
        manager, travel, _now = await self.make()
        session = manager.sessions[0]
        ticket = travel.reserve(session, 330)
        session.room_id = 121
        self.assertFalse(await travel.perform(ticket))
        session.change_room.assert_not_awaited()
        self.assertFalse(travel.reservations)

    async def test_invitation_routes_locally_and_returns_when_lease_expires(self):
        manager, travel, now = await self.make()
        session = manager.sessions[0]
        await manager.director.on_chat(session, 999, 'Lola, vamos a la pizzeria')
        await drain(manager.director)
        if travel.jobs:
            await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(session.room_id, 330)
        self.assertEqual(manager.provider.calls, 0)
        self.assertIn('Intentare', sent(manager)[0][1])
        now[0] = travel.leases[0].expires + 1
        await travel.tick()
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(session.room_id, 120)

    async def test_schedule_blocks_choose_away_rooms_only_when_phase3_is_enabled(self):
        manager, travel, _now = await self.make()
        life = manager.sessions[0].behavior.life
        when = dt.datetime(2026, 11, 6, 13, 30, tzinfo=dt.timezone.utc)
        life.choose(when)
        # Fix only the schedule's chance draw, preserving the card's real block.
        life._choices[1] = True
        self.assertEqual(travel.destination(life, when), 330)

    async def test_virtual_48_hours_keep_all_planned_rooms_covered_and_full_over_70_percent(self):
        from pathlib import Path
        from bots.config import build_config
        from bots.manager import BotManager
        from bots.metrics import Registry
        import sys
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / 'scripts'))
        import bot_accounts
        document = bot_accounts.work_config_document()
        document['bots'].extend(bot_accounts.cast_config_document(root / 'containers/bots/cast')['bots'])
        document['dialogue'] = {'enabled': True, 'greeting_chance': 0}
        document['life'] = {'enabled': True, 'events': False, 'departure_min': 0,
                            'departure_max': 0, 'visit_seconds': 120, 'wander_chance': .3}
        manager = BotManager(build_config(document, {'BOTS_CAST_DIR': str(root / 'containers/bots/cast')}), Registry())
        self.addAsyncCleanup(manager.close)
        # Synthetic sessions only: this is a virtual clock test, not a real soak.
        from types import SimpleNamespace
        for i, session in enumerate(manager.sessions):
            session.player_id, session.room_id, session.joined_room = 100 + i, session.bot.room, True
            session.conn = SimpleNamespace(closed=False, close=AsyncMock())
            session.behavior.stopped = False
            manager.world.register_bot(session.player_id)
        for session in manager.sessions:
            manager.world.bot_entered(session.player_id, session.room_id,
                [record(s.player_id, s.bot.nickname) for s in manager.sessions if s.room_id == session.room_id])
        travel, clock = activate_travel(manager)
        for room in travel.plan:
            manager.world.member_added(room, record(9000 + room, 'SyntheticRoomHuman'))
        full = {room: 0 for room in travel.plan}
        moved = 0
        start = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
        for minute in range(48 * 60):
            clock[0] = minute * 60
            travel.update()
            for session in manager.sessions:
                if not session.bot.card:
                    continue
                lease = travel.leases.get(session.index)
                target = None
                if lease and clock[0] >= lease.expires:
                    target = session.bot.room
                elif not lease:
                    life = session.behavior.life
                    when = start + dt.timedelta(minutes=minute)
                    life.choose(when)
                    target = travel.destination(life, when)
                if target is not None and target != session.room_id:
                    try:
                        ticket = travel.reserve(session, target)
                        moved += bool(await travel.perform(ticket))
                    except TravelDenied:
                        pass
            counts = travel.projected()
            for room, planned in travel.plan.items():
                self.assertGreaterEqual(counts.get(room, 0), max(1, len(planned) - 1))
                if counts.get(room, 0) >= len(planned):
                    full[room] += 1
        self.assertGreater(moved, 100)
        for room, minutes in full.items():
            self.assertGreaterEqual(minutes / (48 * 60), .70, (room, minutes))

    async def test_follow_uses_observed_rooms_grace_and_does_not_extend_the_deadline(self):
        manager, travel, now = await self.make(usernames=('lolaritmo', 'titogroove', 'mambo', 'peperina'))
        session = manager.sessions[0]
        self.assertIsNone(travel.offer(session, 'follow', 999))
        deadline = travel.leases[0].expires
        manager.world.member_removed(120, 999)
        manager.world.member_added(330, record(999))
        await travel.tick()
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(session.room_id, 330)
        self.assertEqual(travel.leases[0].expires, deadline)
        manager.world.member_removed(330, 999)
        await travel.tick()
        self.assertEqual(session.room_id, 330)
        now[0] += 9
        await travel.tick()
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(session.room_id, 120)

    async def test_credit_requires_home_recovery_before_another_long_visit(self):
        manager, travel, now = await self.make()
        session = manager.sessions[0]
        await travel.perform(travel.reserve(session, 330))
        now[0] += 120
        travel.update()
        await travel.perform(travel.reserve(session, 120))
        with self.assertRaises(TravelDenied) as denied:
            travel.reserve(session, 330)
        self.assertEqual(denied.exception.reason, 'budget')
        now[0] += 360
        self.assertGreaterEqual(travel.reserve(session, 330).seconds, 119)

    async def test_negated_requests_and_arbitrary_coordinates_are_not_intents(self):
        manager, _travel, _now = await self.make()
        catalog = manager.config.catalog
        self.assertEqual(invitation('Lola, vamos a la pizzeria', catalog), 'go:330')
        self.assertEqual(invitation('Lola sigueme', catalog), 'follow')
        self.assertIsNone(invitation('Lola no vamos a la pizzeria', catalog))
        self.assertIsNone(invitation('go:999999 x=3 y=4', catalog))

    async def test_follow_in_room_uses_only_a_measured_segment(self):
        manager, travel, _now = await self.make()
        session = manager.sessions[0]
        session.behavior.behavior.personal_space = 0  # original walk: straight onto the player
        travel.offer(session, 'follow', 999)
        life = session.behavior.life
        self.assertEqual(life.target(), (350, 250))
        manager.world.member_moved(120, 999, 9999, -1)
        self.assertIsNone(life.target())

    async def test_model_travel_intent_is_validated_and_unknown_room_cannot_be_claimed(self):
        from test_brain import FakeVoice
        provider = FakeVoice(['{"say":"Ya llegue","do":"go:9999"}',
                              '{"say":"Ya llegue","do":"go:330"}'])
        manager, travel, _now = await self.make(provider=provider)
        session = manager.sessions[0]
        await manager.director.on_chat(session, 999, 'Lola sorprende a la isla')
        await drain(manager.director)
        self.assertEqual(session.room_id, 120)
        self.assertNotIn('Ya llegue', sent(manager)[0][1])
        await manager.director.on_chat(session, 999, 'Lola sorprendeme otra vez')
        await drain(manager.director)
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(session.room_id, 330)
        self.assertNotIn('Ya llegue', sent(manager)[-1][1])

    async def test_another_human_conversation_cannot_be_overridden_by_the_requester(self):
        manager, travel, _now = await self.make()
        session = manager.sessions[0]
        manager.world.talked(session.player_id, 999)
        manager.world.talked(session.player_id, 998)
        with self.assertRaises(TravelDenied) as error:
            travel.reserve(session, 330, requester=999, kind='invitation')
        self.assertEqual(error.exception.reason, 'conversation')

    async def test_snowball_reaction_is_local_deduplicated_and_does_not_move(self):
        manager, _travel, _now = await self.make(usernames=('bolazo', 'ventisca', 'avalancha'))
        bolazo = manager.sessions[0]
        manager.world.member_added(230, record(999, 'SyntheticHuman', 400, 240))
        position = next(m for m in manager.world.members(230) if m.id == bolazo.player_id)
        for observer in manager.sessions:
            await observer._handle_xt(f'%xt%sb%-1%999%{position.x}%{position.y}%')
        bolazo.send_snowball.assert_awaited_once_with(400, 240)
        bolazo.send_move.assert_not_awaited()
        self.assertEqual(manager.provider.calls, 0)

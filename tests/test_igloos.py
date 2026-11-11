"""Igloo body: role gate, j#jp/g#or/g#cr, lease and the room minimum. No provider HTTP."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bots import as2
from bots.cast import may_enter_igloo
from bots.travel import TravelDenied
from test_brain import FakeVoice, fixture
from test_director import drain, sent
from test_travel import travel_fixture
from test_world import record

ROOT = Path(__file__).resolve().parents[1]


def _load_provision():
    spec = importlib.util.spec_from_file_location(
        'provision_igloos', ROOT / 'scripts/provision-igloos.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wire(session):
    """Record open/join/close and move the world model the way the server would."""
    calls = []

    async def open_igloo():
        session.igloo_opened = True
        calls.append('or')

    async def close_igloo():
        if session.igloo_opened:
            calls.append('cr')
        session.igloo_opened = False

    async def join_igloo(owner_id):
        external = as2.igloo_external_id(owner_id)
        session.room_id = external
        calls.append(('jp', owner_id + as2.IGLOO_JOIN_OFFSET, external))
        return SimpleNamespace(args=[str(external)])

    session.open_igloo = open_igloo
    session.close_igloo = close_igloo
    session.join_igloo = join_igloo
    return calls


class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_helper_and_guide_never_enter_and_a_body_without_a_card_stays_out(self):
        manager = fixture(usernames=('lolaritmo', 'peperina', 'pacopregon', 'djia'))
        self.addAsyncCleanup(manager.close)
        by_name = {session.bot.username: session.bot for session in manager.sessions}
        self.assertTrue(may_enter_igloo(by_name['lolaritmo']))
        self.assertFalse(may_enter_igloo(by_name['peperina']))
        self.assertEqual(by_name['peperina'].role, 'helper')
        self.assertFalse(may_enter_igloo(by_name['pacopregon']))
        self.assertEqual(by_name['pacopregon'].role, 'guide')
        # Role note: the Secret post stays, the role is stamped, there is no life card.
        self.assertIsNone(by_name['djia'].card)
        self.assertEqual(by_name['djia'].role, 'helper')
        self.assertFalse(may_enter_igloo(by_name['djia']))
        self.assertEqual(by_name['djia'].room, 100)


class IglooTravelTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_refuses_before_occupancy(self):
        manager, travel, _now = travel_fixture(
            usernames=('peperina', 'pacopregon', 'djia'))
        self.addAsyncCleanup(manager.close)
        for session in manager.sessions:
            with self.assertRaises(TravelDenied) as rejected:
                travel.reserve_igloo(session)
            self.assertEqual(rejected.exception.reason, 'role')

    async def test_resident_lease_returns_home_on_the_fixed_post(self):
        manager, travel, now = travel_fixture()
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        calls = _wire(lola)
        ticket = travel.reserve_igloo(lola)
        self.assertEqual(ticket.kind, 'igloo')
        self.assertEqual(ticket.target, as2.igloo_external_id(lola.player_id))
        with self.assertRaises(TravelDenied) as busy:
            travel.reserve_igloo(lola)
        self.assertEqual(busy.exception.reason, 'busy')
        self.assertTrue(await travel.perform(ticket))
        self.assertEqual(calls[0], 'or')
        self.assertEqual(calls[1][0], 'jp')
        self.assertEqual(calls[1][1], lola.player_id + 1000)
        self.assertEqual(lola.room_id, as2.igloo_external_id(lola.player_id))
        self.assertEqual(lola.behavior.life.position, as2.IGLOO_POST)
        self.assertEqual(as2.IGLOO_POST, (380, 320))
        self.assertIn(lola.index, travel.leases)
        # No walk inside the igloo.
        lola.send_move.reset_mock()
        await lola.behavior.life.step()
        lola.send_move.assert_not_awaited()
        now[0] = travel.leases[lola.index].expires
        await travel.tick()
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(lola.room_id, lola.bot.room)
        self.assertNotIn(lola.index, travel.leases)
        self.assertIn('cr', calls)
        self.assertTrue(all(session.provider.calls == 0 for session in manager.sessions))

    async def test_singleton_with_a_human_cannot_leave_for_an_igloo(self):
        manager, travel, _now = travel_fixture(usernames=('mimi',))
        self.addAsyncCleanup(manager.close)
        mimi = manager.sessions[0]
        _wire(mimi)
        manager.world.member_added(mimi.room_id, record(998, 'Visitante', 400, 300))
        with self.assertRaises(TravelDenied) as rejected:
            travel.reserve_igloo(mimi)
        self.assertEqual(rejected.exception.reason, 'minimum')
        self.assertNotIn(mimi.index, travel.reservations)

    async def test_one_departure_holds_the_room_until_the_first_lands(self):
        manager, travel, _now = travel_fixture()
        self.addAsyncCleanup(manager.close)
        for session in manager.sessions:
            _wire(session)
        first = travel.reserve_igloo(manager.sessions[0])
        with self.assertRaises(TravelDenied) as rejected:
            travel.reserve_igloo(manager.sessions[1])
        self.assertEqual(rejected.exception.reason, 'minimum')
        self.assertTrue(await travel.perform(first))
        self.assertEqual(manager.sessions[0].room_id,
                         as2.igloo_external_id(manager.sessions[0].player_id))

    async def test_restart_clears_the_lease_and_closes_the_igloo(self):
        manager, travel, _now = travel_fixture()
        self.addAsyncCleanup(manager.close)
        session = manager.sessions[0]
        _wire(session)
        self.assertTrue(await travel.perform(travel.reserve_igloo(session)))
        self.assertTrue(session.igloo_opened)
        await travel.close()
        self.assertEqual(travel.leases, {})
        self.assertEqual(travel.reservations, {})
        self.assertFalse(session.igloo_opened)
        # A new world login always takes the home door, never j#jp.
        conn = AsyncMock()
        conn.handshake = AsyncMock(return_value='rnd')
        conn.login_legacy = AsyncMock(return_value=['7', 'key'])
        conn.join_world = AsyncMock()
        conn.join_room = AsyncMock(return_value=SimpleNamespace(args=['120']))
        conn.send_move = AsyncMock()
        conn.join_player_igloo = AsyncMock()
        session.igloo_opened = True
        session._login_key = 'key'
        session.server = SimpleNamespace(auth_timeout=1)
        await session._enter_world(conn)
        conn.join_room.assert_awaited()
        self.assertEqual(conn.join_room.await_args.args[0], session.bot.room)
        conn.join_player_igloo.assert_not_awaited()
        self.assertFalse(session.igloo_opened)


class IglooChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_hears_a_human_inside_the_igloo(self):
        provider = FakeVoice()
        manager = fixture(provider=provider, usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        session = manager.sessions[0]
        external = as2.igloo_external_id(session.player_id)
        session.room_id = external
        manager.world.bot_entered(session.player_id, external, [
            record(session.player_id, session.bot.nickname, *as2.IGLOO_POST),
            record(999, 'SyntheticHuman', 400, 330),
        ])
        await session.behavior.on_chat(999, 'Lola, hola desde el iglu')
        await drain(manager.director, external)
        self.assertEqual([name for name, _line in sent(manager)], ['lolaritmo'])
        self.assertEqual(len(provider.calls), 1)


class PacketTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_open_and_close_frames(self):
        reader = asyncio.StreamReader()
        written = bytearray()

        class Writer:
            def write(self, data):
                written.extend(data)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        conn = as2.AS2Connection(reader, Writer())
        reader.feed_data(b'%xt%jr%-1%2105%5|Ada|1|1|1|0|0|0|0|0|0|0|380|320|1|0|0|0%\x00')
        frame = await conn.join_player_igloo(105, 1)
        self.assertEqual(frame.args[0], '2105')
        self.assertIn(b'%xt%s%j#jp%-1%1105%\x00', bytes(written))
        written.clear()
        await conn.set_igloo_open(True)
        await conn.set_igloo_open(False)
        self.assertEqual(bytes(written), b'%xt%s%g#or%-1%\x00%xt%s%g#cr%-1%\x00')


class ProvisionSqlTests(unittest.TestCase):
    def test_sql_is_idempotent_and_rollback_only_undoes_its_audit(self):
        module = _load_provision()
        names = module.usernames_from_cast(ROOT / 'containers/bots/cast')
        self.assertIn('peperina', names)
        self.assertIn('baristaia', names)
        self.assertNotIn('guiaia', names)
        sql = module.provision_sql(names)
        self.assertEqual(sql, module.provision_sql(names))
        self.assertIn('ON CONFLICT (penguin_id) DO NOTHING', sql)
        self.assertIn('type, flooring, location, music, locked', sql)
        self.assertIn('SELECT t.id, 1, 0, 1, 0, true', sql)
        self.assertIn('p.igloo IS NULL', sql)
        undo = module.rollback_sql()
        self.assertIn('DELETE FROM igloo_provision_audit', undo)
        self.assertIn('SET igloo = a.previous_igloo', undo)
        with self.assertRaises(ValueError):
            module.provision_sql(('Guiá',))
        source = Path(module.__file__).read_text()
        self.assertNotIn('psycopg', source)
        self.assertNotIn('sqlalchemy', source)


if __name__ == '__main__':
    unittest.main()

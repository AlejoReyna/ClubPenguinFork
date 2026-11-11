"""Igloo invitations: seeded free will, occupancy, and dialog-log fields. No paid calls."""
import asyncio
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bots import as2, dialog_log, igloo_invite
from bots.dialog_log import DialogLog, emit, hash_player, install
from bots.provider import SimulatedProvider
from bots.travel import invitation
from test_brain import FakeVoice, fixture
from test_director import drain, sent
from test_igloos import _wire
from test_session import wait_until
from test_travel import travel_fixture
from test_world import record

UTC = dt.timezone.utc
DAY = '2026-11-11'
WHEN = dt.datetime(2026, 11, 11, 15, tzinfo=UTC)  # Mimi's work block
OFF_DUTY = dt.datetime(2026, 11, 11, 21, tzinfo=UTC)
HUMAN = 999


class FixedRoll:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def seed_for(username, host_key, chance, accept):
    """A life_seed whose first roll lands on the requested side of ``chance``."""
    for seed in range(10000):
        roll = igloo_invite.decision_rng(seed, username, DAY, host_key).random()
        if (roll < chance) is accept:
            return seed
    raise AssertionError(f'no seed for {username}')


def _events(log):
    found = []
    while True:
        try:
            _day, _kind, line = log._queue.get_nowait()
        except asyncio.QueueEmpty:
            return found
        found.append(json.loads(line))


def _park(manager, sessions, room, human_id=HUMAN):
    """Put these penguins in ``room`` with one human. Their plan stays at home."""
    members = [record(session.player_id, session.bot.nickname, 200, 200) for session in sessions]
    members.append(record(human_id, 'SyntheticHuman', 240, 200))
    for session in sessions:
        session.room_id = room
    manager.world.bot_entered(sessions[0].player_id, room, members)
    for session in sessions[1:]:
        manager.world._bot_room[session.player_id] = room


class VerdictTests(unittest.TestCase):
    def test_the_same_seed_repeats_and_the_roll_is_the_only_soft_rule(self):
        first = igloo_invite.decision_rng(7, 'lolaritmo', DAY, 'p:999').random()
        second = igloo_invite.decision_rng(7, 'lolaritmo', DAY, 'p:999').random()
        self.assertEqual(first, second)
        base = dict(role_blocked=False, on_duty=False, pinned=False, busy=False,
                    energy=0.8, social=0.3, recent=False, warm=False, host_is_human=True)
        # 0.84 is above a human's 0.70 and below 0.70 + 0.20.
        self.assertEqual(igloo_invite.verdict(FixedRoll(0.84), **base), ('decline', 'seeded_roll'))
        self.assertEqual(igloo_invite.verdict(FixedRoll(0.84), **{**base, 'warm': True}),
                         ('accept', 'seeded_roll'))
        self.assertEqual(igloo_invite.verdict(FixedRoll(0.10), **{**base, 'energy': 0.1}),
                         ('decline', 'low_energy'))
        self.assertEqual(igloo_invite.verdict(FixedRoll(0.10), **{**base, 'role_blocked': True})[0],
                         'skip')
        self.assertIsNone(igloo_invite.verdict(FixedRoll(0.10), **{**base, 'role_blocked': True})[1])

    def test_invitation_phrases_name_whose_igloo_and_a_no_cancels(self):
        catalog = SimpleNamespace(rooms={120: object(), 330: object()})
        self.assertEqual(invitation('Lola, vamos a tu iglu', catalog), 'igloo:yours')
        self.assertEqual(invitation('ven a mi casa', catalog), 'igloo:mine')
        self.assertEqual(invitation('casa mía, vamos', catalog), 'igloo:mine')
        self.assertIsNone(invitation('no, no vamos a tu iglu', catalog))
        self.assertEqual(invitation('Lola, vamos a la pizzeria', catalog), 'go:330')


class InvitationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._chance = igloo_invite.HOST_CHANCE
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: install(None))
        self.addCleanup(lambda: setattr(igloo_invite, 'HOST_CHANCE', self._chance))

    def _log(self, enabled='true'):
        root = Path(self.tmp.name)
        switch = root / 'enabled'
        switch.write_text(enabled + '\n')
        log = DialogLog(root / 'days', switch, 'test-salt', flush_seconds=60, switch_seconds=60,
                        retention_interval=10 ** 9)
        install(log)
        log.reload_switch()
        return log

    def _use(self, manager, username, host_key, chance, accept):
        manager.config.service.life_seed = seed_for(username, host_key, chance, accept)
        for session in manager.sessions:
            if getattr(session.behavior, 'life', None) is not None:
                session.behavior.life._last = WHEN

    async def test_fixed_seed_accepts_and_declines_without_calling_a_provider(self):
        manager, travel, _now = travel_fixture(usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        _wire(lola)
        log = self._log()
        self._use(manager, 'lolaritmo', 'p:999', 0.70, accept=False)
        await travel.answer_human_igloo(lola, HUMAN, 'yours', WHEN)
        declined = _events(log)
        self.assertEqual([row['kind'] for row in declined], ['decline'])
        self.assertEqual(declined[0]['reason'], 'seeded_roll')
        self.assertEqual(declined[0]['source'], 'local')
        self.assertEqual(declined[0]['dest_room_id'], as2.igloo_external_id(lola.player_id))
        self.assertEqual(declined[0]['speaker'], 'lolaritmo')
        self.assertEqual(lola.room_id, lola.bot.room)
        self.assertEqual(lola.provider.calls, 0)
        # The pair is cooling down, so the next answer is the rule, not a new roll.
        self._use(manager, 'lolaritmo', 'p:999', 0.70, accept=True)
        await travel.answer_human_igloo(lola, HUMAN, 'yours', WHEN)
        again = _events(log)
        self.assertEqual(again[0]['reason'], 'recent_decline')
        self.assertEqual(lola.room_id, lola.bot.room)

    async def test_fixed_seed_accept_travels_to_the_real_igloo_and_comes_home(self):
        manager, travel, now = travel_fixture(usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        calls = _wire(lola)
        _park(manager, [lola], 111)
        log = self._log()
        self._use(manager, 'lolaritmo', 'p:999', 0.70, accept=True)
        await travel.answer_human_igloo(lola, HUMAN, 'yours', WHEN)
        await asyncio.gather(*list(travel.jobs.values()))
        rows = _events(log)
        self.assertEqual([row['kind'] for row in rows], ['invite', 'accept', 'travel'])
        self.assertEqual(rows[1]['reason'], 'seeded_roll')
        self.assertEqual(rows[1]['source'], 'local')
        self.assertEqual(rows[2]['dest_room_id'], as2.igloo_external_id(lola.player_id))
        self.assertEqual(rows[2]['invite_id'], rows[0]['invite_id'])
        self.assertEqual(rows[2]['reason'], 'seeded_roll')
        self.assertIn('or', calls)
        self.assertEqual(lola.room_id, as2.igloo_external_id(lola.player_id))
        self.assertEqual(lola.behavior.life.position, (380, 320))
        self.assertEqual(lola.provider.calls, 0)
        now[0] = travel.leases[lola.index].expires
        await travel.tick()
        await asyncio.gather(*list(travel.jobs.values()))
        home = _events(log)
        self.assertEqual(home[0]['kind'], 'travel')
        self.assertEqual(home[0]['reason'], 'lease_expired')
        self.assertEqual(home[0]['dest_room_id'], lola.bot.room)
        self.assertEqual(home[0]['invite_id'], rows[0]['invite_id'])
        self.assertEqual(lola.room_id, lola.bot.room)
        self.assertNotIn(lola.index, travel.leases)
        self.assertIn('cr', calls)

    async def test_helpers_and_the_guide_never_take_part(self):
        manager, travel, _now = travel_fixture(usernames=('peperina', 'pacopregon', 'djia'))
        self.addAsyncCleanup(manager.close)
        log = self._log()
        igloo_invite.HOST_CHANCE = 1
        for session in manager.sessions:
            _wire(session)
            manager.world.member_added(session.room_id, record(HUMAN, 'SyntheticHuman', 200, 200))
            self.assertFalse(await travel.consider_igloo(session.behavior.life, WHEN))
            decision = await travel.answer_human_igloo(session, HUMAN, 'mine', WHEN)
            self.assertEqual(decision, 'skip')
            self.assertNotIn(session.index, travel.reservations)
            self.assertEqual(session.room_id, session.bot.room)
        self.assertEqual(_events(log), [])
        self.assertTrue(all(session.provider.calls == 0 for session in manager.sessions))

    async def test_a_human_invite_uses_the_same_rules_and_the_model_only_if_paid_and_allowed(self):
        provider = FakeVoice([json.dumps({'say': 'Dale, te abro', 'do': None})])
        manager, travel, _now = travel_fixture(usernames=('lolaritmo',), provider=provider)
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        _wire(lola)
        _park(manager, [lola], 111)
        log = self._log()
        self._use(manager, 'lolaritmo', 'p:999', 0.70, accept=True)

        class Gate:
            def allows(self, priority):
                return priority != 'P2'

        manager.director.ledger = Gate()
        await manager.director.on_chat(lola, HUMAN, 'Lola, vamos a mi casa')
        await drain(manager.director, 111)
        await asyncio.gather(*list(travel.jobs.values()))
        rows = _events(log)
        self.assertEqual([row['kind'] for row in rows], ['invite', 'accept', 'travel'])
        self.assertEqual(rows[1]['text'], 'Dale, te abro')
        self.assertEqual(rows[1]['source'], 'model')
        self.assertEqual(rows[1]['priority'], 'P0')
        self.assertEqual(rows[1]['reason'], 'seeded_roll')
        self.assertEqual(rows[2]['dest_room_id'], as2.igloo_external_id(HUMAN))
        self.assertEqual(rows[2]['invite_id'], rows[0]['invite_id'])
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIsInstance(lola.provider, SimulatedProvider)

    async def test_p2_cutoff_keeps_the_local_line_and_still_makes_the_trip(self):
        provider = FakeVoice([json.dumps({'say': 'esto no se llama', 'do': None})])
        manager, travel, _now = travel_fixture(provider=provider)
        self.addAsyncCleanup(manager.close)
        lola, tito = manager.sessions[0], manager.sessions[1]
        _park(manager, [lola, tito], 111)
        for session in (lola, tito):
            _wire(session)
        log = self._log()
        self._use(manager, 'titogroove', 'b:lolaritmo', 0.35, accept=True)

        class Closed:
            def allows(self, priority):
                return False

        manager.director.ledger = Closed()
        decision = await travel.invite_bot(lola, tito, WHEN)
        await asyncio.gather(*list(travel.jobs.values()))
        rows = _events(log)
        self.assertEqual(decision, 'accept')
        self.assertEqual(provider.calls, [])
        accept = next(row for row in rows if row['kind'] == 'accept')
        self.assertEqual(accept['source'], 'fallback')
        self.assertEqual(accept['priority'], 'P2')
        self.assertEqual(accept['reason'], 'seeded_roll')
        self.assertNotIn('esto no se llama', accept['text'])
        self.assertTrue(all(row['dest_room_id'] == as2.igloo_external_id(lola.player_id)
                            for row in rows if row['kind'] in ('invite', 'accept', 'travel')))
        self.assertEqual(lola.room_id, as2.igloo_external_id(lola.player_id))
        self.assertEqual(tito.room_id, as2.igloo_external_id(lola.player_id))

    async def test_a_life_step_takes_a_bot_when_two_can_leave_and_the_human_otherwise(self):
        manager, travel, _now = travel_fixture()
        self.addAsyncCleanup(manager.close)
        lola, tito, mambo = manager.sessions
        for session in manager.sessions:
            _wire(session)
        igloo_invite.HOST_CHANCE = 1
        await travel.consider_igloo(lola.behavior.life, WHEN)
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(lola.room_id, as2.igloo_external_id(lola.player_id))
        self.assertEqual(tito.room_id, 120)
        self.assertEqual(mambo.room_id, 120)
        self.assertEqual(travel.projected()[120], 2)

        other, travel2, _clock = travel_fixture()
        self.addAsyncCleanup(other.close)
        host, guest, stay = other.sessions
        for session in other.sessions:
            _wire(session)
        _park(other, [host, guest], 111)
        igloo_invite.HOST_CHANCE = 1
        self._use(other, 'titogroove', 'b:lolaritmo', 0.35, accept=True)
        decision = await travel2.consider_igloo(host.behavior.life, WHEN)
        await asyncio.gather(*list(travel2.jobs.values()))
        self.assertTrue(decision)
        igloo = as2.igloo_external_id(host.player_id)
        self.assertEqual(host.room_id, igloo)
        self.assertEqual(guest.room_id, igloo)
        self.assertEqual(stay.room_id, stay.bot.room)
        self.assertTrue(all(session.provider.calls == 0 for session in other.sessions))

    async def test_no_bot_loop_and_a_planned_room_is_not_left_under_its_minimum(self):
        manager, travel, _now = travel_fixture()
        self.addAsyncCleanup(manager.close)
        lola, tito, mambo = manager.sessions
        for session in manager.sessions:
            _wire(session)
        log = self._log()
        igloo_invite.HOST_CHANCE = 1
        manager.world.member_removed(120, HUMAN)
        self.assertIsNone(await travel.invite_bot(lola, tito, WHEN))
        self.assertFalse(await travel.consider_igloo(lola.behavior.life, WHEN))
        self.assertEqual(_events(log), [])
        self.assertTrue(all(session.room_id == 120 for session in manager.sessions))
        manager.world.member_added(120, record(HUMAN, 'SyntheticHuman', 350, 250))
        self._use(manager, 'titogroove', 'b:lolaritmo', 0.35, accept=True)
        decision = await travel.invite_bot(lola, tito, WHEN)
        rows = _events(log)
        self.assertEqual(decision, 'decline')
        self.assertEqual(rows[-1]['reason'], 'room_min_guard')
        self.assertEqual(rows[-1]['kind'], 'decline')
        self.assertEqual(travel.reservations, {})
        self.assertTrue(all(session.room_id == 120 for session in (lola, tito, mambo)))
        self.assertEqual(travel.projected()[120], 3)

    async def test_singleton_with_a_human_stays_and_says_why(self):
        manager, travel, _now = travel_fixture(usernames=('mimi',))
        self.addAsyncCleanup(manager.close)
        mimi = manager.sessions[0]
        _wire(mimi)
        manager.world.member_added(310, record(HUMAN, 'SyntheticHuman', 200, 200))
        log = self._log()
        await travel.answer_human_igloo(mimi, HUMAN, 'yours', WHEN)
        self.assertEqual(_events(log)[0]['reason'], 'off_schedule')
        self.assertEqual(mimi.room_id, 310)
        igloo_invite.HOST_CHANCE = 1
        mimi.behavior.life._last = OFF_DUTY
        # A fresh human so the pair cooldown from the work refusal does not apply.
        await travel.consider_igloo(mimi.behavior.life, OFF_DUTY)
        rows = _events(log)
        self.assertEqual(rows[-1]['reason'], 'room_min_guard')
        self.assertEqual(mimi.room_id, 310)
        self.assertEqual(travel.reservations, {})
        self.assertEqual(mimi.provider.calls, 0)

    async def test_low_energy_and_a_busy_partner_decline_before_any_walk(self):
        manager, travel, _now = travel_fixture(usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        _wire(lola)
        log = self._log()
        lola.behavior.life.needs['energy'] = 0.1
        await travel.answer_human_igloo(lola, HUMAN, 'mine', WHEN)
        self.assertEqual(_events(log)[0]['reason'], 'low_energy')
        self.assertEqual(lola.room_id, 120)
        lola.behavior.life.needs['energy'] = 0.8
        manager.world.talked(lola.player_id, 998, 180)
        await travel.answer_human_igloo(lola, HUMAN, 'mine', WHEN)
        self.assertEqual(_events(log)[-1]['reason'], 'busy_with_human')
        self.assertEqual(travel.reservations, {})

    async def test_restart_closes_the_igloo_and_the_next_login_is_the_home_door(self):
        manager, travel, _now = travel_fixture(usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        _wire(lola)
        _park(manager, [lola], 111)
        self._use(manager, 'lolaritmo', 'p:999', 0.70, accept=True)
        await travel.answer_human_igloo(lola, HUMAN, 'yours', WHEN)
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertTrue(lola.igloo_opened)
        await travel.close()
        self.assertEqual(travel.leases, {})
        self.assertEqual(travel.reservations, {})
        self.assertEqual(travel.jobs, {})
        self.assertFalse(lola.igloo_opened)
        self.assertFalse(lola.traveling)
        conn = AsyncMock()
        conn.handshake = AsyncMock(return_value='rnd')
        conn.login_legacy = AsyncMock(return_value=['7', 'key'])
        conn.join_world = AsyncMock()
        conn.join_room = AsyncMock(return_value=SimpleNamespace(args=['120']))
        conn.send_move = AsyncMock()
        conn.join_player_igloo = AsyncMock()
        lola.igloo_opened = True
        lola._login_key = 'key'
        lola.server = SimpleNamespace(auth_timeout=1)
        await lola._enter_world(conn)
        self.assertEqual(conn.join_room.await_args.args[0], lola.bot.room)
        conn.join_player_igloo.assert_not_awaited()
        self.assertFalse(lola.igloo_opened)

    async def test_switch_off_makes_emit_a_no_op_and_a_raw_player_id_is_never_stored(self):
        manager, travel, _now = travel_fixture(usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        _wire(lola)
        _park(manager, [lola], 111)
        dark = self._log(enabled='false')
        self.assertFalse(dark.enabled)
        self._use(manager, 'lolaritmo', 'p:999', 0.70, accept=True)
        await travel.answer_human_igloo(lola, HUMAN, 'yours', WHEN)
        await asyncio.gather(*list(travel.jobs.values()))
        self.assertEqual(_events(dark), [])
        self.assertFalse((Path(self.tmp.name) / 'days').exists())
        self.assertEqual(dark.errors['invalid'], 0)
        install(None)
        # The manager installs its own log (off, in this config). Put the test log on after it.
        fresh, travel2, _now2 = travel_fixture(usernames=('lolaritmo',))
        self.addAsyncCleanup(fresh.close)
        log = self._log()
        lola2 = fresh.sessions[0]
        _wire(lola2)
        _park(fresh, [lola2], 111)
        self._use(fresh, 'lolaritmo', 'p:999', 0.70, accept=True)
        await travel2.answer_human_igloo(lola2, HUMAN, 'yours', WHEN)
        await asyncio.gather(*list(travel2.jobs.values()))
        rows = _events(log)
        self.assertGreaterEqual(len(rows), 3)
        digest = hash_player(HUMAN, 'test-salt')
        for row in rows:
            self.assertEqual(set(row), set(dialog_log.FIELDS))
            self.assertNotIn('player_id', row)
            if row['kind'] in ('invite', 'accept', 'decline', 'travel'):
                self.assertEqual(row['dest_room_id'], as2.igloo_external_id(lola2.player_id))
                self.assertTrue(row['invite_id'])
            if row['target_kind'] == 'player':
                self.assertEqual(row['player'], digest)
            else:
                self.assertIsNone(row['player'])
            self.assertNotIn(str(HUMAN), json.dumps({k: v for k, v in row.items() if k != 'player'}))
        self.assertEqual(log.errors['invalid'], 0)
        # A direct emit while the switch is off still does nothing. The file was
        # turned on for the trip above; put it back before re-reading.
        (Path(self.tmp.name) / 'enabled').write_text('false\n')
        dark.reload_switch()
        self.assertFalse(dark.enabled)
        install(dark)
        emit(room_id=120, speaker='lolaritmo', kind='travel', target_kind='room', source='local',
             invite_id='ig9', reason='seeded_roll', dest_room_id=2100)
        self.assertEqual(_events(dark), [])


class SaidTests(unittest.IsolatedAsyncioTestCase):
    """reply, greeting and scene are logged only after safety, from the line that was sent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: install(None))

    def _log(self):
        root = Path(self.tmp.name)
        switch = root / 'enabled'
        switch.write_text('true\n')
        log = DialogLog(root / 'days', switch, 'test-salt', flush_seconds=60, switch_seconds=60,
                        retention_interval=10 ** 9)
        install(log)
        log.reload_switch()
        return log

    async def test_a_rejected_model_line_is_logged_as_the_local_fallback(self):
        provider = FakeVoice(['Ve a example.com ahora mismo'])
        manager = fixture(provider=provider, usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        log = self._log()
        await manager.sessions[0].behavior.on_chat(HUMAN, 'Lola, hola')
        await drain(manager.director)
        rows = _events(log)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'reply')
        self.assertEqual(rows[0]['source'], 'local')
        self.assertEqual(rows[0]['priority'], 'P0')
        self.assertEqual(rows[0]['human_text'], 'Lola, hola')
        self.assertNotIn('example.com', rows[0]['text'])
        self.assertEqual(rows[0]['text'], sent(manager)[0][1])
        self.assertEqual(rows[0]['player'], hash_player(HUMAN, 'test-salt'))

    async def test_greeting_and_scene_are_logged_after_they_are_sent(self):
        manager = fixture(greeting_chance=1, greeting_model_share=0,
                          greeting_delay_min=0, greeting_delay_max=0, usernames=('lolaritmo',))
        self.addAsyncCleanup(manager.close)
        log = self._log()
        manager.director.member_added(120, HUMAN)
        await wait_until(lambda: len(sent(manager)) == 1)
        await drain(manager.director)
        greeting = _events(log)
        self.assertEqual(greeting[0]['kind'], 'greeting')
        self.assertEqual(greeting[0]['source'], 'local')
        self.assertEqual(greeting[0]['priority'], 'P1')
        self.assertEqual(greeting[0]['text'], sent(manager)[0][1])
        self.assertIsNone(greeting[0]['human_text'])

        def scene(messages, priority):
            cast = json.loads(messages[1]['content'].split(': ', 1)[1])
            return json.dumps({'lines': [{'bot': row['bot'], 'say': 'Escena de prueba'} for row in cast]})

        crowd = fixture(provider=FakeVoice([scene]))
        self.addAsyncCleanup(crowd.close)
        install(log)
        self.assertTrue(crowd.director.scene(120))
        await drain(crowd.director)
        scenes = [row for row in _events(log) if row['kind'] == 'scene']
        self.assertEqual(len(scenes), 3)
        self.assertTrue(all(row['source'] == 'model' and row['priority'] == 'P2' for row in scenes))
        self.assertTrue(all(row['text'] == 'Escena de prueba' and row['scene_id'] for row in scenes))
        self.assertEqual([row['text'] for row in scenes], [line for _name, line in sent(crowd)])

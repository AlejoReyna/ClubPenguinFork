"""Deterministic schedules and needs; no movement from pending rooms."""
import datetime as dt
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bots.cast import parse_card
from bots.config import build_config
from bots.life import Life
from bots.rooms import load_catalog

CARDS = Path(__file__).resolve().parents[1] / 'containers/bots/cast'


def life_for(username='lolaritmo', seed=11):
    config = build_config({'bots': [{'username': username, 'password': 'synthetic'}],
                           'service': {'life_seed': seed}}, {'BOTS_CAST_DIR': str(CARDS)})
    bot = config.bots[0]
    session = SimpleNamespace(bot=bot, room_id=bot.room, service=config.service,
                              body_lock=asyncio.Lock(),
                              catalog=config.catalog, send_move=AsyncMock(),
                              send_frame=AsyncMock(), send_action=AsyncMock())
    return Life(session)


class LifeTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_crosses_midnight_and_ignores_away_blocks(self):
        life = life_for()
        now = dt.datetime(2026, 11, 5, 23, tzinfo=dt.timezone.utc)
        self.assertEqual(life.choose(now), 'dance')
        life._last = None  # isolate agenda coverage from two hours of needs
        self.assertEqual(life.choose(now + dt.timedelta(hours=2)), 'dance')
        life.card = parse_card({'username': 'lolaritmo', 'schedule': [
            {'from': '00:00', 'to': '23:59', 'room': 330, 'activity': 'eat'}]})
        life.choose(now)
        self.assertEqual(life.session.room_id, 120)
        self.assertNotEqual(life.activity, 'eat')

    async def test_same_seed_produces_the_same_body_choices(self):
        a, b = life_for(), life_for()
        now = dt.datetime(2026, 11, 5, 8, tzinfo=dt.timezone.utc)
        sequence = []
        for minute in range(15):
            at = now + dt.timedelta(minutes=minute)
            self.assertEqual(a.choose(at), b.choose(at))
            self.assertEqual(a.target(), b.target())
            self.assertEqual(a.needs, b.needs)
            sequence.append(a.activity)
        self.assertGreater(len(set(sequence)), 1)

    async def test_needs_override_schedule_and_remain_bounded(self):
        life = life_for()
        now = dt.datetime(2026, 11, 5, 20, tzinfo=dt.timezone.utc)
        life.needs['energy'] = 0.1
        self.assertEqual(life.choose(now), 'rest')
        life.choose(now + dt.timedelta(minutes=3))
        self.assertGreater(life.needs['energy'], 0.1)
        life.needs['hunger'] = 0.99
        life.needs['energy'] = 1
        self.assertEqual(life.choose(now + dt.timedelta(minutes=4)), 'eat')
        self.assertTrue(all(0 <= n <= 1 for n in life.needs.values()))

    async def test_unmeasured_room_never_walks_even_if_bored(self):
        life = life_for('bruma')
        life.needs['boredom'] = 1
        life.card.schedule = ()
        await life.step()
        life.session.send_move.assert_not_awaited()
        self.assertEqual(life.position, load_catalog().require(320).spawn)

    async def test_dance_uses_the_verified_frame_after_a_safe_walk(self):
        life = life_for()
        life.clock = lambda: dt.datetime(2026, 11, 5, 20, tzinfo=dt.timezone.utc)
        with patch('bots.life.asyncio.sleep', new=AsyncMock()):
            await life.step()
        life.session.send_frame.assert_awaited_with(26)
        for call in life.session.send_move.await_args_list:
            self.assertTrue(life.session.catalog.require(120).allows(*call.args))

    async def test_explicit_room_change_does_not_enable_travel_behavior(self):
        life = life_for()
        life.session.room_id = 330
        await life.step()
        life.session.send_move.assert_not_awaited()
        life.session.send_frame.assert_not_awaited()

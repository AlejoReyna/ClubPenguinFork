"""Shared observations are idempotent and discard rooms without listeners."""
import unittest

from bots.world import World, parse_record


def record(pid, name='Penguin', x=300, y=320):
    return f'{pid}|{name}|8|1|0|0|0|0|0|0|0|0|{x}|{y}|1|'


class WorldTests(unittest.TestCase):
    def test_record_positions_and_malformed_frames(self):
        self.assertEqual(parse_record(record(5)), (5, 'Penguin', 300, 320))
        self.assertEqual(parse_record('5|Short'), (5, 'Short', None, None))
        for bad in (None, '100', 'bad|Penguin'):
            self.assertIsNone(parse_record(bad))

    def test_roster_presence_positions_and_human_classification(self):
        world = World()
        world.register_bot(1)
        world.register_bot(2)
        for bot in (1, 2):
            world.bot_entered(bot, 120, [record(1), record(2), record(9, 'Human')])
        self.assertEqual(len(world.members(120)), 3)
        self.assertEqual([p.id for p in world.humans_in(120)], [9])
        world.member_added(120, record(10, 'New', 1, 2))
        world.member_added(120, record(10, 'New', 1, 2))
        self.assertEqual(len(world.members(120)), 4)
        world.member_moved(120, 10, '310', '330')
        self.assertEqual(world.nearest_human(120, 310, 330).id, 10)
        world.member_moved(120, 10, 'bad', None)
        self.assertEqual(world.nearest_human(120, 310, 330).x, 310)
        world.member_removed(120, 9)
        world.member_removed(120, 9)
        self.assertEqual(world.bot_counts(), {120: 2})
        world.bot_left(1)
        self.assertEqual([m.id for m in world.bots_in(120)], [2])
        world.bot_entered(2, 330, [record(2)])
        self.assertEqual(world.members(120), [])
        self.assertFalse(world.has_humans(120))
        self.assertEqual(world.room_of(2), 330)
        world.bot_left(2)
        self.assertEqual(world.members(330), [])

    def test_snapshot_removes_stale_members(self):
        world = World()
        world.snapshot(110, [record(1), record(9)])
        world.snapshot(110, [record(1)])
        self.assertEqual([m.id for m in world.members(110)], [1])

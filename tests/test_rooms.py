"""Catalog provenance and bounds, including the deliberately pending rooms."""
import copy
import json
import random
import unittest

from bots.rooms import DATA_PATH, RoomError, load_catalog, parse_catalog


class RoomTests(unittest.TestCase):
    def test_catalog_has_only_the_three_existing_measured_rooms(self):
        catalog = load_catalog()
        self.assertEqual(catalog.measured_ids(), [110, 120, 330])
        self.assertEqual(len(catalog.rooms), 14)
        for room in catalog.rooms.values():
            self.assertTrue(room.source)
            self.assertTrue(room.spawn_source)
            self.assertTrue(room.allows(*room.spawn))
            for rect in list(room.walk) + [p.rect for p in room.pois.values()]:
                self.assertTrue(0 <= rect.min_x <= rect.max_x <= 1520)
                self.assertTrue(0 <= rect.min_y <= rect.max_y <= 960)
            if not room.measured:
                self.assertEqual(room.walk, ())
                self.assertEqual(room.pois, {})
                self.assertIsNone(room.walk_point(random.Random(5)))
                self.assertEqual(room.post(['stage'], random.Random(5)), (room.spawn, None))

    def test_bad_bounds_missing_provenance_and_guessed_pending_walks_are_rejected(self):
        document = json.loads(DATA_PATH.read_text())
        for key, value in [('measured', 'yes'), ('source', ''),
                           ('walk', [{'min_x': 0, 'max_x': 1521, 'min_y': 0, 'max_y': 1}]),
                           ('spawn', {'x': 10, 'y': 10})]:
            bad = copy.deepcopy(document)
            bad['rooms']['110'][key] = value
            with self.subTest(key=key), self.assertRaises(RoomError):
                parse_catalog(bad)
        pending = copy.deepcopy(document)
        pending['rooms']['121']['walk'] = document['rooms']['120']['walk']
        with self.assertRaises(RoomError):
            parse_catalog(pending)

    def test_random_points_and_booth_have_measured_provenance(self):
        room = load_catalog().require(120)
        rng = random.Random(7)
        for _ in range(100):
            self.assertTrue(room.allows(*room.walk_point(rng)))
        point, poi = room.post(['booth'], rng)
        self.assertEqual(point, (322, 168))
        self.assertTrue(poi.source)

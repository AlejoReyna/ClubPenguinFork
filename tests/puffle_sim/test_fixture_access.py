"""Host-account features must not authorize other fixture players."""
import unittest

from experiments.puffle_sim.run_houdini_fixture import human_account_indexes


class FixtureAccessTests(unittest.TestCase):
    def test_host_actions_and_spotify_are_owner_only(self):
        self.assertEqual(human_account_indexes(True, False, True), [3])
        self.assertEqual(human_account_indexes(True, True, False), [3])
        self.assertEqual(human_account_indexes(True, True, True), [3])
        self.assertEqual(human_account_indexes(True, False, False, True), [3])
        self.assertEqual(human_account_indexes(True, False, False, False, True), [3])

    def test_simulated_fixture_still_exercises_multiple_humans(self):
        self.assertEqual(human_account_indexes(False, False, False), [0, 1])
        self.assertEqual(human_account_indexes(True, False, False), [0, 1, 3])

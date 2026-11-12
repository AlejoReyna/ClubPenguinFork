"""Use the real Houdini model classes without a database or live accounts."""
from types import SimpleNamespace
import unittest

from houdini.data.pet import PenguinPuffle
from houdini.data.room import PenguinIglooRoom, Room
from experiments.puffle_sim.authority import select_owned_puffle, validate_binding
from experiments.puffle_sim.engine import Binding


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.room = PenguinIglooRoom(id=21, penguin_id=1)
        self.pets = {
            8: PenguinPuffle(id=8, penguin_id=1, puffle_id=1, backyard=False),
            4: PenguinPuffle(id=4, penguin_id=1, puffle_id=2, backyard=False),
            2: PenguinPuffle(id=2, penguin_id=99, puffle_id=2, backyard=False),
            3: PenguinPuffle(id=3, penguin_id=1, puffle_id=2, backyard=True),
            1: PenguinPuffle(id=1, penguin_id=1, puffle_id=42, backyard=False),
        }
        self.player = SimpleNamespace(id=1, room=self.room, joined_world=True, muted=False,
                                      is_legacy_client=True, puffles=self.pets, walking=None)
        self.player.server = SimpleNamespace(penguins_by_id={1: self.player})
        self.room.penguins_by_id[1] = self.player
        self.human = lambda _: True

    def test_owned_visible_selection_and_preference(self):
        self.assertEqual(select_owned_puffle(self.player, self.human), 4)
        self.assertEqual(select_owned_puffle(self.player, self.human, 8), 8)
        self.player.walking = 4
        self.assertEqual(select_owned_puffle(self.player, self.human), 8)

    def test_vanilla_client_with_a_classic_puffle_is_accepted(self):
        self.player.is_legacy_client = False
        self.player.is_vanilla_client = True
        self.assertEqual(select_owned_puffle(self.player, self.human), 4)

    def test_visitor_bot_and_mute_are_rejected(self):
        self.assertIsNone(select_owned_puffle(self.player, lambda _: False))
        self.player.muted = True
        self.assertIsNone(select_owned_puffle(self.player, self.human))
        self.player.muted = False
        self.room.penguin_id = 9
        self.assertIsNone(select_owned_puffle(self.player, self.human))

    def test_disconnected_or_replaced_session_is_rejected(self):
        self.player.server.penguins_by_id[1] = object()
        self.assertIsNone(select_owned_puffle(self.player, self.human))
        self.player.server.penguins_by_id[1] = self.player
        self.player.joined_world = False
        self.assertIsNone(select_owned_puffle(self.player, self.human))

    def test_binding_invalidated_by_room_or_puffle_change(self):
        binding = Binding(1, 21, 4)
        self.assertTrue(validate_binding(self.player, binding, self.human))
        self.player.walking = 4
        self.assertFalse(validate_binding(self.player, binding, self.human))
        self.player.room = Room(id=100)
        self.assertFalse(validate_binding(self.player, binding, self.human))

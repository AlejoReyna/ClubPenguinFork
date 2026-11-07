"""Houdini-side ownership checks for the puffle bridge; registers no handlers itself.

The caller must invoke these checks only for chat already accepted by Houdini,
with a trusted server-side human classification and explicit opt-in. It must
also manage session lifecycles and serialize validation with packet delivery.
"""
from houdini.data.pet import PenguinPuffle
from houdini.data.room import PenguinIglooRoom
from houdini.handlers.play.pet import LegacyPuffleIds

from experiments.puffle_sim.engine import select_puffle


def select_owned_puffle(player, human_authorized, preferred=None):
    """Return a visible legacy puffle id only when the server context qualifies."""
    room = player.room
    if (human_authorized(player) is not True or not player.joined_world or player.muted
            or not player.is_legacy_client or type(room) is not PenguinIglooRoom
            or room.penguin_id != player.id
            or room.penguins_by_id.get(player.id) is not player
            or player.server.penguins_by_id.get(player.id) is not player):
        return None
    visible = [pet.id for pet in player.puffles.values()
               if isinstance(pet, PenguinPuffle) and pet.penguin_id == player.id
               and pet.puffle_id in LegacyPuffleIds and not pet.backyard
               and pet.id != player.walking]
    return select_puffle(visible, preferred)


def validate_binding(player, binding, human_authorized, preferred=None):
    if player.id != binding.player or player.room is None:
        return False
    if getattr(player.room, 'id', None) != binding.igloo:
        return False
    return select_owned_puffle(player, human_authorized, preferred) == binding.puffle

"""Server-side bridge between accepted Houdini chat and the puffle engine.

production/puffle/world.py installs it in the world server, behind the
``puffle_enabled`` switch. The classifier must be a trusted server-side source
of human opt-in, not a name supplied by a client.
"""
from experiments.puffle_sim.authority import select_owned_puffle, validate_binding
from experiments.puffle_sim.client.protocol import puffle_talk_fields
from experiments.puffle_sim.engine import Binding, Engine


class PuffleBridge:
    def __init__(self, server, provider, human_authorized, *, max_enabled=32, **limits):
        if max_enabled < 1:
            raise ValueError('invalid enabled-player limit')
        self.server = server
        self.human_authorized = human_authorized
        self.max_enabled = max_enabled
        self.enabled = {}  # player id -> (live connection, preferred puffle id)
        self.bindings = {}  # player id -> (live connection, binding)
        self.delivered = {}  # per-visit bounded request ids
        self.engine = Engine(provider, self._valid, self._deliver, **limits)

    def enable(self, player, preferred=None):
        if player.server is not self.server or self.human_authorized(player) is not True:
            return False
        prior = self.enabled.get(player.id)
        if prior:
            self.disable(prior[0])
        elif len(self.enabled) >= self.max_enabled:
            return False
        self.enabled[player.id] = (player, preferred)
        return True

    def disable(self, player):
        self.leave(player)
        if self.enabled.get(player.id, (None,))[0] is player:
            self.enabled.pop(player.id, None)

    def leave(self, player):
        active = self.bindings.get(player.id)
        if active and active[0] is player:
            self.bindings.pop(player.id, None)
            self.delivered.pop(active[1].session, None)
            self.engine.cancel(active[1])

    def disconnect(self, player):
        self.disable(player)
        forget = getattr(self.engine.provider, 'forget', None)
        if forget is not None:
            forget(player.id)

    def observe_accepted(self, player, message):
        """Synchronous and bounded: called only after the normal chat checks."""
        setting = self.enabled.get(player.id)
        if not setting or setting[0] is not player:
            return False
        preferred = setting[1]
        pet_id = select_owned_puffle(player, self.human_authorized, preferred)
        active = self.bindings.get(player.id)
        if pet_id is None:
            self.leave(player)
            return False
        if active and (active[0] is not player or active[1].igloo != player.room.id
                       or active[1].puffle != pet_id):
            if active[0] is player:
                self.leave(player)
            else:
                return False
            active = None
        if not active:
            binding = Binding(player.id, player.room.id, pet_id)
            self.bindings[player.id] = (player, binding)
            if not self.engine.start(binding):
                self.bindings.pop(player.id, None)
                return False
        else:
            binding = active[1]
        return self.engine.observe(binding, message)

    def _valid(self, binding):
        active = self.bindings.get(binding.player)
        setting = self.enabled.get(binding.player)
        return bool(active and setting and active[1] == binding and active[0] is setting[0]
                    and validate_binding(active[0], binding, self.human_authorized, setting[1]))

    async def _deliver(self, binding, text, request_id):
        if not self._valid(binding):
            return
        seen = self.delivered.setdefault(binding.session, set())
        if request_id in seen or len(seen) >= 128:
            return
        seen.add(request_id)
        owner = self.bindings[binding.player][0]
        room = owner.room
        fields = puffle_talk_fields(room.external_id, binding.puffle, text)
        for recipient in tuple(room.penguins_by_id.values()):
            if not self._valid(binding):
                return
            if recipient.room is room and room.penguins_by_id.get(recipient.id) is recipient:
                await recipient.send_xt(*fields)

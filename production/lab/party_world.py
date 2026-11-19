"""Dev-only ``/party`` command on the world server."""
import asyncio
import logging
import os
import time

from .party_bus import default_bus

logger = logging.getLogger('lab.party')

DISCO_ROOM = 120
DISCO_BOT_CAP = 6
GUEST_COUNT = 5
# Lowercase usernames of bots managed by this lab stack (work + optional party guests).
_DEFAULT_BOTS = frozenset({
    'baristaia', 'djia', 'pizzeroia',
    'fiestalila', 'fiestasol', 'fiestamar', 'fiestaverde', 'fiestaglam',
})


def _allowed_players():
    raw = os.environ.get('LAB_PARTY_PLAYERS', '').strip()
    if not raw:
        return None
    return frozenset(int(x) for x in raw.split(',') if x.strip().isdigit())


def _bot_usernames():
    raw = os.environ.get('LAB_PARTY_BOT_USERNAMES', '')
    extra = {x.strip().lower() for x in raw.split(',') if x.strip()}
    return _DEFAULT_BOTS | extra


class PartyBridge:
    """Authorizes ``/party``, enforces disco capacity, and publishes scene state."""

    def __init__(self, server, bus=None, clock=time.monotonic):
        self.server = server
        self.bus = bus or default_bus(clock)
        self.clock = clock
        self._lock = asyncio.Lock()
        self._active = None  # scene_id str

    def enabled(self):
        return os.environ.get('LAB_PARTY_ENABLED', '').lower() in ('1', 'true', 'yes')

    def _bots_in_room(self, room):
        usernames = _bot_usernames()
        return [p for p in room.penguins_by_id.values()
                if getattr(p, 'username', '').lower() in usernames]

    def _capacity_ok(self, room):
        bots = self._bots_in_room(room)
        # Five guests must be able to stand in the room with whoever is already there.
        needed = GUEST_COUNT
        return len(bots) + needed <= DISCO_BOT_CAP

    async def try_chat(self, p, penguin_id, message):
        if not self.enabled():
            return False
        if penguin_id != p.id:
            return False
        normalized = message.strip()
        if normalized.casefold() != '/party':
            return False
        if getattr(p, 'username', '').lower() in _bot_usernames():
            return False
        if p.room is None or int(p.room.id) != DISCO_ROOM:
            await p.send_xt('mm', 'La fiesta solo funciona en la disco.', p.id)
            return True
        allowed = _allowed_players()
        if allowed is not None and p.id not in allowed:
            await p.send_xt('mm', 'No tienes permiso para invocar la fiesta.', p.id)
            return True
        if not self._capacity_ok(p.room):
            free = max(0, DISCO_BOT_CAP - len(self._bots_in_room(p.room)))
            await p.send_xt('mm',
                             f'No hay cupo en la pista ({free} plazas libres; se necesitan {GUEST_COUNT}).',
                             p.id)
            return True
        async with self._lock:
            existing = self.bus.read()
            if existing and existing.get('status') not in ('done', 'cancelled', 'failed'):
                await p.send_xt('mm', 'Ya hay una fiesta activa en la disco.', p.id)
                return True
            scene_id = f'{int(self.clock())}:{p.id}'
            document = {
                'scene_id': scene_id,
                'status': 'requested',
                'invoker_id': p.id,
                'invoker_nick': p.nickname,
                'room_id': DISCO_ROOM,
                'x': int(p.x),
                'y': int(p.y),
                'requested_at': self.clock(),
                'generation': int(existing.get('generation', 0)) + 1 if existing else 1,
            }
            self.bus.write(document)
            self._active = scene_id
        await p.send_xt('mm', '¡Invocaste a la fiesta! Las chicas están en camino.', p.id)
        logger.info('party requested by %s (%s) in room %s', p.nickname, p.id, DISCO_ROOM)
        return True

    async def player_left(self, p):
        if not self.enabled():
            return
        document = self.bus.read()
        if not document or document.get('invoker_id') != p.id:
            return
        if document.get('status') in ('done', 'cancelled', 'failed'):
            return
        async with self._lock:
            document = self.bus.read()
            if not document or document.get('invoker_id') != p.id:
                return
            document['status'] = 'cancelled'
            document['cancel_reason'] = 'invoker_left'
            document['cancelled_at'] = self.clock()
            self.bus.write(document)
            self._active = None

    def update_document(self, patch):
        """Bots service merges progress (status, dance, etc.)."""
        async def _apply():
            async with self._lock:
                document = self.bus.read()
                if document is None:
                    return None
                document.update(patch)
                self.bus.write(document)
                return document
        return _apply()


def install():
    """Hook message send and room removal when LAB_PARTY_ENABLED is set."""
    if os.environ.get('LAB_PARTY_ENABLED', '').lower() not in ('1', 'true', 'yes'):
        return
    from houdini.houdini import Houdini

    original_init = Houdini.__init__

    def initialize(self, config):
        original_init(self, config)
        if config.type == 'world':
            self.lab_party = PartyBridge(self)

    Houdini.__init__ = initialize

    import houdini.data.room as room_mod

    _remove = room_mod.RoomMixin.remove_penguin

    async def remove_penguin(self, p):
        bridge = getattr(p.server, 'lab_party', None)
        if bridge is not None:
            await bridge.player_left(p)
        return await _remove(self, p)

    room_mod.RoomMixin.remove_penguin = remove_penguin
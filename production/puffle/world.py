"""Houdini integration using authenticated, per-player device pairing."""
import asyncio
import os
from pathlib import Path
import re

from experiments.puffle_sim.authority import select_owned_puffle
from experiments.puffle_sim.bridge import PuffleBridge
from .http import request


class DeviceProvider:
    def __init__(self, url, secret):
        self.url, self.secret = url, secret

    async def call(self, path, player, **data):
        return await request(self.url, '/internal/' + path, self.secret, dict(data, player=player))

    async def evaluate_for(self, player, context, valid):
        job = None
        try:
            if not valid():
                return False, ''
            result = await self.call('submit', player, context=[list(item) for item in context])
            job = result['job']
            while valid():
                result = await self.call('result', player, job=job)
                if result['result'] is not None:
                    answer = result['result']
                    return answer['reply'], answer['text']
                await asyncio.sleep(0.25)
            return False, ''
        finally:
            if job:
                try:
                    await self.call('cancel', player, job=job)
                except (ConnectionError, OSError, TimeoutError, ValueError):
                    pass


class ProductionBridge(PuffleBridge):
    def __init__(self, server, provider, denied=(), allowed=()):
        self.denied, self.allowed = frozenset(denied), frozenset(allowed)
        self.pending = {}
        self.controls = set()
        super().__init__(server, provider, self.authorized, deadline=27, max_parallel=8,
                         max_sessions=32, min_interval=2, max_pending=8)

    def authorized(self, player):
        return (getattr(player, 'username', '').casefold() not in self.denied
                and not getattr(player, 'is_bot', False)
                and (not self.allowed or player.id in self.allowed))

    async def control(self, player, message):
        """Control messages are private, after mute/filter/identity validation."""
        normalized = message.strip()
        pairing = re.fullmatch(r'puffle conectar ([A-Fa-f0-9]{10})', normalized)
        revoke = normalized.casefold() == 'puffle desconectar'
        if not pairing and not revoke:
            return False
        if select_owned_puffle(player, self.authorized) is None:
            return True
        if player.id in self.controls or len(self.controls) >= 32:
            return True
        self.controls.add(player.id)
        try:
            if revoke:
                self.disable(player)
                await self.engine.provider.call('revoke', player.id)
                text = 'Puffle desconectado.'
            else:
                await self.engine.provider.call('pair', player.id, code=pairing[1].upper(),
                                                nickname=player.nickname)
                text = 'Confirma tu pingüino en tu computadora.'
            pet = select_owned_puffle(player, self.authorized)
            if pet is not None:
                await player.send_xt('puffletalk', player.room.external_id, pet, text)
        except (ConnectionError, OSError, TimeoutError, ValueError):
            pass  # fixed silence; never include server/provider error details
        finally:
            self.controls.discard(player.id)
        return True

    def observe_accepted(self, player, message):
        if select_owned_puffle(player, self.authorized) is None:
            self.leave(player)
            return False
        if player.id in self.pending or len(self.pending) >= 32:
            return False
        room = player.room

        async def observe():
            try:
                status = await self.engine.provider.call('status', player.id)
                if player.room is room and status['paired'] and status['online']:
                    if self.enabled.get(player.id, (None,))[0] is not player:
                        self.enable(player)
                    super(ProductionBridge, self).observe_accepted(player, message)
                else:
                    self.disable(player)
            except (ConnectionError, OSError, TimeoutError, ValueError, KeyError):
                self.disable(player)
            finally:
                self.pending.pop(player.id, None)

        self.pending[player.id] = asyncio.create_task(observe())
        return True

    def leave(self, player):
        task = self.pending.get(player.id)
        if task is not None and task is not asyncio.current_task():
            self.pending.pop(player.id, None)
            task.cancel()
        super().leave(player)


def install():
    """Called once by the runtime, before world bootstrap; disabled by default."""
    if os.environ.get('PUFFLE_ENABLED') != 'true':
        return
    from houdini.houdini import Houdini
    secret = Path(os.environ['PUFFLE_SECRET_FILE']).read_text().strip()
    if len(secret) < 32:
        raise ValueError('invalid puffle service secret')
    url = os.environ.get('PUFFLE_BROKER_URL', 'http://puffle:9103')
    denied = {x.strip().casefold() for x in os.environ.get(
        'PUFFLE_DENIED_USERNAMES', 'baristaia,djia,pizzeroia').split(',') if x.strip()}
    allowed = {int(x) for x in os.environ.get('PUFFLE_ALLOWED_PLAYERS', '').split(',') if x.strip()}
    original = Houdini.__init__

    def initialize(self, config):
        original(self, config)
        if config.type == 'world':
            self.puffle_conversation = ProductionBridge(self, DeviceProvider(url, secret), denied, allowed)

    Houdini.__init__ = initialize

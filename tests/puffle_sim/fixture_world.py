"""Test-only world entrypoint. Never use with a real database or players."""
import os
import runpy
import sys

if os.environ.get('PUFFLE_FIXTURE_ISOLATED') != '1':
    raise SystemExit('Disposable fixture flag required')

sys.path[:0] = ['/app/houdini', '/src']
from houdini.houdini import Houdini
from experiments.puffle_sim.bridge import PuffleBridge
from experiments.puffle_sim.provider import SimulatedProcess

allowed = frozenset(int(value) for value in os.environ['PUFFLE_FIXTURE_HUMANS'].split(','))


# Spanish sample with accents and ñ for the visual bubble-length check.
LENGTH_SAMPLE = ('¿Sabías que el pingüino más pequeño camina sobre la nieve? Mañana jugaré '
                 'contigo junto al iglú; también podemos bailar, pescar y comer pizza caliente. '
                 'Después buscaremos monedas en la montaña y saludaremos a todos los compañeros.')


def length_probe(context):
    """Test-only: an owner line 'largo N?' asks for the first N UTF-8 bytes of the sample."""
    latest = next((text for role, text in reversed(context) if role == 'player'), '')
    words = latest.replace('?', ' ').split()
    if len(words) == 2 and words[0] == 'largo' and words[1].isdigit():
        size = int(words[1])
        return True, LENGTH_SAMPLE.encode()[:size].decode(errors='ignore').strip()
    return None


class SlowSimulation:
    async def evaluate(self, context):
        import asyncio
        await asyncio.sleep(1)
        return length_probe(context) or await SimulatedProcess().evaluate(context)


class FixtureBridge(PuffleBridge):
    def observe_accepted(self, player, message):
        if player.id in allowed and player.id not in self.enabled:
            self.enable(player)
        return super().observe_accepted(player, message)


original_init = Houdini.__init__


def fixture_provider():
    # Explicit opt-in; the default remains the offline simulator.
    if os.environ.get('PUFFLE_FIXTURE_PROVIDER', 'simulator') == 'relay':
        from experiments.puffle_sim.host_relay import RelayClient
        with open('/run/puffle-relay/secret') as handle:
            secret = handle.read().strip()
        return RelayClient('relay', 8790, secret, timeout=55), 55
    if os.environ.get('PUFFLE_FIXTURE_PROVIDER') == 'spotify-sim':
        # Simulated per-player Spotify accounts; nothing leaves the container.
        from experiments.puffle_sim.spotify import SimulatedSpotify, SpotifyAgent
        accounts = {pid: SimulatedSpotify() for pid in allowed}
        return SpotifyAgent(SlowSimulation(), accounts.get, min_interval=0.5), 4
    return SlowSimulation(), 4


def fixture_init(self, config):
    original_init(self, config)
    if config.type == 'world':
        provider, deadline = fixture_provider()
        self.puffle_conversation = FixtureBridge(
            self, provider, lambda player: player.id in allowed,
            batch_seconds=0.05, min_interval=0.1, deadline=deadline)


Houdini.__init__ = fixture_init
runpy.run_path('/app/run.py', run_name='__main__')

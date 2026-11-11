"""Container-only probe: actual BotManager sessions and synthetic AS2 human."""
import asyncio
import datetime as dt
import json
import sys

# The installed image is the subject, never a host package override.
sys.path.insert(0, '/app')
from bots import as2
from bots.config import build_config
from bots.manager import BotManager
from bots.metrics import Registry
from bots.travel import TravelDenied


async def until(check, message, seconds=60):
    async def poll():
        while not check():  # noqa: ASYNC110 - real wire state has no completion event
            await asyncio.sleep(.05)
    try:
        await asyncio.wait_for(poll(), seconds)
    except TimeoutError:
        raise AssertionError(message) from None


async def human_login(identity):
    conn = await as2.AS2Connection.connect('login', 6112)
    try:
        rnd = await conn.handshake(10)
        pid, key = (await conn.login_legacy(identity['username'],
                    as2.login_server_password(identity['password'], rnd), 10))[:2]
    finally:
        await conn.close()
    conn = await as2.AS2Connection.connect('glaciar', 9876)
    rnd = await conn.handshake(10)
    await conn.login_legacy(identity['username'], as2.world_login_password(key, rnd), 10)
    await conn.join_world(int(pid), key, 10)
    await conn.join_room(120, 10, 330, 250)
    return conn, int(pid)


async def main(identities):
    document = {
        'bots': identities[:-1],
        'server': {'login_host': 'login', 'world_host': 'glaciar'},
        'provider': {'mode': 'simulated'},
        'service': {'startup_stagger_min': .1, 'startup_stagger_max': .2, 'life_seed': 42},
        'dialogue': {'enabled': True, 'greeting_chance': 0, 'room_gap': 0,
                     'typing_cps': 0, 'scene_min_delay': 3600, 'scene_max_delay': 3600},
        'policy': {'player_cooldown': 0, 'bot_min_gap': 0},
        'life': {'enabled': True, 'autonomous': False, 'events': False,
                 'visit_seconds': 120, 'departure_min': 0, 'departure_max': 0,
                 'tick_seconds': .1},
        'behavior': {'life_min_delay': 60, 'life_max_delay': 60},
    }
    registry = Registry()
    manager = BotManager(build_config(document, {'BOTS_CAST_DIR': '/cast'}), registry)
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    human = None
    try:
        await until(lambda: manager.sessions_in_room() == 8, 'Eight real sessions did not connect')
        sessions = {s.bot.username: s for s in manager.sessions}
        lola, tito = sessions['lolaritmo'], sessions['titogroove']
        travel = manager.director.travel
        for s in manager.sessions:
            assert s.room_id == s.bot.room
        human, pid = await human_login(identities[-1])

        async def frame(op):
            async def read():
                while True:
                    raw = await human.read_line(5)
                    if raw.startswith('%'):
                        value = as2.Frame.parse(raw)
                        if value.op == op:
                            return value
            return await asyncio.wait_for(read(), 10)

        async def open_map():
            await human.send_line('%xt%s%g#gr%-1%')
            return (await frame('gr')).args

        if '--restart' in sys.argv:
            assert not travel.leases and not travel.reservations
            assert not any('|' in value for value in await open_map()), 'Map retained a dead host'
            assert 'bots_provider_requests_total{' not in registry.render()
            print('PASS: abrupt process restart: eight home rooms, empty leases/map, zero provider HTTP.', flush=True)
            return

        await until(lambda: manager.world.has_humans(120), 'Human missing from world model')
        # Both seeded outcomes repeat with identical day, host and state.
        now = dt.datetime(2026, 11, 11, 3, tzinfo=dt.timezone.utc)
        lola.behavior.life.needs.update(energy=1, social=0)
        outcomes = set()
        for host in range(100, 200):
            first = travel._guest_facts(lola, now, f'p:{host}', (), True)
            assert first == travel._guest_facts(lola, now, f'p:{host}', (), True), 'Seeded decision changed'
            outcomes.add(first[0])
        assert outcomes == {'accept', 'decline'}
        lola.behavior.life.needs['energy'] = .1
        assert travel._guest_facts(lola, now, 'p:100', (), True) == ('decline', 'low_energy')
        lola.behavior.life.needs['energy'] = 1
        print('PASS: fixed seed/day/pairs repeat accept and decline; low energy refuses.', flush=True)

        for name in ('peperina', 'pacopregon', 'baristaia', 'pizzeroia', 'djia'):
            s = sessions[name]
            assert s.bot.role in ('helper', 'guide')
            assert await travel.answer_human_igloo(s, pid, 'yours', now) == 'skip'
            try:
                travel.reserve_igloo(s)
            except TravelDenied as error:
                assert error.reason == 'role'
            else:
                raise AssertionError('Role entered an igloo')
            if s.behavior.life:
                assert not await travel.consider_igloo(s.behavior.life, now)
            assert s.room_id == s.bot.room and not s.igloo_opened
        print('PASS: all five helper/guide cards skip invitations and cannot reserve an igloo.', flush=True)

        ticket = travel.reserve_igloo(lola)
        try:
            travel.reserve_igloo(tito)
        except TravelDenied as error:
            assert error.reason == 'minimum'
        else:
            raise AssertionError('Concurrent departure broke room minimum')
        assert await travel.perform(ticket), 'Real igloo join failed'
        assert lola.room_id == as2.igloo_external_id(lola.player_id)
        assert lola.behavior.life.position == (380, 320)
        assert any(value.startswith(str(lola.player_id) + '|') for value in await open_map())
        await asyncio.sleep(1.1)
        await human.join_player_igloo(lola.player_id, 10)
        # Roster from real Houdini observes the fixed post; never treats grs serial as room id.
        await human.send_line('%xt%s%j#grs%-1%')
        roster = await frame('grs')
        entries = [v.split('|') for v in roster.args if '|' in v]
        assert any(v[0] == str(lola.player_id) and v[12:14] == ['380', '320'] for v in entries)
        assert not await travel.consider_igloo(lola.behavior.life, now), 'Nested invitation'
        travel.leases[lola.index].expires = travel.clock()
        await until(lambda: lola.room_id == lola.bot.room and lola.index not in travel.leases,
                    'Lease did not return home')
        assert not lola.igloo_opened
        assert not any(value.startswith(str(lola.player_id) + '|') for value in await open_map())
        print('PASS: real j#jp, fixed 380,320, open map, human visit, lease home, closed map; no nested invite/minimum breach.', flush=True)

        await asyncio.sleep(1.1)
        await human.join_room(120, 10, 330, 250)
        await until(lambda: manager.world.human_room(pid) == 120, 'Human did not return')
        # Low energy fixes a local decline through actual chat -> director.
        lola.behavior.life.needs['energy'] = .1
        await human.send_chat(pid, 'Lola vamos a tu iglu')
        answer = await frame('sm')
        while answer.args[0] != str(lola.player_id):
            answer = await frame('sm')
        await until(lambda: bool(travel._recent_pairs), 'Chat did not reach invitation decision')
        assert lola.room_id == lola.bot.room and not travel.leases
        print('PASS: synthetic human chat reaches the director and gets a local igloo decline.', flush=True)
        assert 'bots_provider_requests_total{' not in registry.render()
        # Disconnect the synthetic human before an independent resident's visit.
        await human.close()
        human = None
        await asyncio.sleep(1.1)
        assert await travel.perform(travel.reserve_igloo(tito)), 'Mid-lease visit failed'
        assert travel.leases and tito.igloo_opened
        print('PASS: zero provider HTTP in simulated mode on the internal network.', flush=True)
        print('READY_RESTART', flush=True)
        await asyncio.Event().wait()
    finally:
        if human:
            await human.close()
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    with open('/identities.json') as source:
        asyncio.run(main(json.load(source)))

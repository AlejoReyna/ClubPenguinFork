"""Synthetic AS2 clients for an internal, disposable integration stack."""
import asyncio
import json
import sys

from bot_as2 import AS2Connection, Frame, login_server_password, world_login_password


async def login(account):
    login_conn = await AS2Connection.connect('login', 6112)
    try:
        key = await login_conn.handshake(5)
        fields = await login_conn.login_legacy(account['name'],
                                                login_server_password(account['password'], key), 5)
    finally:
        await login_conn.close()
    pid, login_key = int(fields[0]), fields[1]
    assert pid == account['id']
    world = await AS2Connection.connect('glaciar', 9876)
    key = await world.handshake(5)
    await world.login_legacy(account['name'], world_login_password(login_key, key), 5)
    await world.join_world(pid, login_key, 5)
    await world.join_room(100, 5)
    return world


async def read_for(conn, duration):
    result = []
    end = asyncio.get_running_loop().time() + duration
    while (remaining := end - asyncio.get_running_loop().time()) > 0:
        try:
            raw = await conn.read_line(remaining)
        except asyncio.TimeoutError:
            break
        if raw.startswith('%xt%'):
            frame = Frame.parse(raw)
            if frame.as_error():
                raise AssertionError('Synthetic game error')
            result.append(frame)
    return result


async def join_igloo(conn, owner):
    await conn.send_line(f'%xt%s%j#jp%-1%{1000 + owner}%')
    end = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < end:
        frame = Frame.parse(await conn.read_line(5))
        if frame.op == 'jr' and frame.args and frame.args[0] == str(2000 + owner):
            return
        if frame.as_error():
            raise AssertionError('Cannot join synthetic igloo')
    raise AssertionError('Igloo join timed out')


async def run(doc):
    accounts = doc['accounts']
    worlds = []
    try:
        for account in accounts:
            worlds.append(await login(account))
        a, visitor, bot = worlds
        owner_id, visitor_id, bot_id = [x['id'] for x in accounts]
        await join_igloo(a, owner_id)
        await join_igloo(visitor, owner_id)
        await join_igloo(bot, bot_id)
        await read_for(a, 0.05)
        await read_for(visitor, 0.05)
        await read_for(bot, 0.05)

        await visitor.send_chat(visitor_id, 'visitante?')
        await bot.send_chat(bot_id, 'bot?')
        quiet = await asyncio.gather(read_for(a, 1.3), read_for(visitor, 1.3), read_for(bot, 1.3))
        assert not any(f.op == 'puffletalk' for frames in quiet for f in frames)

        await a.send_chat(owner_id, 'jugamos?')
        await a.send_move(300, 250)
        await a.send_heartbeat()
        owner_frames, visitor_frames, bot_frames = await asyncio.gather(
            read_for(a, 1.6), read_for(visitor, 1.6), read_for(bot, 1.6))
        ops = [f.op for f in owner_frames]
        assert 'puffletalk' in ops and 'sp' in ops and 'h' in ops
        assert ops.index('sp') < ops.index('puffletalk') and ops.index('h') < ops.index('puffletalk')
        owner_talk = [f.args for f in owner_frames if f.op == 'puffletalk']
        guest_talk = [f.args for f in visitor_frames if f.op == 'puffletalk']
        assert owner_talk == guest_talk == [[str(2000 + owner_id), str(accounts[0]['pet']), '¡Hola! ¿Jugamos?']]
        assert not any(f.op == 'puffletalk' for f in bot_frames)

        if doc.get('spotify'):
            # Visitor tries to drive the owner's (simulated) Spotify: nothing happens.
            await visitor.send_chat(visitor_id, 'pon despacito en spotify')
            quiet = await asyncio.gather(read_for(a, 1.3), read_for(visitor, 1.3))
            assert not any(f.op == 'puffletalk' for frames in quiet for f in frames)
            pet = str(accounts[0]['pet'])
            for command, reply in (('pon despacito en spotify', 'Puse «Despacito» (simulado).'),
                                   ('siguiente', 'Listo, siguiente canción.')):
                await a.send_chat(owner_id, command)
                got = await asyncio.gather(read_for(a, 1.5), read_for(visitor, 1.5))
                talks = [[f.args for f in frames if f.op == 'puffletalk'] for frames in got]
                assert talks == [[[str(2000 + owner_id), pet, reply]]] * 2, talks
            print('PASS: simulated Spotify: visitor command ignored; owner play/next confirmed in the bubble for owner and visitor.')

        await a.send_chat(owner_id, 'respuesta tardia?')
        await asyncio.sleep(0.15)
        await a.join_room(100, 5)
        later = await asyncio.gather(read_for(visitor, 1.4), read_for(a, 1.4))
        assert not any(f.op == 'puffletalk' for frames in later for f in frames)

        await join_igloo(visitor, visitor_id)
        await visitor.send_chat(visitor_id, 'otra cuenta?')
        frames = await read_for(visitor, 1.6)
        talks = [f.args for f in frames if f.op == 'puffletalk']
        assert talks == [[str(2000 + visitor_id), str(accounts[1]['pet']), '¡Hola! ¿Jugamos?']]
        print('PASS: real Houdini, isolated PostgreSQL/Redis and simulated worker: owner-only chat, deterministic pet, room recipients, slow inference, movement, heartbeat, exit cancellation and second player.')
    finally:
        for conn in worlds:
            await conn.close()


if __name__ == '__main__':
    asyncio.run(run(json.load(sys.stdin)))

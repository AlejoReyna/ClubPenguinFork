"""Executed inside the disposable dash container by puffle_integration.py."""
import asyncio
import json
import sys

from production.puffle.http import request
from production.puffle.companion import run_job


async def journey(accounts):
    url = 'http://puffle:9103'
    login, join, read = ns['login'], ns['join_igloo'], ns['read_for']
    a, b, guest = [await login(x) for x in accounts]
    devices = []
    try:
        await join(a, accounts[0]['id'])
        await join(b, accounts[1]['id'])
        await join(guest, accounts[0]['id'])
        for conn, account in ((a, accounts[0]), (b, accounts[1])):
            device = await request(url, '/puffle/api/pair')
            await conn.send_chat(account['id'], 'puffle conectar ' + device['code'])
            for _ in range(30):
                status = await request(url, '/puffle/api/pair-status', device['token'])
                if status['player'] is not None:
                    break
                await asyncio.sleep(0.1)
            assert status['player'] == account['id']
            await request(url, '/puffle/api/confirm', device['token'], {'player': account['id']})
            await request(url, '/puffle/api/poll', device['token'])
            devices.append({'url': url, 'token': device['token']})
        guest_frames = await read(guest, 0.1)
        assert not any('puffle conectar' in str(f.args) for f in guest_frames), 'pairing code leaked'
        for conn in (a, b):
            await read(conn, 0.1)
        # Pairing uses the real chat handler too. Respect its 500 ms cooldown;
        # a fast CI host otherwise sends the first normal line too early.
        await asyncio.sleep(0.55)

        class Answer:
            def __init__(self, text):
                self.text = text
            async def evaluate(self, context):
                assert context[-1][1] in ('hola uno?', 'hola dos?')
                await asyncio.sleep(0.1)
                return True, self.text

        tasks = []
        for conn, account, text, device, reply in (
                (a, accounts[0], 'hola uno?', devices[0], 'Respuesta uno'),
                (b, accounts[1], 'hola dos?', devices[1], 'Respuesta dos')):
            await request(url, '/puffle/api/poll', device['token'])
            await conn.send_chat(account['id'], text)
            await conn.send_move(300, 250)
            await conn.send_heartbeat()
            for _ in range(50):
                job = await request(url, '/puffle/api/poll', device['token'])
                if job['job']:
                    break
                await asyncio.sleep(0.1)
            assert job['job'], 'production world did not submit a job'
            tasks.append(asyncio.create_task(run_job(device, job, Answer(reply))))
        await asyncio.gather(*tasks)
        frames = await asyncio.gather(read(a, 1), read(b, 1), read(guest, 1))
        talks = [[f.args[-1] for f in group if f.op == 'puffletalk'] for group in frames]
        assert talks == [['Respuesta uno'], ['Respuesta dos'], ['Respuesta uno']], talks
        assert all(any(f.op == 'sp' for f in group) and any(f.op == 'h' for f in group) for group in frames[:2])

        # A visitor cannot use the owner's pairing, even with a valid code.
        rejected = await request(url, '/puffle/api/pair')
        await guest.send_chat(accounts[2]['id'], 'puffle conectar ' + rejected['code'])
        await asyncio.sleep(0.2)
        assert (await request(url, '/puffle/api/pair-status', rejected['token']))['player'] is None

        # Leaving the igloo cancels the remote job, so no late result is usable.
        await request(url, '/puffle/api/poll', devices[0]['token'])
        await asyncio.sleep(2)
        await a.send_chat(accounts[0]['id'], 'tarde?')
        for _ in range(40):
            job = await request(url, '/puffle/api/poll', devices[0]['token'])
            if job['job']:
                break
            await asyncio.sleep(0.1)
        assert job['job']
        await a.join_room(100, 5)
        await asyncio.sleep(0.5)
        try:
            await request(url, '/puffle/api/result', devices[0]['token'],
                          {'job': job['job'], 'reply': True, 'text': 'late'})
        except ConnectionError:
            pass
        else:
            raise AssertionError('Late result was accepted')
        await request(url, '/puffle/api/disconnect', devices[1]['token'])
        try:
            await request(url, '/puffle/api/poll', devices[1]['token'])
        except ConnectionError:
            pass
        else:
            raise AssertionError('Revoked device was accepted')
        print('PASS: production Houdini pairing, private codes, two isolated devices, companion replies, visitor visibility, movement/heartbeat, exit cancellation and revocation.')
    finally:
        for conn in (a, b, guest):
            await conn.close()


asyncio.run(journey(json.loads(sys.argv[1])))

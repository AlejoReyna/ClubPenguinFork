"""Phase 3 on disposable real AS2; simulated providers, no account/cluster access."""
import json
import time


def check_life(root, docker, exec_script, dash, network, label, image,
               service, document, config_path, cast_path, observers, ids):
    from bots_image import verify_bots_image
    verify_bots_image(root, docker, image)
    docker(['rm', '-f', service])
    document['provider']['mode'] = 'simulated'
    document['service'] = {'startup_stagger_min': .1, 'startup_stagger_max': .2}
    document['dialogue'].update(room_gap=.7)
    document['policy'].update(player_cooldown=0, bot_min_gap=0, requests_per_minute=100)
    document['life'] = {'enabled': True, 'autonomous': False, 'events': False,
                        'visit_seconds': 18, 'follow_seconds': 30, 'departure_min': .6,
                        'departure_max': .8, 'tick_seconds': .2}
    document['behavior'].update(life_min_delay=.5, life_max_delay=1)
    config_path.chmod(0o600)
    config_path.write_text(json.dumps(document))
    config_path.chmod(0o444)
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', network, '--network-alias', 'bots', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--mount', f'type=bind,source={config_path},target=/etc/bots/config.json,readonly',
            '--mount', f'type=bind,source={cast_path},target=/etc/bots-cast,readonly',
            '-e', 'BOTS_PROVIDER_MODE=simulated', '-e', 'BOTS_CAST_DIR=/etc/bots-cast', image])
    fetch = 'import urllib.request\nprint(urllib.request.urlopen("http://bots:9102/metrics",timeout=3).read().decode())'
    for _ in range(180):
        try:
            metrics = exec_script(dash, fetch).stdout
            online = [line for line in metrics.splitlines() if line.startswith('bots_connected{')]
            if len(online) == 26 and all(line.endswith(' 1') for line in online):
                break
        except Exception:
            pass
        time.sleep(.5)
    else:
        raise RuntimeError('Phase 3 fixture did not connect 26 bodies')
    protocol = (root / 'containers/bots/bots/as2.py').read_text()
    report = docker(['exec', '-i', dash, 'python', '-I', '-c', protocol + CLIENTS],
                    input=json.dumps({'humans': observers[:2], 'ids': ids}))
    if json.loads(report.stdout) != {'checks': 5}:
        raise RuntimeError('Phase 3 protocol checks did not finish')
    metrics = exec_script(dash, fetch).stdout
    if any(line.startswith('bots_provider_requests_total{') for line in metrics.splitlines()):
        raise RuntimeError('Simulated life issued provider HTTP requests')
    if 'bots_travel_total{result="ok"}' not in metrics or 'bots_travel_denied_total{reason="minimum"}' not in metrics:
        raise RuntimeError('Phase 3 did not expose successful travel and occupancy refusal')
    print('PASS: Phase 3 on real disposable AS2: invitation to 330, occupancy denial in 120, '
          'home return, following into pending 121 at its exact spawn, local Bolazo snowball; zero provider HTTP.')


CLIENTS = r'''
import sys,json
async def main():
    doc=json.load(sys.stdin)
    connections=[]
    heartbeats=[]
    async def keepalive(conn):
        while True:
            await asyncio.sleep(60)
            await conn.send_heartbeat()
    async def login(identity):
        conn=await AS2Connection.connect('login',6112)
        try:
            rndk=await conn.handshake(10)
            fields=await conn.login_legacy(identity['username'],login_server_password(identity['password'],rndk),10)
        finally: await conn.close()
        pid,key=fields[:2]
        conn=await AS2Connection.connect('glaciar',9876)
        connections.append(conn)
        rndk=await conn.handshake(10)
        await conn.login_legacy(identity['username'],world_login_password(key,rndk),10)
        await conn.join_world(int(pid),key,10)
        await conn.join_room(120,10,330,250)
        await conn.send_heartbeat()
        heartbeats.append(asyncio.create_task(keepalive(conn)))
        return conn,int(pid)
    async def reply(client,sender,message):
        conn,pid=client
        await conn.send_chat(pid,message)
        until=asyncio.get_running_loop().time()+6
        while asyncio.get_running_loop().time()<until:
            try: raw=await conn.read_line(.5)
            except TimeoutError: continue
            if raw.startswith('%'):
                frame=Frame.parse(raw)
                if frame.op=='sm' and frame.args[0]==str(sender): return frame.args[1]
        raise AssertionError('Missing travel acknowledgement')
    async def roster(conn):
        await conn.send_line('%xt%s%j#grs%-1%')
        while True:
            raw=await conn.read_line(5)
            if raw.startswith('%'):
                frame=Frame.parse(raw)
                if frame.op=='grs':
                    return {int(p[0]):p for p in (v.split('|') for v in frame.args[1:]) if len(p)>13 and p[0].isdigit()}
    async def wait_bot(conn,player,present=True,point=None,timeout=12):
        until=asyncio.get_running_loop().time()+timeout
        while asyncio.get_running_loop().time()<until:
            members=await roster(conn)
            if (player in members)==present:
                if not present or point is None or tuple(map(int,members[player][12:14]))==point:
                    return members
            await asyncio.sleep(.7)
        raise AssertionError('Bot did not reach the expected roster/post')
    try:
        first,second=await login(doc['humans'][0]),await login(doc['humans'][1])
        await asyncio.sleep(.7)  # both clients must clear their initial room-join cooldown
        lola,tito=doc['ids']['lolaritmo'],doc['ids']['titogroove']
        answer=await reply(first,lola,'Lola, vamos a la pizzeria')
        assert 'Intentare' in answer, 'Invitation was not accepted'
        await first[0].join_room(330,5,400,278)
        await wait_bot(first[0],lola)
        denied=await reply(second,tito,'Tito vamos al Lounge')
        assert 'me quedo' in denied, 'Second departure should violate the disco minimum'
        members=await roster(second[0])
        assert {doc['ids']['djia'],tito,doc['ids']['mambo']}<=set(members), 'Disco lost its minimum'
        # Invitation leases are shortened only in this synthetic fixture.
        await wait_bot(second[0],lola,timeout=35)
        await asyncio.sleep(.7)
        await second[0].join_room(230,5,400,240)
        ventisca=doc['ids']['ventisca']
        following=await reply(second,ventisca,'Ventisca sigueme')
        assert 'Te sigo' in following
        await asyncio.sleep(.7)
        await second[0].join_room(121,5,560,320)
        await wait_bot(second[0],ventisca,point=(560,320))
        await asyncio.sleep(.7)
        await second[0].join_room(230,5,400,240)
        await wait_bot(second[0],ventisca)
        # A human throws at Bolazo's documented post; the reply targets only
        # that observed human position, never another bot or arbitrary code.
        await asyncio.sleep(.7)
        await first[0].join_room(230,5,400,240)
        await first[0].send_move(400,240)
        await asyncio.sleep(.3)
        await first[0].send_snowball(380,220)
        until=asyncio.get_running_loop().time()+6
        returned=False
        while asyncio.get_running_loop().time()<until:
            try: raw=await first[0].read_line(.5)
            except TimeoutError: continue
            if raw.startswith('%'):
                frame=Frame.parse(raw)
                if frame.op=='sb' and frame.args[0]==str(doc['ids']['bolazo']):
                    assert list(map(int,frame.args[1:3]))==[400,240]
                    returned=True
                    break
        assert returned, 'Bolazo did not return the snowball locally'
        print(json.dumps({'checks':5}))
    finally:
        for task in heartbeats: task.cancel()
        await asyncio.gather(*heartbeats,return_exceptions=True)
        await asyncio.gather(*(c.close() for c in connections),return_exceptions=True)
asyncio.run(main())
'''

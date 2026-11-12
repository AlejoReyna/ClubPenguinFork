"""Phase 4 on disposable AS2: SQLite notes, simulated summaries, restart and forget."""
import json
import os
import time


def check_recall(root, docker, exec_script, dash, network, label, image,
                 service, document, config_path, cast_path, observers, ids, temp):
    from bots_image import verify_bots_image
    verify_bots_image(root, docker, image)
    docker(['rm', '-f', service])
    pdata = temp / 'recall-pvc'
    pdata.mkdir(mode=0o777, exist_ok=True)
    os.chmod(pdata, 0o777)
    document['provider']['mode'] = 'simulated'
    document['service'] = {'startup_stagger_min': 0.1, 'startup_stagger_max': 0.2}
    document['dialogue'] = {'enabled': True, 'greeting_chance': 0,
                            'scene_min_delay': 3600, 'scene_max_delay': 3600,
                            'room_gap': 0.7, 'hello_chance': 1, 'question_chance': 0,
                            'other_chance': 0}
    document['policy'].update(player_cooldown=0, bot_min_gap=0, requests_per_minute=100)
    document['recall'] = {'enabled': True, 'min_turns': 3, 'ttl_days': 30}
    document.pop('life', None)
    config_path.chmod(0o600)
    config_path.write_text(json.dumps(document))
    config_path.chmod(0o444)
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', network, '--network-alias', 'bots', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user', '10001:10001',
            '--mount', f'type=bind,source={config_path},target=/etc/bots/config.json,readonly',
            '--mount', f'type=bind,source={cast_path},target=/etc/bots-cast,readonly',
            '--mount', f'type=bind,source={pdata},target=/var/lib/bots',
            '-e', 'BOTS_PROVIDER_MODE=simulated', '-e', 'BOTS_CAST_DIR=/etc/bots-cast',
            '-e', 'BOTS_RECALL_PATH=/var/lib/bots/recall.sqlite', image])
    fetch = 'import urllib.request\nprint(urllib.request.urlopen("http://bots:9102/metrics",timeout=3).read().decode())'

    def wait_all():
        for _ in range(180):
            try:
                text = exec_script(dash, fetch).stdout
                online = [line for line in text.splitlines() if line.startswith('bots_connected{')]
                if len(online) == 26 and all(line.endswith(' 1') for line in online):
                    return text
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError('Phase 4 fixture did not connect 26 bodies')

    wait_all()
    lola_index = next(i for i, bot in enumerate(document['bots']) if bot['username'] == 'lolaritmo')
    protocol = (root / 'containers/bots/bots/as2.py').read_text()
    payload = json.dumps({'human': observers[0], 'lola': ids['lolaritmo'],
                          'lola_index': lola_index})
    report = docker(['exec', '-i', dash, 'python', '-I', '-c', protocol + CLIENTS], input=payload)
    body = json.loads(report.stdout)
    if body.get('turns') != 3 or not body.get('replies'):
        raise RuntimeError('Phase 4 client did not complete three addressed turns')
    time.sleep(2.5)

    player_id = body['player_id']

    def read_note():
        script = (
            'import sqlite3\n'
            'c=sqlite3.connect("/var/lib/bots/recall.sqlite")\n'
            f'row=c.execute("select note from notes where bot_index=? and player_id=?", '
            f'({lola_index}, {player_id})).fetchone()\n'
            'print(row[0] if row else "")'
        )
        return docker(['exec', service, 'python', '-I', '-c', script],
                        timeout=30).stdout.strip()

    note = read_note()
    if not note:
        raise RuntimeError('Expected a relationship note on recall.sqlite after three turns')
    docker(['restart', '-t', '3', service])
    wait_all()
    if read_note() != note:
        raise RuntimeError('Relationship note did not survive a bots process restart')
    forget = docker(['exec', '-i', dash, 'python', '-I', '-c', protocol + FORGET],
                      input=json.dumps({'human': observers[0], 'lola': ids['lolaritmo'],
                                        'player_id': body['player_id']})).stdout.strip()
    if forget != 'ok':
        raise RuntimeError('Forget phrase did not complete')
    time.sleep(0.5)
    if read_note():
        raise RuntimeError('Forget phrase did not delete the SQLite note')
    metrics = exec_script(dash, fetch).stdout
    if any(line.startswith('bots_provider_requests_total{') for line in metrics.splitlines()):
        raise RuntimeError('Simulated recall issued provider HTTP requests')
    print('PASS: Phase 4 on real disposable AS2: three-turn note on PVC, persistence after '
          'bots restart, forget clears SQLite; zero provider HTTP.')


CLIENTS = r'''
import sys,json,asyncio
async def main():
    doc=json.load(sys.stdin)
    conn=await AS2Connection.connect('login',6112)
    try:
        rndk=await conn.handshake(10)
        fields=await conn.login_legacy(doc['human']['username'],login_server_password(doc['human']['password'],rndk),10)
    finally: await conn.close()
    logged=parse_login(fields)
    pid,key=logged.player_id,logged.login_key
    conn=await AS2Connection.connect('glaciar',9876)
    rndk=await conn.handshake(10)
    await conn.login_world(doc['human']['username'],logged,rndk,10)
    await conn.join_world(int(pid),key,10)
    await conn.join_room(120,10,330,250)
    await asyncio.sleep(.7)
    async def roster():
        await conn.send_line('%xt%s%j#grs%-1%')
        while True:
            raw=await conn.read_line(5)
            if raw.startswith('%'):
                frame=Frame.parse(raw)
                if frame.op=='grs':
                    return {p[0] for p in (v.split('|') for v in frame.args[1:]) if p and p[0].isdigit()}
    until=asyncio.get_running_loop().time()+30
    while asyncio.get_running_loop().time()<until:
        if str(doc['lola']) in await roster():
            break
        await asyncio.sleep(.5)
    else:
        raise AssertionError('Lola not present in disco roster')
    replies=0
    for n in ('uno','dos','tres'):
        await conn.send_chat(int(pid),f"Lola hola {n}")
        until=asyncio.get_running_loop().time()+15
        while asyncio.get_running_loop().time()<until:
            try: raw=await conn.read_line(.5)
            except TimeoutError: continue
            if not raw.startswith('%'): continue
            frame=Frame.parse(raw)
            if frame.op=='sm' and frame.args[0]==str(doc['lola']):
                replies+=1
                await asyncio.sleep(1.0)
                break
        else:
            raise AssertionError('Missing reply')
    print(json.dumps({'turns':3,'replies':replies,'player_id':int(pid)}))
    await conn.close()
asyncio.run(main())
'''

FORGET = r'''
import sys,json,asyncio
async def main():
    doc=json.load(sys.stdin)
    conn=await AS2Connection.connect('login',6112)
    try:
        rndk=await conn.handshake(10)
        fields=await conn.login_legacy(doc['human']['username'],login_server_password(doc['human']['password'],rndk),10)
    finally: await conn.close()
    logged=parse_login(fields)
    pid,key=logged.player_id,logged.login_key
    conn=await AS2Connection.connect('glaciar',9876)
    rndk=await conn.handshake(10)
    await conn.login_world(doc['human']['username'],logged,rndk,10)
    await conn.join_world(int(pid),key,10)
    await conn.join_room(120,10,330,250)
    await asyncio.sleep(.7)
    await conn.send_chat(int(pid),'Lola, borra mi memoria')
    until=asyncio.get_running_loop().time()+8
    while asyncio.get_running_loop().time()<until:
        try: raw=await conn.read_line(.5)
        except TimeoutError: continue
        if raw.startswith('%'):
            frame=Frame.parse(raw)
            if frame.op=='sm' and frame.args[0]==str(doc['lola']) and 'borre' in frame.args[1].lower():
                print('ok')
                await conn.close()
                return
    raise AssertionError('Missing forget acknowledgement')
asyncio.run(main())
'''

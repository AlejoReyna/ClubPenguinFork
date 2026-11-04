"""Real Houdini fixture for test-containers.py; never connects to Kubernetes or an LLM.

The caller owns the internal Docker network and label-scoped cleanup. Credentials
are generated for this disposable database only, never taken from the operator.
"""
import hashlib
import json
import secrets
import time

import bot_accounts


def check_bots(root, docker, exec_script, dash, postgres, world, network, label, tag, temp):
    image = 'clubpenguin/bots:' + tag
    # Refuse stale images: the service under test must contain the current source.
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (root / 'containers/bots/bots').glob('*.py')}
    check = ('import pathlib,hashlib,json\n'
             'print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() '
             'for p in pathlib.Path("/app/bots").glob("*.py")}))')
    actual = docker(['run', '--rm', '--pull=never', '--network=none', '--read-only',
                     '--entrypoint', 'python', image, '-c', check])
    if json.loads(actual.stdout) != source_hashes:
        raise RuntimeError('Bots image is stale; rebuild before testing or publishing')
    inspected = json.loads(docker(['image', 'inspect', image]).stdout)[0]
    host_arch = docker(['info', '--format', '{{.Architecture}}']).stdout.strip()
    host_arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(host_arch, host_arch)
    if inspected['Architecture'] != host_arch:
        raise RuntimeError('Bots fixture must use the native architecture of the Docker host')

    # Measured rectangles (see bot_accounts.MEASURED_WORK_AREAS).
    doc = bot_accounts.work_config_document()
    # Speed up local walking only; heartbeat uses the real server cadence.
    doc['behavior'].update(walk_min_delay=0.7, walk_max_delay=1.2,
                           first_move_delay_min=0.1, first_move_delay_max=0.5)
    observer = {'username': 'auditbot', 'nickname': 'AuditBot',
                'password': secrets.token_urlsafe(16)}
    register_source = (root / 'scripts/bot_accounts.py').read_text()
    register_source += '''
import sys
payload = json.load(sys.stdin)
for bot in payload['bots']:
    register('http://dash:3000', bot['nickname'], bot['password'])
print('registered')
'''
    result = docker(['exec', '-i', dash, 'python', '-c', register_source],
                    input=json.dumps({'bots': doc['bots'] + [observer]}))
    if result.stdout.strip() != 'registered':
        raise RuntimeError('Disposable bot registration failed')
    # Dash title-cases nicknames. The visible name has to be the role name.
    corrected = docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                        '-At', '-v', 'ON_ERROR_STOP=1', '-c', bot_accounts.nickname_correction_sql()]).stdout
    if sorted(line.strip() for line in corrected.splitlines()
              if line.strip() and not line.strip().startswith('UPDATE ')) != ['baristaia', 'djia', 'pizzeroia']:
        raise RuntimeError('Visible nicknames were not corrected to the role names')

    def account_snapshot():
        query = ("SELECT json_agg(json_build_object('id',id,'username',username,'active',active,"
                 "'moderator',moderator,'stealth_moderator',stealth_moderator) ORDER BY id) "
                 "FROM penguin WHERE username IN ('baristaia','djia','pizzeroia');")
        rows = json.loads(docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                                  '-At', '-v', 'ON_ERROR_STOP=1', '-c', query]).stdout)
        if len(rows) != 3 or any(not b['active'] or b['moderator'] or b['stealth_moderator'] for b in rows):
            raise RuntimeError('Bot identities must be active, ordinary accounts')
        return rows

    initial = account_snapshot()
    authenticated = docker(['exec', '-i', dash, 'python', '-c', bot_accounts.authentication_program()],
                           input=json.dumps(doc))
    if json.loads(authenticated.stdout) != {'authenticated': 3}:
        raise RuntimeError('The operator provisioning authentication check failed')
    expected_ids = {b['username']: b['id'] for b in initial}
    config_path = temp / 'synthetic-bots.json'
    config_path.write_text(json.dumps(doc))
    # Synthetic credentials only. A single read-only file is mounted, never .local/.
    config_path.chmod(0o444)
    service = network + '-bots'
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', network, '--network-alias', 'bots', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--mount', f'type=bind,source={config_path},target=/etc/bots/config.json,readonly',
            '-e', 'BOTS_PROVIDER_MODE=simulated', image])

    fetch = '''import urllib.request
with urllib.request.urlopen('http://bots:9102/metrics',timeout=3) as r:
 print(r.read().decode())
'''

    def metrics():
        return exec_script(dash, fetch).stdout

    def samples(text, metric):
        return [float(line.rsplit(' ', 1)[1]) for line in text.splitlines()
                if line.startswith(metric + '{') or line.startswith(metric + ' ')]

    def wait_metric(metric, predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                values = samples(metrics(), metric)
                if predicate(values):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError('Bots fixture did not satisfy metric: ' + metric)

    def connected(values):
        return len(values) == 3 and all(v == 1 for v in values)

    wait_metric('bots_connected', connected)
    started = time.monotonic()
    protocol = (root / 'containers/bots/bots/as2.py').read_text()
    observe = protocol + '''
import sys,json
async def observe():
    doc=json.load(sys.stdin)
    identity=doc['observer']
    login=await AS2Connection.connect('login',6112)
    try:
        rndk=await login.handshake(5)
        fields=await login.login_legacy(identity['username'],login_server_password(identity['password'],rndk),5)
    finally:
        await login.close()
    pid,key=fields[:2]
    conn=await AS2Connection.connect('glaciar',9876)
    try:
        rndk=await conn.handshake(5)
        await conn.login_legacy(identity['username'],world_login_password(key,rndk),5)
        await conn.join_world(int(pid),key,5)
        all_ids=set(doc['expected_ids'].values())
        count=0
        for name, alias, room in [('baristaia','Barista',110),('djia','DJ',120),('pizzeroia','Pizzero',330)]:
            frame=await conn.join_room(room,5)
            expected=doc['expected_ids'][name]
            present={int(v.split('|')[0]) for v in frame.args if '|' in v and v.split('|')[0].isdigit()}
            assert present & all_ids == {expected}, 'Wrong room roster'
            record=next(v.split('|') for v in frame.args if v.split('|')[0] == str(expected))
            assert record[1] == {'baristaia': 'BaristaIA', 'djia': 'DJIA', 'pizzeroia': 'PizzeroIA'}[name], 'Wrong registered nickname in roster'
            assert int(record[2]) & 8, 'Name is not approved for Spanish'
            wrong={'baristaia':'DJ','djia':'Pizzero','pizzeroia':'Barista'}[name]
            await conn.send_chat(int(pid),wrong+' hola')
            silence_until=asyncio.get_running_loop().time()+2
            while asyncio.get_running_loop().time()<silence_until:
                try: raw=await conn.read_line(0.5)
                except TimeoutError: continue
                if raw.startswith('%'):
                    ignored=Frame.parse(raw)
                    assert not (ignored.op=='sm' and ignored.args and ignored.args[0]==str(expected)), 'Replied to another role'
            await conn.send_chat(int(pid),alias+' hola')
            moved=False
            replies=[]
            until=asyncio.get_running_loop().time()+8
            while asyncio.get_running_loop().time()<until:
                try: raw=await conn.read_line(0.5)
                except TimeoutError: continue
                if not raw.startswith('%'): continue
                frame=Frame.parse(raw)
                if frame.op in ('sp','sm') and frame.args and frame.args[0].isdigit():
                    sender=int(frame.args[0])
                    if sender in all_ids and frame.op=='sm': replies.append(sender)
                    if sender==expected and frame.op=='sp':
                        x,y=map(int,frame.args[1:3])
                        # Mirrors bot_accounts.MEASURED_WORK_AREAS (coffee/dance/pizza.swf block_mc).
                        a={'baristaia':(300,360,290,350),'djia':(280,380,230,265),'pizzeroia':(365,430,258,290)}[name]
                        assert a[0] <= x <= a[1] and a[2] <= y <= a[3], 'Move outside measured area'
                        moved=True
            assert moved and replies==[expected], 'Expected movement and exactly one addressed reply'
            count+=1
        print(json.dumps({'rooms':count}))
    finally:
        await conn.close()
asyncio.run(observe())
'''
    result = docker(['exec', '-i', dash, 'python', '-c', observe],
                    input=json.dumps({'observer': observer, 'expected_ids': expected_ids}))
    if json.loads(result.stdout) != {'rooms': 3}:
        raise RuntimeError('Real room observation failed')
    print('PASS: three ordinary bot accounts are visible in their respective rooms, moving within measured room areas and replying to aliases; zero browsers.')

    # Includes more than one full 60-65 s heartbeat interval with no observer.
    while time.monotonic() - started < 70:
        time.sleep(min(1, 70 - (time.monotonic() - started)))
    wait_metric('bots_heartbeats_total', lambda v: len(v) == 3 and min(v) >= 1, seconds=10)
    wait_metric('bots_connected', connected)
    text = metrics()
    if samples(text, 'bots_provider_requests_total'):
        raise RuntimeError('Simulated mode must not issue provider HTTP requests')
    print('PASS: bots remain online without humans and receive real heartbeat acknowledgements; no paid API.')

    observe_rosters = protocol + '''
import sys,json
async def observe():
    doc=json.load(sys.stdin)
    identity=doc['observer']
    login=await AS2Connection.connect('login',6112)
    try:
        rndk=await login.handshake(5)
        fields=await login.login_legacy(identity['username'],login_server_password(identity['password'],rndk),5)
    finally: await login.close()
    pid,key=fields[:2]
    conn=await AS2Connection.connect('glaciar',9876)
    try:
        rndk=await conn.handshake(5)
        await conn.login_legacy(identity['username'],world_login_password(key,rndk),5)
        await conn.join_world(int(pid),key,5)
        all_ids=set(doc['expected_ids'].values())
        for name,room in [('baristaia',110),('djia',120),('pizzeroia',330)]:
            frame=await conn.join_room(room,5)
            present={int(v.split('|')[0]) for v in frame.args if '|' in v and v.split('|')[0].isdigit()}
            assert present & all_ids == {doc['expected_ids'][name]}, 'Reconnected bot in wrong room'
            await asyncio.sleep(0.7)
        print('ok')
    finally: await conn.close()
asyncio.run(observe())
'''

    def check_reconnected_rooms():
        observation = docker(['exec', '-i', dash, 'python', '-c', observe_rosters],
                             input=json.dumps({'observer': observer, 'expected_ids': expected_ids}))
        if observation.stdout.strip() != 'ok':
            raise RuntimeError('Reconnected bots missing from their assigned rooms')

    docker(['restart', '-t', '5', world])
    wait_metric('bots_sessions_total', lambda v: len(v) == 3 and min(v) >= 2, seconds=45)
    wait_metric('bots_connected', connected)
    if account_snapshot() != initial:
        raise RuntimeError('Bot account identities changed across world reconnect')
    check_reconnected_rooms()
    print('PASS: all three bots automatically reconnect after an isolated world restart, using the same identities.')

    docker(['restart', '-t', '10', service])
    wait_metric('bots_connected', connected)
    if account_snapshot() != initial:
        raise RuntimeError('Restarting bots changed account identities')
    check_reconnected_rooms()
    print('PASS: a separate bots service restart reuses the existing accounts; no registration at runtime.')

    # Exercise the actual HTTP provider against a synthetic loopback server in
    # the bot container's network namespace. The enclosing Docker network has
    # already been proved to have no external route.
    docker(['rm', '-f', service])
    doc['policy'].update(player_cooldown=0.1, bot_min_gap=0.1, requests_per_minute=60)
    doc['provider'].update(timeout=6, retries=1, retry_wait=0.1, max_requests_per_day=5,
                           fallback_replies=['Ahora no puedo responder.'])
    config_path.chmod(0o600)
    config_path.write_text(json.dumps(doc))
    config_path.chmod(0o444)
    shared = temp / 'fake-provider'
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', network, '--network-alias', 'bots', '--read-only',
            '--mount', f'type=bind,source={config_path},target=/etc/bots/config.json,readonly',
            '--mount', f'type=bind,source={shared},target=/fake',
            '-e', 'BOTS_PROVIDER_MODE=api', '-e', 'MOONSHOT_API_KEY=synthetic-only',
            '-e', 'BOTS_PROVIDER_BASE_URL=http://127.0.0.1:18991/v1',
            '-e', 'BOTS_BUDGET_PATH=/fake/budget.json', image])
    fake = '''import asyncio,json
async def handle(reader,writer):
    try:
        header=await reader.readuntil(b"\\r\\n\\r\\n")
        length=next(int(line.split(b":",1)[1]) for line in header.split(b"\\r\\n") if line.lower().startswith(b"content-length:"))
        payload=json.loads(await reader.readexactly(length))
        system=payload['messages'][0]['content']
        last=payload['messages'][-1]['content']
        with open('/fake/events.jsonl','a') as out:
            out.write(json.dumps({'model':payload['model'],'system':system,'error':'fallo' in last,
                                  'thinking':payload.get('thinking'),'max_completion_tokens':payload.get('max_completion_tokens')})+'\\n')
        if 'fallo' in last:
            status,body=503,b'{}'
        else:
            if 'lento' in last: await asyncio.sleep(4)
            status=200
            body=json.dumps({'choices':[{'message':{'content':'Respuesta de prueba'}}]}).encode()
        writer.write(b'HTTP/1.1 '+str(status).encode()+b' Test\\r\\nContent-Length: '+str(len(body)).encode()+b'\\r\\nConnection: close\\r\\n\\r\\n'+body)
        await writer.drain()
    finally:
        writer.close()
async def main():
    server=await asyncio.start_server(handle,'127.0.0.1',18991)
    async with server: await server.serve_forever()
asyncio.run(main())
'''
    fake_name = network + '-fake-provider'
    docker(['run', '-d', '--pull=never', '--name', fake_name, '--label', label,
            '--network', 'container:' + service, '--read-only',
            '--mount', f'type=bind,source={shared},target=/fake',
            '--entrypoint', 'python', image, '-u', '-c', fake])
    for attempt in range(30):
        try:
            docker(['exec', service, 'python', '-c',
                    'import socket; socket.create_connection(("127.0.0.1",18991),1).close()'])
            break
        except Exception:
            if attempt == 29:
                raise RuntimeError('Fake HTTP provider did not start')
            time.sleep(0.2)
    wait_metric('bots_connected', connected)
    observe_api = protocol + '''
import sys,json
async def observe():
    doc=json.load(sys.stdin)
    identity=doc['observer']
    login=await AS2Connection.connect('login',6112)
    try:
        rndk=await login.handshake(5)
        fields=await login.login_legacy(identity['username'],login_server_password(identity['password'],rndk),5)
    finally: await login.close()
    pid,key=fields[:2]
    conn=await AS2Connection.connect('glaciar',9876)
    try:
        rndk=await conn.handshake(5)
        await conn.login_legacy(identity['username'],world_login_password(key,rndk),5)
        await conn.join_world(int(pid),key,5)
        async def reply(sender,slow=False):
            moves=0
            until=asyncio.get_running_loop().time()+12
            while asyncio.get_running_loop().time()<until:
                try: raw=await conn.read_line(0.5)
                except TimeoutError: continue
                if not raw.startswith('%'): continue
                frame=Frame.parse(raw)
                if frame.args and frame.args[0]==str(sender):
                    if frame.op=='sp': moves+=1
                    if frame.op=='sm':
                        assert not slow or moves>0, 'No movement during slow inference'
                        return frame.args[-1]
            raise AssertionError('Missing reply')
        for name,alias,room in [('baristaia','Barista',110),('djia','DJ',120),('pizzeroia','Pizzero',330)]:
            await conn.join_room(room,5)
            await conn.send_chat(int(pid),alias+(' lento' if room==110 else ' hola'))
            if room==110:
                await asyncio.sleep(0.7)
                await conn.send_chat(int(pid),'mensaje sin destinatario')
                await asyncio.sleep(0.3)
                reader,writer=await asyncio.open_connection('bots',9102)
                writer.write(b'GET /metrics HTTP/1.1\\r\\nHost: bots\\r\\nConnection: close\\r\\n\\r\\n')
                await writer.drain()
                metrics=(await asyncio.wait_for(reader.read(),2)).decode()
                writer.close()
                received=[line for line in metrics.splitlines() if line.startswith('bots_chats_received_total{bot="0"}')]
                assert received and float(received[0].split()[-1]) >= 2, 'Reader stopped during inference'
                assert 'bots_chats_sent_total{bot="0"}' not in metrics, 'Slow response arrived too soon'
            assert await reply(doc['expected_ids'][name],room==110)=='Respuesta de prueba'
            await asyncio.sleep(0.7)  # Houdini's human-chat cooldown is 0.5 s.
        await conn.join_room(110,5)
        await conn.send_chat(int(pid),'Barista fallo')
        assert await reply(doc['expected_ids']['baristaia'])=='Ahora no puedo responder.'
        await asyncio.sleep(0.7)
        await conn.send_chat(int(pid),'Barista otra')
        assert await reply(doc['expected_ids']['baristaia'])=='Ahora no puedo responder.'
        print('ok')
    finally: await conn.close()
asyncio.run(observe())
'''
    result = docker(['exec', '-i', dash, 'python', '-c', observe_api],
                    input=json.dumps({'observer': observer, 'expected_ids': expected_ids}))
    if result.stdout.strip() != 'ok':
        raise RuntimeError('Fake HTTP provider observation failed')
    events = [json.loads(line) for line in (shared / 'events.jsonl').read_text().splitlines()]
    if len(events) != 5 or sum(e['error'] for e in events) != 2:
        raise RuntimeError('Retries and daily quota did not limit fake HTTP requests')
    prompts = ' '.join(e['system'] for e in events)
    if not all(role in prompts for role in ('BaristaIA', 'DJIA', 'PizzeroIA')):
        raise RuntimeError('Expected distinct role prompts at the fake HTTP server')
    if any(e['model'] != 'kimi-k2.6' for e in events):
        raise RuntimeError('Unexpected model in fake requests')
    if any(e['thinking'] != {'type': 'disabled'} or e['max_completion_tokens'] != 120 for e in events):
        raise RuntimeError('Unexpected fake provider request parameters')
    wait_metric('bots_provider_fallback_total', lambda values: sum(values) >= 2)
    if account_snapshot() != initial:
        raise RuntimeError('Fake HTTP scenario changed account identities')
    print('PASS: slow fake HTTP inference permits movement; role prompts, fallback, retries and quota verified on real Houdini; no paid calls.')

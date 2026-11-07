"""26 bodies and ten protocol clients on the caller's internal disposable network.

No real Secret, cluster client, paid provider, browser or operator data is used.
This proves protocol behavior only; visual acceptance and a 24 h soak are separate.
"""
import json
import secrets
import re
import sys
import time

import bot_accounts
from bots_image import verify_bots_image


def check_live_bots(root, docker, exec_script, dash, postgres, world, network, label, tag, temp,
                    voices=False, life=False, recall=False):
    sys.path.insert(0, str(root / 'containers/bots'))
    from bots.config import build_config
    from bots.rooms import load_catalog

    image = 'clubpenguin/bots:' + tag
    package = verify_bots_image(root, docker, image)
    cast_path = root / 'containers/bots/cast'
    document = bot_accounts.work_config_document()
    document['bots'].extend(bot_accounts.cast_config_document(cast_path)['bots'])
    document['behavior'].update(life_min_delay=1, life_max_delay=2,
                                walk_min_delay=0.8, walk_max_delay=1.5)
    if voices or life or recall:
        document['dialogue'] = {'enabled': True, 'greeting_chance': 0,
                                'scene_min_delay': 3600, 'scene_max_delay': 3600}
    config = build_config(document, {'BOTS_CAST_DIR': str(cast_path)})
    if len(config.bots) != 26:
        raise RuntimeError('Live cast fixture must have 26 bodies')
    observers = [{'username': f'auditlive{i}', 'nickname': f'Auditlive{i}',
                  'password': secrets.token_urlsafe(16)} for i in range(10)]
    register = (root / 'scripts/bot_accounts.py').read_text() + '''
import sys
for bot in json.load(sys.stdin)['bots']:
    register('http://dash:3000', bot['username'], bot['password'])
print('registered')
'''
    if docker(['exec', '-i', dash, 'python', '-I', '-c', register],
              input=json.dumps({'bots': document['bots'] + observers})).stdout.strip() != 'registered':
        raise RuntimeError('Disposable registration failed')
    docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-At', '-v', 'ON_ERROR_STOP=1',
            '-c', bot_accounts.cast_nickname_sql(document['bots'])])
    users = ','.join("'%s'" % bot.username for bot in config.bots)

    def identities():
        query = ("SELECT json_agg(json_build_object('id',id,'username',username,'nickname',nickname,"
                 "'active',active,'approval_es',approval_es,'moderator',moderator,"
                 "'stealth_moderator',stealth_moderator) ORDER BY id) FROM penguin WHERE username IN ("
                 + users + ');')
        return json.loads(docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-At', '-c', query]).stdout)

    initial = identities()
    if len(initial) != 26 or any(not p['active'] or not p['approval_es'] or p['moderator'] or
                                  p['stealth_moderator'] for p in initial):
        raise RuntimeError('All disposable bodies must be ordinary Spanish-approved accounts')
    ids = {p['username']: p['id'] for p in initial}
    planned, allowed, legacy_posts = {}, {}, {}
    catalog = load_catalog()
    for bot in config.bots:
        pid = str(ids[bot.username])
        planned.setdefault(str(bot.room), {})[pid] = bot.nickname
        if bot.card:
            room = catalog.require(bot.room)
            rects = [r.as_tuple() for r in room.walk] + [p.rect.as_tuple() for p in room.pois.values()]
            if room.spawn:
                x, y = room.spawn
                rects.append((x, x, y, y))
        else:
            rects = [bot.area]
            if bot.stationary:
                legacy_posts[pid] = bot.spawn
        allowed[pid] = rects
    path = temp / 'live-synthetic.json'
    path.write_text(json.dumps(document))
    path.chmod(0o444)
    service = network + '-bots'
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', network, '--network-alias', 'bots', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--mount', f'type=bind,source={path},target=/etc/bots/config.json,readonly',
            '--mount', f'type=bind,source={cast_path},target=/etc/bots-cast,readonly',
            '-e', 'BOTS_PROVIDER_MODE=simulated', '-e', 'BOTS_CAST_DIR=/etc/bots-cast', image])

    def metrics():
        return exec_script(dash, 'import urllib.request\nprint(urllib.request.urlopen("http://bots:9102/metrics",timeout=3).read().decode())').stdout

    def values(text, name):
        return [float(line.rsplit(' ', 1)[1]) for line in text.splitlines()
                if line.startswith(name + '{') or line.startswith(name + ' ')]

    def wait_all(sessions=1, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                text = metrics()
                connected, established = values(text, 'bots_connected'), values(text, 'bots_sessions_total')
                if len(connected) == 26 and min(connected) == 1 and len(established) == 26 and min(established) >= sessions:
                    return text
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError('26 bodies did not connect before the 180 s deadline')

    wait_all()
    print('PASS: 26 simulated sessions connected with the default 2–4 s stagger.')
    protocol = (package / 'as2.py').read_text()
    observe = protocol + CLIENTS
    payload = json.dumps({'observers': observers, 'planned': planned, 'allowed': allowed,
                          'ids': ids, 'bots': document['bots'], 'legacy_posts': legacy_posts})
    report = json.loads(docker(['exec', '-i', dash, 'python', '-I', '-c', observe], input=payload).stdout)
    if report.get('clients') != 10 or report.get('rooms') != 11 or report.get('replies', 0) < 10:
        raise RuntimeError('Protocol observation did not cover ten humans and all eleven home rooms')
    text = metrics()
    if values(text, 'bots_provider_requests_total') or values(text, 'bots_spend_cap_ratio') != [0.0]:
        raise RuntimeError('Simulated fixture attempted provider HTTP or recorded spend')
    print('PASS: 10 concurrent protocol clients entered; all 11 home rooms, names, bit 8, '
          'safe positions/movement and addressed local replies verified; no provider HTTP.')
    time.sleep(1)
    restart_start = time.monotonic()
    docker(['restart', '-t', '5', world])
    wait_all(sessions=2, seconds=170)
    elapsed = time.monotonic() - restart_start
    if elapsed >= 180 or identities() != initial:
        raise RuntimeError('World reconnect exceeded three minutes or changed identities')
    # Inspect every room again after reconnect, rather than trusting counters alone.
    result = json.loads(docker(['exec', '-i', dash, 'python', '-I', '-c', observe], input=payload).stdout)
    if result.get('rooms') != 11:
        raise RuntimeError('Home room rosters failed after reconnect')
    print(f'PASS: all 26 bodies returned after the disposable world restart in {elapsed:.1f} s; '
          'same ids, home rooms and no new accounts.')

    def heartbeats():
        return {re.search(r'bot="([0-9]+)"', line).group(1): float(line.rsplit(' ', 1)[1])
                for line in metrics().splitlines() if line.startswith('bots_heartbeats_total{')}

    baseline = heartbeats()
    # Observe a fresh, complete real cadence after reconnect, not just the
    # immediate cast heartbeat at join. All bodies must remain connected.
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        beats = heartbeats()
        if len(beats) == 26 and all(beats[key] > baseline.get(key, 0) for key in beats):
            break
        time.sleep(1)
    else:
        raise RuntimeError(f'Missing real heartbeat acknowledgement: {len(beats)} of 26 series')
    if min(values(metrics(), 'bots_connected')) != 1 or identities() != initial:
        raise RuntimeError('A body disconnected or an account changed during the heartbeat observation')
    print('PASS: all 26 sessions received real heartbeat acknowledgements on the internal network.')
    if voices:
        from voices_integration import check_voices
        check_voices(root, docker, exec_script, dash, network, label, image,
                     service, document, path, cast_path, observers, ids, temp)
    if life:
        from life_integration import check_life
        check_life(root, docker, exec_script, dash, network, label, image,
                   service, document, path, cast_path, observers, ids)
    if recall:
        from recall_integration import check_recall
        check_recall(root, docker, exec_script, dash, network, label, image,
                     service, document, path, cast_path, observers, ids, temp)


CLIENTS = r'''
import sys,json
async def main():
    doc=json.load(sys.stdin)
    connections=[]
    rooms=sorted(map(int,doc['planned']))
    covered=set()
    replies=0
    async def connect(identity):
        login=await AS2Connection.connect('login',6112)
        try:
            rndk=await login.handshake(10)
            fields=await login.login_legacy(identity['username'],login_server_password(identity['password'],rndk),10)
        finally: await login.close()
        pid,key=fields[:2]
        conn=await AS2Connection.connect('glaciar',9876)
        connections.append(conn)
        rndk=await conn.handshake(10)
        await conn.login_legacy(identity['username'],world_login_password(key,rndk),10)
        await conn.join_world(int(pid),key,10)
        return conn,int(pid)
    def position(pid,x,y,landing=False):
        if landing and pid in doc['legacy_posts']:
            # Preserve legacy sessions exactly: Houdini jitters their j#jr post
            # by +/-20 (Room.max_users // 4). This is protocol tolerance, NOT a
            # newly measured floor rectangle. Every u#sp still uses strict bounds.
            ax,ay=doc['legacy_posts'][pid]
            assert abs(x-ax)<=20 and abs(y-ay)<=20, 'Legacy landing outside server jitter'
            return
        assert any(a<=x<=b and c<=y<=d for a,b,c,d in doc['allowed'][pid]), f'Position outside measured floor or fixed post: id={pid} x={x} y={y}'
    async def inspect(conn,pid,room):
        nonlocal replies
        frame=await conn.join_room(room,10)
        expected=doc['planned'][str(room)]
        present={v.split('|')[0]:v.split('|') for v in frame.args if '|' in v}
        assert set(present)&set(doc['allowed'])==set(expected), 'Wrong planned roster'
        for bot_id,name in expected.items():
            entry=present[bot_id]
            assert entry[1]==name and int(entry[2])&8, 'Wrong nickname or approval'
            position(bot_id,int(entry[12]),int(entry[13]),landing=True)
        covered.add(room)
        chosen=next(b for b in doc['bots'] if b['room']==room)
        addressed=str(doc['ids'][chosen['username']])
        await conn.send_chat(pid,chosen['username']+' hola')
        answered=[]
        until=asyncio.get_running_loop().time()+8
        while asyncio.get_running_loop().time()<until:
            try: raw=await conn.read_line(0.5)
            except TimeoutError: continue
            if not raw.startswith('%'): continue
            frame=Frame.parse(raw)
            if frame.op in ('sp','sm') and frame.args and frame.args[0] in doc['allowed']:
                sender=frame.args[0]
                assert sender in expected, 'Body traveled between rooms'
                if frame.op=='sp': position(sender,int(frame.args[1]),int(frame.args[2]))
                else:
                    assert len(frame.args[1])<=54, 'Chat exceeded the AS2 limit'
                    answered.append(sender)
        assert answered==[addressed], 'Expected exactly one addressed local reply'
        replies+=len(answered)
    try:
        # Sequential logins avoid a burst of bcrypt work. All ten remain online.
        clients=[await connect(identity) for identity in doc['observers']]
        await asyncio.gather(*(inspect(conn,pid,room) for (conn,pid),room in zip(clients,rooms[:10])))
        await asyncio.sleep(0.7)
        await inspect(*clients[0],rooms[10])
        print(json.dumps({'clients':len(clients),'rooms':len(covered),'replies':replies}))
    finally:
        await asyncio.gather(*(conn.close() for conn in connections),return_exceptions=True)
asyncio.run(main())
'''

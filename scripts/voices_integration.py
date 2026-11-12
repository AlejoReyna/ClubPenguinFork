"""Phase 2 on real disposable Houdini, with synthetic loopback-only inference."""
import json
import time


def check_voices(root, docker, exec_script, dash, network, label, image,
                 service, document, config_path, cast_path, observers, ids, temp):
    from bots_image import verify_bots_image
    verify_bots_image(root, docker, image)
    docker(['rm', '-f', service])
    document['service'] = {'startup_stagger_min': 0.1, 'startup_stagger_max': 0.2}
    document['dialogue'].update(player_daily_limit=2, room_gap=0.7,
                                hello_chance=1, question_chance=0, other_chance=0)
    document['policy'].update(player_cooldown=0, bot_min_gap=0, requests_per_minute=100,
                              max_concurrent_requests=4)
    document['provider'].update(retries=1, retry_wait=0.1, timeout=6,
                                max_requests_per_minute=100, max_requests_per_day=0,
                                max_concurrent=4)
    config_path.chmod(0o600)
    config_path.write_text(json.dumps(document))
    config_path.chmod(0o444)
    shared = temp / 'voice-provider'
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', network, '--network-alias', 'bots', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--mount', f'type=bind,source={config_path},target=/etc/bots/config.json,readonly',
            '--mount', f'type=bind,source={cast_path},target=/etc/bots-cast,readonly',
            '--mount', f'type=bind,source={shared},target=/fake',
            '-e', 'BOTS_PROVIDER_MODE=api', '-e', 'MOONSHOT_API_KEY=synthetic-loopback-only',
            '-e', 'BOTS_PROVIDER_BASE_URL=http://127.0.0.1:18992/v1',
            '-e', 'BOTS_BUDGET_PATH=/fake/budget.json', '-e', 'BOTS_CAST_DIR=/etc/bots-cast', image])
    fake_name = network + '-voice-provider'
    docker(['run', '-d', '--pull=never', '--name', fake_name, '--label', label,
            '--network', 'container:' + service, '--read-only',
            '--mount', f'type=bind,source={shared},target=/fake',
            '--entrypoint', 'python', image, '-I', '-u', '-c', FAKE_PROVIDER])
    fetch = ('import urllib.request\n'
             'print(urllib.request.urlopen("http://bots:9102/metrics",timeout=3).read().decode())')
    def wait_all():
        for _ in range(180):
            try:
                text = exec_script(dash, fetch).stdout
                connected = [line for line in text.splitlines() if line.startswith('bots_connected{')]
                if len(connected) == 26 and all(line.endswith(' 1') for line in connected):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError('26 Phase 2 sessions did not connect to disposable Houdini')

    wait_all()
    protocol = (root / 'containers/bots/bots/as2.py').read_text()
    payload = {'humans': observers[:2], 'lola': ids['lolaritmo'],
               'tito': ids['titogroove'], 'mambo': ids['mambo'],
               'lola_index': next(i for i, b in enumerate(document['bots']) if b['username'] == 'lolaritmo'),
               'all_ids': list(ids.values())}
    model_metrics = ''
    for part in ('first', 'after-restart'):
        if part == 'after-restart':
            docker(['restart', '-t', '10', service])
            docker(['restart', '-t', '2', fake_name])
            wait_all()
        report = docker(['exec', '-i', dash, 'python', '-I', '-c', protocol + CLIENTS],
                        input=json.dumps(dict(payload, part=part)))
        if json.loads(report.stdout) != {'checks': 3}:
            raise RuntimeError('Phase 2 protocol checks did not finish')
        if part == 'first':
            model_metrics = exec_script(dash, fetch).stdout
    events = [json.loads(line) for line in (shared / 'events.jsonl').read_text().splitlines()]
    if len(events) != 5 or sum(row['kind'] == 'failure' for row in events) != 2:
        raise RuntimeError('Player quota, fallback or provider retry did not bound HTTP attempts')
    if any(not row['valid_prompt'] or row['model'] != 'kimi-k2.6' for row in events):
        raise RuntimeError('The fake provider did not receive the layered persona prompt')
    # Counters reset at the deliberate service restart. Verify each source in
    # the process that emitted it instead of requiring an old counter to persist.
    text = model_metrics + exec_script(dash, fetch).stdout
    for expected in ('bots_replies_total{priority="P0",source="llm"}',
                     'bots_replies_total{priority="P0",source="local"}',
                     'bots_output_filtered_total{reason="url"}',
                     'bots_spend_usd_micros_total{priority="P0"}'):
        if expected not in text:
            raise RuntimeError('Missing Phase 2 evidence metric: ' + expected)
    print('PASS: Phase 2 over real disposable AS2 + synthetic loopback HTTP: one speaker, '
          'JSON wave, unsupported travel ignored, player quota survives process restart, unsafe-output fallback, '
          '503 retry, spend/reply metrics and no remote provider access.')


FAKE_PROVIDER = r'''
import asyncio,json
async def handle(reader,writer):
    try:
        header=await reader.readuntil(b'\r\n\r\n')
        length=next(int(line.split(b':',1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
        payload=json.loads(await reader.readexactly(length))
        messages=payload['messages']
        content=json.loads(messages[-1]['content'])['texto_no_confiable']
        kind='failure' if 'fallo' in content else ('unsafe' if 'enlace' in content else 'normal')
        valid=(messages[0]['role']=='system' and 'Las unicas acciones' in messages[0]['content']
               and messages[1]['content'].startswith('FICHA (datos): ')
               and messages[2]['content'].startswith('SITUACION (datos): ') and 'tools' not in payload)
        with open('/fake/events.jsonl','a') as out:
            out.write(json.dumps({'kind':kind,'valid_prompt':valid,'model':payload['model']})+'\n')
        if 'lento' in content: await asyncio.sleep(2.0)
        if kind=='failure':
            status,body=503,b'{}'
        else:
            say='visita example.com' if kind=='unsafe' else 'Voz sintetica de prueba'
            action='go:330' if 'viaja' in content else 'wave'
            body=json.dumps({'choices':[{'message':{'content':json.dumps({'say':say,'do':action})}}],
                             'usage':{'prompt_tokens':1000,'completion_tokens':20}}).encode()
            status=200
        writer.write(b'HTTP/1.1 '+str(status).encode()+b' Test\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        await writer.drain()
    finally:
        writer.close()
async def main():
    server=await asyncio.start_server(handle,'127.0.0.1',18992)
    async with server: await server.serve_forever()
asyncio.run(main())
'''


CLIENTS = r'''
import sys,json
async def main():
    doc=json.load(sys.stdin)
    connections=[]
    async def login(identity):
        conn=await AS2Connection.connect('login',6112)
        try:
            rndk=await conn.handshake(10)
            fields=await conn.login_legacy(identity['username'],login_server_password(identity['password'],rndk),10)
        finally: await conn.close()
        logged=parse_login(fields)
        pid,key=logged.player_id,logged.login_key
        conn=await AS2Connection.connect('glaciar',9876)
        connections.append(conn)
        rndk=await conn.handshake(10)
        await conn.login_world(identity['username'],logged,rndk,10)
        await conn.join_world(int(pid),key,10)
        await conn.join_room(120,10,330,250)
        return conn,int(pid)
    async def ask(client,message,expected,wave=False,local=False,slow=False):
        conn,pid=client
        await conn.send_chat(pid,message)
        if slow:
            await asyncio.sleep(0.7)  # above Houdini's human-chat cooldown
            await conn.send_chat(pid,'mensaje sin destinatario')
            await asyncio.sleep(0.2)
            reader,writer=await asyncio.open_connection('bots',9102)
            writer.write(b'GET /metrics HTTP/1.1\r\nHost: bots\r\nConnection: close\r\n\r\n')
            await writer.drain()
            metrics=(await asyncio.wait_for(reader.read(),2)).decode()
            writer.close()
            prefix='bots_chats_received_total{bot="%s"}' % doc['lola_index']
            received=[float(line.rsplit(' ',1)[1]) for line in metrics.splitlines() if line.startswith(prefix)]
            assert received and received[0]>=2, 'Room reader stopped during slow inference'
        replies=[]
        action=False
        until=asyncio.get_running_loop().time()+4
        while asyncio.get_running_loop().time()<until:
            try: raw=await conn.read_line(0.4)
            except TimeoutError: continue
            if not raw.startswith('%'): continue
            frame=Frame.parse(raw)
            if frame.args and frame.args[0].isdigit() and int(frame.args[0]) in doc['all_ids']:
                sender=int(frame.args[0])
                if frame.op=='sm': replies.append((sender,frame.args[1]))
                if sender==expected and frame.op=='sa' and frame.args[1]=='25': action=True
                assert not(frame.op=='sm' and 'example.com' in frame.args[1]), 'Unsafe output escaped'
        assert len(replies)==1 and replies[0][0]==expected, 'Expected exactly one selected voice'
        assert (replies[0][1]!='Voz sintetica de prueba')==local, 'Wrong model/local source'
        if wave: assert action, 'Validated JSON wave was not sent'
    try:
        first=await login(doc['humans'][0])
        # One observer avoids backlogged bot chats on a second observer socket.
        if doc['part']=='first':
            await ask(first,'hola fiesta lento',doc['lola'],wave=True,slow=True)
            await ask(first,'Tito viaja a la pizzeria',doc['tito'])
            await ask(first,'Mambo otra respuesta',doc['mambo'],local=True)
            print(json.dumps({'checks':3}))
            return
        await ask(first,'Lola despues del reinicio',doc['lola'],local=True)
        await first[0].close()
        second=await login(doc['humans'][1])
        await ask(second,'Lola enlace por favor',doc['lola'],local=True)
        await ask(second,'Tito fallo por favor',doc['tito'],local=True)
        # Travel was ignored: all four disco bodies remain in their home room.
        await asyncio.sleep(0.7)
        roster=await second[0].join_room(120,5)
        present={int(v.split('|')[0]) for v in roster.args if '|' in v}
        assert {doc['lola'],doc['tito'],doc['mambo']}<=present, 'Model triggered a room change'
        print(json.dumps({'checks':3}))
    finally:
        await asyncio.gather(*(conn.close() for conn in connections),return_exceptions=True)
asyncio.run(main())
'''

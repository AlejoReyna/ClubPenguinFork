"""Run the experimental bridge against disposable Houdini/Postgres/Redis.

Creates only uniquely labelled Docker resources on an internal network. It
passes no real credentials and cannot reach a provider. No Pi access.
"""
import json
import argparse
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.puffle_sim.houdini_patch import FILES, transform


def human_account_indexes(visual, local_actions, spotify, music_mac=False, music_safari=False):
    """A single-account host relay may only be used by the visual owner."""
    if local_actions or spotify or music_mac or music_safari:
        return [3]
    return [0, 1, 3] if visual else [0, 1]


def run(*args, input=None, timeout=120):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(map(str, args[:3])) + '\n' + result.stderr[-3000:])
    return result.stdout


def docker(*args, input=None, timeout=120):
    return run('docker', *args, input=input, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--visual-swf', type=Path, help='experimental SWF copy for a local Ruffle probe')
    parser.add_argument('--browser-python', type=Path, help='local Python with Playwright installed')
    parser.add_argument('--hold', type=Path, help='keep the visual fixture alive while this JSON file exists (max 45 min)')
    parser.add_argument('--provider', choices=('simulator', 'relay', 'spotify-sim'), default='simulator',
                        help='relay: answer with the owner ChatGPT via a host relay (requires --hold)')
    parser.add_argument('--spotify', action='store_true',
                        help='relay only: owner controls HIS Spotify (needs the :1455 Spotify connection)')
    parser.add_argument('--music-mac', action='store_true',
                        help='relay only: owner controls the local macOS Music app')
    parser.add_argument('--music-safari-window-id', type=int,
                        help='relay only: experimental Apple Music playback in the given Safari window')
    parser.add_argument('--allow-local-actions', action='store_true',
                        help='allow the visual owner to request confirmed local Mac actions')
    args = parser.parse_args()
    if args.provider == 'relay' and not (args.hold and args.visual_swf):
        raise SystemExit('--provider relay requires --visual-swf, --browser-python and --hold (manual session only)')
    if args.spotify and args.provider != 'relay':
        raise SystemExit('--spotify is for the manual relay fixture; use --provider spotify-sim for the simulator')
    if args.music_mac and args.provider != 'relay':
        raise SystemExit('--music-mac requires the manual relay fixture')
    if args.music_safari_window_id is not None and args.provider != 'relay':
        raise SystemExit('--music-safari-window-id requires the manual relay fixture')
    if args.music_safari_window_id is not None and not 0 < args.music_safari_window_id < 2**31:
        raise SystemExit('Invalid Safari window')
    if sum((bool(args.music_mac), bool(args.spotify), args.music_safari_window_id is not None)) > 1:
        raise SystemExit('Choose one music player')
    if args.allow_local_actions and args.provider != 'relay':
        raise SystemExit('--allow-local-actions requires the manual relay fixture')
    if bool(args.visual_swf) != bool(args.browser_python):
        raise SystemExit('Both --visual-swf and --browser-python are required for a visual probe')
    run_id = secrets.token_hex(5)
    prefix = 'puffle-fixture-' + run_id
    label = 'puffle.fixture=' + run_id
    containers = []
    network_created = False
    relay_proc = None

    def start(alias, image, *extra, command=()):
        name = prefix + '-' + alias
        docker('run', '-d', '--pull=never', '--name', name, '--label', label,
               '--network', prefix, '--network-alias', alias, *extra, image, *command)
        containers.append(name)
        return name

    def ready(container, host, port):
        script = ('import socket; s=socket.create_connection((' + repr(host) + ',' + str(port)
                  + '),timeout=1); s.close()')
        for _ in range(80):
            result = subprocess.run(['docker', 'exec', container, 'python', '-c', script],
                                    capture_output=True, timeout=5)
            if result.returncode == 0:
                return
            time.sleep(0.25)
        logs = subprocess.run(['docker', 'logs', '--tail', '15', prefix + '-' + host],
                              capture_output=True, text=True, timeout=10)
        sys.stderr.write((logs.stdout + logs.stderr)[-2000:])
        raise RuntimeError('Disposable service did not become ready: ' + host)

    try:
        docker('network', 'create', '--internal', '--label', label, prefix)
        network_created = True
        if docker('network', 'inspect', '--format', '{{.Internal}}', prefix).strip() != 'true':
            raise RuntimeError('Fixture network is not internal')
        with tempfile.TemporaryDirectory(prefix='puffle-fixture-') as tmpdir:
            tmp = Path(tmpdir)
            env = tmp / 'env'
            env.write_text('POSTGRES_USER=houdini\nPOSTGRES_DB=houdini\nPOSTGRES_PASSWORD='
                           + secrets.token_hex(16) + '\nPOSTGRES_HOST=postgres\nREDIS_HOST=redis\n')
            env.chmod(0o600)
            overlay = tmp / 'overlay'
            for name in FILES:
                output = overlay / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(transform(name, (ROOT / 'vendor/houdini/houdini' / name).read_bytes()))
                output.chmod(0o644)
            postgres = start('postgres', 'clubpenguin/database:lab-mobile', '--env-file', str(env))
            start('redis', 'redis:7.4-alpine')
            # TCP check: during initdb PostgreSQL only listens on its socket and then restarts.
            for _ in range(160):
                r = subprocess.run(['docker', 'exec', postgres, 'pg_isready', '-h', '127.0.0.1', '-U', 'houdini', '-d', 'houdini'],
                                   capture_output=True, timeout=5)
                if r.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError('Disposable PostgreSQL did not start')
            common = ('--env-file', str(env))
            dash = start('dash', 'clubpenguin/runtime:lab-mobile', *common, '-e', 'MODE=dash',
                         '-v', str(ROOT / 'scripts/game-smoke.py') + ':/tmp/game-smoke.py:ro',
                         '-v', str(ROOT / 'containers/bots/bots/as2.py') + ':/tmp/bot_as2.py:ro',
                         '-v', str(ROOT / 'tests/puffle_sim/fixture_clients.py') + ':/tmp/fixture_clients.py:ro')
            start('login', 'clubpenguin/runtime:lab-mobile', *common, '-e', 'MODE=login')
            ready(dash, 'dash', 3000)
            ready(dash, 'login', 6112)
            # Verify no route out from the actual network used by the fixture.
            check = ('import socket\ntry: socket.create_connection(("1.1.1.1",443),timeout=2)\n'
                     'except OSError: pass\nelse: raise SystemExit("unexpected egress")')
            docker('exec', dash, 'python', '-c', check)
            count = 4 if args.visual_swf else 3
            registration = ("import json,runpy\nd=runpy.run_path('/tmp/game-smoke.py',run_name='fixture')\n"
                            "print(json.dumps([dict(zip(('name','password'),d['register']('http://dash:3000'))) "
                            "for _ in range(" + str(count) + ")]))")
            accounts = json.loads(docker('exec', dash, 'python', '-c', registration))
            names = [a['name'] for a in accounts]
            if not all(name.isascii() and name.isalpha() and name.startswith('lab') for name in names):
                raise RuntimeError('Unexpected synthetic names')
            in_names = ','.join("'" + name + "'" for name in names)
            query = 'SELECT username,id FROM penguin WHERE username IN (' + in_names + ') ORDER BY username'
            lines = docker('exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                           '-At', '-F', '|', '-c', query).splitlines()
            ids = {name: int(pid) for name, pid in (line.split('|') for line in lines)}
            if len(ids) != count:
                raise RuntimeError('Synthetic registration returned an unexpected user count')
            for account in accounts:
                account['id'] = ids[account['name']]
            id_list = ','.join(str(a['id']) for a in accounts)
            sql = ('WITH r AS (INSERT INTO penguin_igloo_room(penguin_id,type,flooring,location,locked) '
                   'SELECT id,1,0,1,false FROM penguin WHERE id IN (' + id_list + ') RETURNING id,penguin_id) '
                   'UPDATE penguin SET igloo=r.id FROM r WHERE penguin.id=r.penguin_id; '
                   'INSERT INTO penguin_puffle(penguin_id,puffle_id,name) SELECT id,0,\'Puffle\' '
                   'FROM penguin WHERE id IN (' + id_list + '); '
                   'INSERT INTO penguin_puffle(penguin_id,puffle_id,name) VALUES ('
                   + str(accounts[0]['id']) + ',1,\'Otro\');')
            docker('exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-v', 'ON_ERROR_STOP=1', '-c', sql)
            pet_rows = docker('exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-At', '-F', '|',
                              '-c', 'SELECT penguin_id,min(id) FROM penguin_puffle WHERE penguin_id IN ('
                              + id_list + ') GROUP BY penguin_id').splitlines()
            pets = {int(pid): int(pet) for pid, pet in (row.split('|') for row in pet_rows)}
            for account in accounts:
                account['pet'] = pets[account['id']]
            mounts = []
            for name in FILES:
                mounts += ['-v', str(overlay / name) + ':/app/houdini/houdini/' + name + ':ro']
            mounts += ['-v', str(ROOT / 'experiments/puffle_sim') + ':/src/experiments/puffle_sim:ro',
                       '-v', str(ROOT / 'tests/puffle_sim/fixture_world.py') + ':/tmp/fixture_world.py:ro']
            relay_env = []
            if args.provider == 'relay':
                # The host relay holds the OpenAI session; containers only get a
                # random per-run secret. Docker Desktop cannot share host Unix
                # sockets and internal networks cannot reach the host, so a
                # credential-free forwarder (alias 'relay') is the one exception:
                # it is on bridge + this network and only dials the relay port.
                secret_dir = tmp / 'relay'
                secret_dir.mkdir(mode=0o755)
                (secret_dir / 'secret').write_text(secrets.token_urlsafe(32))
                (secret_dir / 'secret').chmod(0o644)
                ready_file = tmp / 'relay-port'
                relay_args = [str(args.browser_python.absolute()), '-m', 'experiments.puffle_sim.host_relay', '--host', '127.0.0.1',
                              '--secret-file', str(secret_dir / 'secret'), '--ready-file', str(ready_file)]
                if args.allow_local_actions:
                    relay_args.append('--allow-local-actions')
                if args.spotify:
                    relay_args.append('--spotify')
                if args.music_mac:
                    relay_args.append('--music-mac')
                if args.music_safari_window_id is not None:
                    relay_args += ['--music-safari-window-id', str(args.music_safari_window_id)]
                relay_proc = subprocess.Popen(
                    relay_args,
                    cwd=str(ROOT), stdout=subprocess.DEVNULL)
                for _ in range(80):
                    if ready_file.exists() and ready_file.read_text():
                        break
                    if relay_proc.poll() is not None:
                        raise RuntimeError('Host relay failed to start')
                    time.sleep(0.25)
                else:
                    raise RuntimeError('Host relay did not start')
                relay_port = int(ready_file.read_text())
                relay_code = ('import asyncio\n'
                              'async def copy(r,w):\n'
                              ' try:\n'
                              '  while d:=await r.read(65536): w.write(d); await w.drain()\n'
                              ' finally: w.close()\n'
                              'async def handle(r,w):\n'
                              ' try:\n'
                              '  tr,tw=await asyncio.open_connection("host.docker.internal",' + str(relay_port) + ')\n'
                              '  await asyncio.gather(copy(r,tw),copy(tr,w))\n'
                              ' except OSError: w.close()\n'
                              'async def main():\n'
                              ' s=await asyncio.start_server(handle,"0.0.0.0",8790)\n'
                              ' async with s: await s.serve_forever()\n'
                              'asyncio.run(main())\n')
                relayfwd = prefix + '-relayfwd'
                docker('run', '-d', '--pull=never', '--name', relayfwd, '--label', label,
                       '--network', 'bridge', '--entrypoint', 'python',
                       'clubpenguin/runtime:lab-mobile', '-u', '-c', relay_code)
                containers.append(relayfwd)
                docker('network', 'connect', '--alias', 'relay', prefix, relayfwd)
                relay_env = ['-e', 'PUFFLE_FIXTURE_PROVIDER=relay',
                             '-v', str(secret_dir) + ':/run/puffle-relay:ro']
            if args.provider == 'spotify-sim':
                relay_env = ['-e', 'PUFFLE_FIXTURE_PROVIDER=spotify-sim']
            start('glaciar', 'clubpenguin/runtime:lab-mobile', *common, '-e', 'MODE=world', *relay_env,
                  '-e', 'PUFFLE_FIXTURE_ISOLATED=1', '-e', 'PYTHONPATH=/src:/app/houdini',
                  '-e', 'PUFFLE_FIXTURE_HUMANS=' + ','.join(str(accounts[i]['id']) for i in
                          human_account_indexes(bool(args.visual_swf), args.allow_local_actions,
                                                args.spotify, args.music_mac,
                                                args.music_safari_window_id is not None)),
                  '-e', 'PUFFLE_FIXTURE_ACTIONS=' + ('1' if args.allow_local_actions else '0'),
                  '--entrypoint', 'python', *mounts, command=('/tmp/fixture_world.py',))
            ready(dash, 'glaciar', 9876)
            # The world itself must still have no route out (relay alias excepted).
            docker('exec', prefix + '-glaciar', 'python', '-c', check)
            print('PASS: disposable Docker network has no outbound route.')
            if args.provider == 'relay':
                # Synthetic clients chat automatically; skip them so the only
                # real inference is the one the owner types by hand.
                print('SKIP: synthetic protocol clients (relay mode).')
            else:
                result = docker('exec', '-i', dash, 'python', '/tmp/fixture_clients.py',
                                input=json.dumps({'accounts': accounts[:3],
                                                  'spotify': args.provider == 'spotify-sim'}), timeout=120)
                print(result.strip())
            if args.visual_swf:
                web = start('web', 'clubpenguin/web:lab-mobile',
                            '-v', str(args.visual_swf.resolve()) + ':/srv/media/play/v2/client/engine.swf:ro')
                proxy = prefix + '-webproxy'
                docker('run', '-d', '--pull=never', '--name', proxy, '--label', label,
                       '--network', 'container:' + web, '-e', 'MODE=proxy', 'clubpenguin/runtime:lab-mobile')
                containers.append(proxy)
                # Docker Desktop does not publish ports from an internal network.
                # A credential-free forwarder is the only container on bridge;
                # the world, database and simulated worker remain internal.
                forward_code = '''import asyncio
async def copy(reader,writer):
 try:
  while data := await reader.read(65536):
   writer.write(data); await writer.drain()
 finally:
  writer.close()
async def handle(reader,writer):
 try:
  target_reader,target_writer=await asyncio.open_connection("web",8080)
  await asyncio.gather(copy(reader,target_writer),copy(target_reader,writer))
 except OSError:
  writer.close()
async def main():
 server=await asyncio.start_server(handle,"0.0.0.0",8080)
 async with server: await server.serve_forever()
asyncio.run(main())
'''
                forward = prefix + '-forward'
                docker('run', '-d', '--pull=never', '--name', forward, '--label', label,
                       '--network', 'bridge', '-p', '127.0.0.1::8080', '--entrypoint', 'python',
                       'clubpenguin/runtime:lab-mobile', '-u', '-c', forward_code)
                containers.append(forward)
                docker('network', 'connect', prefix, forward)
                port = docker('port', forward, '8080/tcp').strip().rsplit(':', 1)[1]
                screenshots = '/tmp/' + prefix + '-screens'
                browser_input = json.dumps({'base': 'http://127.0.0.1:' + port,
                                            'account': accounts[3], 'screenshots': screenshots})
                if args.hold:
                    args.hold.write_text(json.dumps({'base': 'http://127.0.0.1:' + port, 'accounts': accounts,
                                                     'prefix': prefix, 'dash': dash}))
                    print('HOLD ' + str(args.hold), flush=True)
                    deadline = time.time() + 45 * 60
                    while args.hold.exists() and time.time() < deadline:
                        time.sleep(2)
                    return
                result = run(str(args.browser_python.absolute()), str(ROOT / 'tests/puffle_sim/fixture_browser.py'),
                             input=browser_input, timeout=90)
                print(result.strip())
    finally:
        if relay_proc and relay_proc.poll() is None:
            relay_proc.terminate()
            relay_proc.wait(timeout=10)
        for container in containers:
            subprocess.run(['docker', 'rm', '-f', '-v', container], capture_output=True, timeout=30)
        if network_created:
            subprocess.run(['docker', 'network', 'rm', prefix], capture_output=True, timeout=30)


if __name__ == '__main__':
    main()

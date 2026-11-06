#!/usr/bin/env python3
"""Test an isolated release before publishing or deploying it.

Everything runs in containers on a private, *internal* Docker network (no route to
the Internet or to the Raspberry Pi), with throwaway volumes. Consequently no test
can reach Moonshot or any other paid API, and nothing here touches real data.

Checks, in order:
  1. PostgreSQL, Redis, login, Glaciar, Dash and the socket proxy start and listen.
  2. The internal network really has no egress (a connection to a public IP fails).
  3. Two synthetic accounts register, authenticate and share room 100.
  4. The WebSocket proxy forwards the AS2 handshake from an allowed origin.
  5. With --web-tag: nginx -> proxy -> login works for allowed origins, refuses other
     origins and paths, serves /healthz and /servers.xml, and registration works
     through nginx (the route the browser uses).

Cleanup removes only containers, volumes and the network carrying this run's label.
"""
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402

root = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')
FORBIDDEN_ENV = re.compile(r'(?i)(moonshot|kimi|openai|anthropic).*(key|token|secret)|^(MOONSHOT|KIMI)_')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tag', default='lab-002', help='tag for components without --component-tag')
    parser.add_argument('--component-tag', action='append', default=[], metavar='NAME=TAG')
    parser.add_argument('--web-tag', help='also test the web image (nginx + proxy + registration route)')
    parser.add_argument('--bots-tag', help='also test a separate bots service against real, disposable Houdini')
    parser.add_argument('--live-cast', action='store_true', help='26 simulated sessions and 10 protocol clients; requires --bots-tag')
    parser.add_argument('--voices', action='store_true', help='also test opt-in Phase 2 with a synthetic loopback HTTP provider')
    parser.add_argument('--life', action='store_true', help='also test Phase 3 invitations, following and returns with simulated providers')
    parser.add_argument('--recall', action='store_true', help='also test Phase 4 relationship notes on a writable PVC slice (simulated summaries)')
    parser.add_argument('--puffle', action='store_true', help='test the production world and two isolated companions')
    parser.add_argument('--keep-logs', action='store_true', help='print container logs even when tests pass')
    args = parser.parse_args()
    if args.live_cast and not args.bots_tag:
        parser.error('--live-cast requires --bots-tag')
    if args.voices and not args.live_cast:
        parser.error('--voices requires --live-cast')
    if args.life and not args.live_cast:
        parser.error('--life requires --live-cast')
    if args.recall and not args.live_cast:
        parser.error('--recall requires --live-cast')
    tags = {name: args.tag for name in ('runtime', 'database')}
    for item in args.component_tag:
        name, _, value = item.partition('=')
        tags[name] = value
    if args.web_tag:
        tags['web'] = args.web_tag
    if args.bots_tag:
        tags['bots'] = args.bots_tag
    for name, value in tags.items():
        if not TAG_RE.fullmatch(value) or value == 'latest':
            raise SystemExit(f'Invalid image tag for {name}')
    return args, tags


args, tags = parse_args()
run_id = secrets.token_hex(4)
prefix = 'penguin-test-' + run_id
label = f'clubpenguin.lab.test-run={run_id}'
containers = []


def docker(command, timeout=240, **kwargs):
    return subprocess.run(['docker'] + command, check=True, capture_output=True, text=True, timeout=timeout, **kwargs)


def exec_script(container, script_text, *argv):
    return docker(['exec', '-i', container, 'python', '-'] + list(argv), input=script_text)


def cleanup():
    """Remove only what this run created (identified by its own label)."""
    found = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label={label}'], capture_output=True, text=True).stdout.split()
    for name in found:
        subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True)
    subprocess.run(['docker', 'network', 'rm', prefix], capture_output=True)
    left = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label={label}'], capture_output=True, text=True).stdout.split()
    networks = subprocess.run(['docker', 'network', 'ls', '-q', '--filter', f'label={label}'], capture_output=True, text=True).stdout.split()
    if left or networks:
        print(f'WARNING: {len(left)} containers and {len(networks)} networks from this run were not removed')


def show_logs():
    for name in containers:
        result = subprocess.run(['docker', 'logs', '--tail', '8', name], capture_output=True, text=True)
        print(name + ':\n' + lablib.redact(result.stdout + result.stderr)[-1200:])


try:
    # --internal: containers can talk to each other but have no route outward.
    docker(['network', 'create', '--internal', '--label', label, prefix])
    with tempfile.TemporaryDirectory(prefix='penguin-test-') as temp:
        env_file = Path(temp) / 'runtime.env'
        env_text = ('POSTGRES_USER=houdini\nPOSTGRES_DB=houdini\nPOSTGRES_PASSWORD=' + secrets.token_hex(16)
                    + '\nPOSTGRES_HOST=postgres\nREDIS_HOST=redis\n')
        if any(FORBIDDEN_ENV.search(line.split('=', 1)[0]) for line in env_text.splitlines()):
            raise SystemExit('Provider credentials must never reach test containers')
        env_file.write_text(env_text)
        env_file.chmod(0o600)

        def start(alias, image, mode=None, extra=None, network=None):
            name = prefix + '-' + alias
            command = ['run', '-d', '--name', name, '--label', label]
            if mode != 'proxy':  # the proxy needs no database credentials
                command += ['--env-file', str(env_file)]
            command += ['--network', network] if network else ['--network', prefix, '--network-alias', alias]
            if mode:
                command += ['-e', 'MODE=' + mode]
            command += extra or []
            docker(command + [image])
            containers.append(name)
            return name

        runtime = f'clubpenguin/runtime:{tags["runtime"]}'
        puffle_extra = []
        if args.puffle:
            service_secret = Path(temp) / 'puffle-secret'
            service_secret.write_text(secrets.token_hex(32))
            service_secret.chmod(0o444)  # synthetic credential in the throwaway network
            puffle_extra = ['-e', 'PUFFLE_ENABLED=true', '-e', 'PUFFLE_SECRET_FILE=/etc/puffle/secret',
                            '-v', f'{service_secret}:/etc/puffle/secret:ro']
        postgres = start('postgres', f'clubpenguin/database:{tags["database"]}')
        start('redis', 'redis:7.4-alpine')
        for _ in range(60):
            ready = subprocess.run(['docker', 'exec', postgres, 'pg_isready', '-h', '127.0.0.1', '-U', 'houdini', '-d', 'houdini'], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Postgres did not start')
        for alias, mode in [('login', 'login'), ('glaciar', 'world'), ('dash', 'dash')]:
            extra = list(puffle_extra if mode == 'world' else [])
            if args.live_cast and mode in ('login', 'world'):
                extra += ['-e', 'WORLD_CAPACITY=60']
            start(alias, runtime, mode, extra=extra)
        start('proxy', runtime, 'proxy')
        if not args.live_cast or args.puffle:
            start('puffle', runtime, 'puffle', extra=puffle_extra or ['-e', 'PUFFLE_ENABLED=false'])
        dash = prefix + '-dash'

        waits = [('login', 6112), ('glaciar', 9876), ('dash', 3000), ('proxy', 8765)]
        if not args.live_cast or args.puffle:
            waits.append(('puffle', 9103))
        if args.web_tag:
            web = start('web', f'clubpenguin/web:{tags["web"]}')
            # Like the Kubernetes pod: the proxy shares the web container's network namespace,
            # which is where nginx sends /socket/ (127.0.0.1:8765).
            start('webproxy', runtime, 'proxy', extra=['-e', 'ALLOWED_ORIGINS=lab.test'], network=f'container:{web}')
            waits.append(('web', 8080))
        readiness = ('import socket,time\nfor host,port in %r:\n for attempt in range(80):\n  try:\n'
                     '   s=socket.create_connection((host,port),timeout=1);s.close();break\n'
                     '  except OSError:time.sleep(0.5)\n else:raise SystemExit("An application listener did not become ready")\n'
                     % (waits,))
        exec_script(dash, readiness)

        # 2. The test network must not reach the outside world.
        egress = ('import socket\ntry:\n socket.create_connection(("1.1.1.1",443),timeout=3)\n'
                  'except OSError:\n print("blocked")\nelse:\n raise SystemExit("EGRESS POSSIBLE")\n')
        if 'blocked' not in exec_script(dash, egress).stdout:
            raise RuntimeError('The isolated network has outbound access')
        print('PASS: test network has no outbound route (paid APIs are unreachable).')

        smoke = (root / 'scripts/game-smoke.py').read_text()
        probe = (root / 'scripts/ws_probe.py').read_text()
        print(exec_script(dash, smoke).stdout.strip())

        exec_script(dash, probe, '--url', 'ws://proxy:8765/socket/login', '--origin', 'http://localhost')
        print('PASS: WebSocket proxy forwards the AS2 handshake to the allowed login endpoint.')

        if args.web_tag:
            http = ('import sys,urllib.request\nr=urllib.request.urlopen(sys.argv[1],timeout=10)\n'
                    'body=r.read().decode()\nassert r.status==200,r.status\nassert sys.argv[2] in body,"unexpected body"\n')
            exec_script(dash, http, 'http://web:8080/healthz', 'ok')
            exec_script(dash, http, 'http://web:8080/servers.xml', 'id="3101"')
            asset_check = ('import hashlib,sys,urllib.request\n'
                           'data=urllib.request.urlopen(sys.argv[1],timeout=10).read()\n'
                           'assert hashlib.sha256(data).hexdigest()==sys.argv[2],"custom background mismatch"\n')
            for kind, digest in (
                ('photos', '2e197285cb8146c2dfbd5d2f4411cd861ce680f90bbfd31444a925ca9294c3cf'),
                ('icons', '9152b4ae337a035b373971e0143ed022ccfb95107ecaca96981fa2fc3f08f159'),
            ):
                exec_script(dash, asset_check,
                            f'http://web:8080/media/play/v2/content/global/clothing/{kind}/19155.swf', digest)
            for path, digest in (
                ('global/crumbs/global_crumbs.swf', '9469d222decbafc44328afd753d92d471c9998861901af69699b5a8f7592a156'),
                ('local/en/crumbs/local_crumbs.swf', '5ab86f8913da6e006b4f047ec330ed8f3926217d668bd20e6762400a9a4ba358'),
                ('local/es/crumbs/local_crumbs.swf', '63f786625090a3e1e93151d7fdf49ecfc8347e7e7e6a3676e0d7b2bcd8110cd3'),
                ('local/fr/crumbs/local_crumbs.swf', '09fca30e27bf5a241ff7f752f11cc0f3825cdba8110ac2d4db31382ae41e8aa5'),
                ('local/pt/crumbs/local_crumbs.swf', '945d13095090b25cfa7d446a9f60f99d6e099a6b6c2f95ad8978e81a6f494791'),
            ):
                exec_script(dash, asset_check, f'http://web:8080/media/play/v2/content/{path}', digest)
            print('PASS: custom background 19155 assets match the published files.')
            print('PASS: nginx serves /healthz and /servers.xml.')
            web_url = 'ws://web:8080/socket/login'
            exec_script(dash, probe, '--url', web_url, '--origin', 'http://localhost')
            exec_script(dash, probe, '--url', web_url, '--origin', 'https://lab.test')
            exec_script(dash, probe, '--url', web_url, '--origin', 'https://evil.test', '--expect-reject')
            exec_script(dash, probe, '--url', 'ws://web:8080/socket/unknown', '--origin', 'http://localhost', '--expect-reject')
            print('PASS: nginx -> proxy -> login accepts allowed origins and refuses other origins and paths.')
            print(exec_script(dash, smoke, '--dash-url', 'http://web:8080').stdout.strip().replace('PASS:', 'PASS (via nginx):', 1))
        if args.bots_tag:
            if args.live_cast:
                from live_bots_integration import check_live_bots as check_bots
            else:
                from bots_integration import check_bots
            check_bots(root, docker, exec_script, dash, postgres, prefix + '-glaciar',
                       prefix, label, tags['bots'], Path(temp),
                       **(dict(voices=args.voices, life=args.life, recall=args.recall)
                          if args.live_cast else {}))
        if args.puffle:
            from puffle_integration import check_puffle
            check_puffle(root, docker, exec_script, dash, postgres)
        if args.keep_logs:
            show_logs()
except (subprocess.CalledProcessError, RuntimeError, subprocess.TimeoutExpired) as exc:
    detail = getattr(exc, 'stdout', None) or ''
    print(lablib.redact(((getattr(exc, 'stderr', '') or '') + detail)[-2000:]) or str(exc))
    show_logs()
    cleanup()
    raise SystemExit(1)
except BaseException:
    cleanup()
    raise
cleanup()

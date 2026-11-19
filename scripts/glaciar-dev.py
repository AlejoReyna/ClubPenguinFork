#!/usr/bin/env python3
"""Local dev stack: Houdini, Dash, nginx, AI bots and optional dialog feed.

Brings up the same Docker layout the repo already used as ``glaciar-dev`` (persistent
Postgres on a named volume, web on 127.0.0.1:8080, socket proxy in the web pod).
With ``--dev-code`` (default) the working tree is bind-mounted for web shell, bots,
socket proxy, dialog feed and (via ``glaciar-dev-puffle.py``) puffle code.

Examples:
  python3 scripts/glaciar-dev.py up
  python3 scripts/glaciar-dev.py status
  python3 scripts/glaciar-dev.py bots          # 3 work bots (simulated LLM)
  python3 scripts/glaciar-dev.py bots --cast    # full persona cast (see docs/dev-mode.md)
  python3 scripts/glaciar-dev.py watch          # hot-reload (separate process)

See docs/dev-mode.md for Moonshot API mode, puffle and the full cast.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glaciar_dev_media  # noqa: E402

DEFAULT_PREFIX = 'glaciar-dev'
STATE_DIR = ROOT / '.local' / 'glaciar-dev'
ENV_FILE = STATE_DIR / 'runtime.env'
DIALOG_SWITCH = STATE_DIR / 'dialog-enabled'


def docker(argv, *, check=True, capture=True, input_text=None):
    result = subprocess.run(['docker', *argv], capture_output=capture, text=True, input=input_text)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or '')[-2500:]
        raise SystemExit('docker ' + ' '.join(argv[:6]) + ' failed:\n' + detail)
    return result


def running(name):
    return docker(['inspect', '-f', '{{.State.Running}}', name], check=False).stdout.strip() == 'true'


def inspect(name):
    r = docker(['inspect', name], check=False)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)[0]


def image_ok(ref):
    return docker(['image', 'inspect', ref], check=False).returncode == 0


def pick_image(kind, override):
    if override:
        if not image_ok(override):
            raise SystemExit(f'Docker image not found: {override}')
        return override
    legacy = {
        'runtime': 'clubpenguin/runtime:lab-mobile',
        'database': 'clubpenguin/database:lab-mobile',
        'web': 'clubpenguin/web:web-81caad0e8636',
        'bots': 'clubpenguin/bots:glaciar-dev',
    }
    for candidate in (legacy.get(kind), f'clubpenguin/{kind}:glaciar-dev', f'clubpenguin/{kind}:dev'):
        if candidate and image_ok(candidate):
            return candidate
    raise SystemExit(
        f'No local image for {kind}. Build first, e.g.:\n'
        f'  python3 scripts/fetch-sources.py --only houdini,dash,ruffle\n'
        f'  python3 scripts/build-images.py --tag glaciar-dev --components runtime,database,web,bots')


def ensure_env():
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    if ENV_FILE.is_file():
        return
    password = secrets.token_hex(16)
    ENV_FILE.write_text(
        'POSTGRES_USER=houdini\nPOSTGRES_DB=houdini\n'
        f'POSTGRES_PASSWORD={password}\nPOSTGRES_HOST=postgres\nREDIS_HOST=redis\n')
    ENV_FILE.chmod(0o600)


def env_map():
    ensure_env()
    return dict(line.split('=', 1) for line in ENV_FILE.read_text().splitlines() if '=' in line)


def run_container(name, image, *, network=None, alias=(), mounts=(), ports=(), extra_env=None,
                  network_mode=None, user=None, entrypoint=None, command=()):
    with tempfile.TemporaryDirectory() as temp:
        env_file = Path(temp) / 'env'
        merged = {**env_map(), **(extra_env or {})}
        env_file.write_text(''.join(f'{k}={v}\n' for k, v in merged.items()))
        env_file.chmod(0o600)
        argv = ['run', '-d', '--name', name, '--env-file', str(env_file)]
        if network_mode:
            argv += ['--network', network_mode]
        else:
            argv += ['--network', network, '--network-alias', *alias]
        for m in mounts:
            argv += ['-v', m]
        for p in ports:
            argv += ['-p', p]
        if user:
            argv += ['--user', user]
        if entrypoint:
            argv += ['--entrypoint', entrypoint]
        docker(['rm', '-f', name], check=False)
        docker(argv + [image, *command])


def wait_listeners(targets, container='glaciar-dev-dash', tries=90):
    script = (
        'import socket,time,sys\n'
        f'targets={targets!r}\n'
        'for host,port in targets:\n'
        ' for _ in range(%d):\n'
        '  try:\n'
        '   s=socket.create_connection((host,port),timeout=1);s.close();break\n'
        '  except OSError: time.sleep(0.5)\n'
        ' else:\n'
        '  print(host,port,file=sys.stderr); sys.exit(1)\n' % tries)
    docker(['exec', container, 'python3', '-c', script], check=True)


def dev_bots_mounts():
    return [
        f'{ROOT}/containers/bots/bots:/app/bots:ro',
        f'{ROOT}/containers/bots/cast:/etc/bots-cast:ro',
    ]


def dev_runtime_mounts():
    return [
        f'{ROOT}/containers/runtime/run.py:/app/run.py:ro',
        f'{ROOT}/containers/runtime/socket_proxy.py:/app/socket_proxy.py:ro',
        f'{ROOT}/containers/runtime/dialog_feed.py:/app/dialog_feed.py:ro',
        f'{ROOT}/production/lab:/app/production/lab:ro',
    ]


def party_stack(p):
    """Volume + env for the shared /party scene bus."""
    vol = f'{p}-lab-party'
    docker(['volume', 'create', vol], check=False)
    # Runtime and bots images run as UID 10001; Docker volumes default to root-only.
    docker(['run', '--rm', '-v', f'{vol}:/var/lib/lab-party', 'alpine:3.20', 'sh', '-c',
            'mkdir -p /var/lib/lab-party && chown 10001:10001 /var/lib/lab-party'], check=False)
    bus = '/var/lib/lab-party/scene.json'
    return vol, bus, {
        'LAB_PARTY_ENABLED': 'true',
        'LAB_PARTY_BUS_PATH': bus,
    }


def ensure_dialog_switch():
    DIALOG_SWITCH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not DIALOG_SWITCH.is_file():
        DIALOG_SWITCH.write_text('true\n')
        DIALOG_SWITCH.chmod(0o644)


def cmd_up(args):
    p = args.prefix
    net = f'{p}-net'
    runtime = pick_image('runtime', args.runtime_image)
    database = pick_image('database', args.database_image)
    web = pick_image('web', args.web_image)

    docker(['network', 'create', net], check=False)
    ensure_env()
    ensure_dialog_switch()
    docker(['volume', 'create', f'{p}-postgres-data'], check=False)
    docker(['volume', 'create', f'{p}-bots-dialog'], check=False)
    party_vol, party_bus, party_env = party_stack(p)

    names = [f'{p}-postgres', f'{p}-redis', f'{p}-login', f'{p}-glaciar', f'{p}-dash',
             f'{p}-web', f'{p}-webproxy', f'{p}-webdialog', f'{p}-puffle-stub']
    if args.recreate:
        for name in names:
            docker(['rm', '-f', name], check=False)

    if inspect(f'{p}-postgres') is None:
        run_container(f'{p}-postgres', database, network=net, alias=['postgres'],
                      mounts=[f'{p}-postgres-data:/var/lib/postgresql/data'])
    elif not running(f'{p}-postgres'):
        docker(['start', f'{p}-postgres'])

    if inspect(f'{p}-redis') is None:
        run_container(f'{p}-redis', 'redis:7.4-alpine', network=net, alias=['redis'])
    elif not running(f'{p}-redis'):
        docker(['start', f'{p}-redis'])

    postgres_name = f'{p}-postgres'
    for _ in range(60):
        if subprocess.run(['docker', 'exec', postgres_name, 'pg_isready', '-h', '127.0.0.1', '-U', 'houdini', '-d', 'houdini'],
                          capture_output=True).returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise SystemExit('PostgreSQL did not become ready')

    welcome_sql = ROOT / 'scripts/lab-welcome-puffle.sql'
    if welcome_sql.is_file():
        docker(['exec', '-i', postgres_name, 'psql', '-U', 'houdini', '-d', 'houdini',
                '-v', 'ON_ERROR_STOP=1'], input_text=welcome_sql.read_text())

    runtime_mounts = dev_runtime_mounts() if args.dev_code else []
    for alias, mode in (('login', 'login'), ('glaciar', 'world'), ('dash', 'dash')):
        name = f'{p}-{alias}'
        extra = {'MODE': mode}
        mounts = list(runtime_mounts)
        if mode == 'world':
            extra['PUFFLE_ENABLED'] = 'false'
            extra['WORLD_CAPACITY'] = os.environ.get('GLACIAR_DEV_WORLD_CAPACITY', '60')
            extra.update(party_env)
            mounts.append(f'{party_vol}:/var/lib/lab-party')
        info = inspect(name)
        if info is None:
            run_container(name, runtime, network=net, alias=[alias], extra_env=extra, mounts=mounts)
        elif args.dev_code and args.recreate:
            run_container(name, runtime, network=net, alias=[alias], extra_env=extra, mounts=mounts)
        elif not running(name):
            docker(['start', name])

    # Stub resolves nginx upstream ``puffle`` until glaciar-dev-puffle.py replaces it.
    stub = f'{p}-puffle-stub'
    real_puffle = inspect(f'{p}-puffle')
    if real_puffle is None and inspect(stub) is None:
        run_container(stub, 'alpine:3.20', network=net, alias=['puffle'],
                      command=['sleep', 'infinity'])
    elif inspect(stub) and not running(stub) and real_puffle is None:
        docker(['start', stub])

    web_name = f'{p}-web'
    if args.dev_code and not (ROOT / 'vendor' / 'vanilla-media' / 'media' / 'play' / 'v2' / 'client' / 'club_penguin.swf').is_file():
        raise SystemExit(
            'Falta vendor/vanilla-media (club_penguin.swf). Ejecuta:\n'
            '  python3 scripts/fetch-sources.py')
    web_mounts = glaciar_dev_media.dev_web_mounts() if args.dev_code else []
    web_rebuilt = False
    if inspect(web_name) is None:
        run_container(web_name, web, network=net, alias=['web'], ports=['127.0.0.1:8080:8080'],
                      mounts=web_mounts)
        web_rebuilt = True
    elif args.dev_code:
        info = inspect(web_name)
        if args.recreate or glaciar_dev_media.web_mounts_differ(info['HostConfig'].get('Binds'), web_mounts):
            ports = []
            for port, binds in (info['HostConfig'].get('PortBindings') or {}).items():
                for b in binds or []:
                    hp = b.get('HostIp') or ''
                    ports.append(f"{hp + ':' if hp else ''}{b['HostPort']}:{port.split('/')[0]}")
            run_container(web_name, web, network=net, alias=['web'], ports=ports or ['127.0.0.1:8080:8080'],
                          mounts=web_mounts)
            web_rebuilt = True
        elif not running(web_name):
            docker(['start', web_name])
    elif not running(web_name):
        docker(['start', web_name])

    def sidecar_attached(name):
        meta = inspect(name)
        if not meta:
            return False
        return meta['HostConfig'].get('NetworkMode') == f'container:{web_name}'

    proxy = f'{p}-webproxy'
    proxy_mounts = dev_runtime_mounts() if args.dev_code else []
    if inspect(proxy) is None or web_rebuilt or not sidecar_attached(proxy):
        run_container(proxy, runtime, network_mode=f'container:{web_name}',
                      extra_env={'MODE': 'proxy', 'ALLOWED_ORIGINS': 'localhost,127.0.0.1'},
                      mounts=proxy_mounts)
    elif not running(proxy):
        docker(['start', proxy])

    dialog = f'{p}-webdialog'
    dialog_mounts = [
        f'{p}-bots-dialog:/var/lib/bots-dialog:ro',
        f'{DIALOG_SWITCH}:/etc/bots-dialog/enabled:ro',
        *(dev_runtime_mounts() if args.dev_code else []),
    ]
    if inspect(dialog) is None or web_rebuilt or not sidecar_attached(dialog):
        run_container(dialog, runtime, network_mode=f'container:{web_name}',
                      extra_env={'MODE': 'dialog'}, mounts=dialog_mounts)
    elif not running(dialog):
        docker(['start', dialog])

    wait_listeners([('postgres', 5432), ('login', 6112), ('glaciar', 9876), ('dash', 3000), ('web', 8080)])
    # Start puffle / bots / loopback if they already exist from a prior session
    for extra in (f'{p}-puffle', f'{p}-bots', f'{p}-spotify-loopback'):
        if inspect(extra) and not running(extra):
            docker(['start', extra])

    origin = f'http://127.0.0.1:{args.port}'
    print(f'Listo: {origin}')
    print('  Registro: %s/register.html' % origin)
    print('  Siguiente: python3 scripts/glaciar-dev.py bots')
    if inspect(f'{p}-puffle') is None:
        print('  Puffle (opcional): python3 scripts/glaciar-dev-puffle.py --players <tu_id>')


def cmd_down(args):
    p = args.prefix
    for name in subprocess.run(['docker', 'ps', '-a', '--filter', f'name=^{p}-', '--format', '{{.Names}}'],
                               capture_output=True, text=True).stdout.split():
        docker(['stop', name], check=False)


def cmd_status(args):
    p = args.prefix
    rows = []
    for name in subprocess.run(['docker', 'ps', '-a', '--filter', f'name=^{p}-', '--format', '{{.Names}}'],
                               capture_output=True, text=True).stdout.split():
        info = inspect(name)
        if not info:
            continue
        state = info['State']
        rows.append((name, 'up' if state['Running'] else state.get('Status', 'down'),
                     info['Config']['Image'].split('/')[-1]))
    if not rows:
        print(f'No hay contenedores {p}-*. Ejecuta: python3 scripts/glaciar-dev.py up')
        return
    w = max(len(r[0]) for r in rows)
    for name, st, img in sorted(rows):
        print(f'{name.ljust(w)}  {st:12}  {img}')
    if running(f'{p}-bots'):
        print('\nBots:', docker(['logs', '--tail', '3', f'{p}-bots'], check=False).stdout.strip())


def _credentials_path(cast):
    name = 'cast-credentials.json' if cast else 'work-credentials.json'
    return ROOT / '.local' / 'bots' / name


def cmd_bots(args):
    os.environ['GLACIAR_DEV_PREFIX'] = args.prefix
    if args.cast:
        cast_cred = ROOT / '.local' / 'bots' / 'cast-credentials.json'
        if not cast_cred.is_file():
            raise SystemExit(
                'Falta .local/bots/cast-credentials.json. Créalo con:\n'
                '  python3 scripts/prepare-work-bots.py --cast containers/bots/cast '
                f'--output {cast_cred}')
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'provision-glaciar-dev-bots.py'),
                        '--credentials', str(cast_cred), *(['--skip-register'] if args.skip_register else [])],
                       check=True)
    else:
        cred = ROOT / '.local' / 'bots' / 'work-credentials.json'
        if not cred.is_file():
            subprocess.run([sys.executable, str(ROOT / 'scripts' / 'prepare-work-bots.py')], check=True)
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'provision-glaciar-dev-bots.py'),
                        *(['--skip-register'] if args.skip_register else [])], check=True)
    p = args.prefix
    name = f'{p}-bots'
    cred_path = _credentials_path(args.cast)
    party_vol, _, party_env = party_stack(p)
    mounts = [
        f'{cred_path}:/etc/bots/config.json:ro',
        f'{p}-bots-dialog:/var/lib/bots-dialog',
        f'{DIALOG_SWITCH}:/etc/bots-dialog/enabled:ro',
        f'{party_vol}:/var/lib/lab-party',
    ]
    if args.dev_code:
        mounts = dev_bots_mounts() + mounts
    music_vol = f'{p}-puffle-bots'
    extra_env = {
        'BOTS_PROVIDER_MODE': args.provider,
        'BOTS_LOGIN_HOST': 'login',
        'BOTS_WORLD_HOST': 'glaciar',
        **party_env,
    }
    if args.provider == 'api' and not os.environ.get('MOONSHOT_API_KEY'):
        raise SystemExit('Modo api: exporta MOONSHOT_API_KEY en el entorno antes de levantar bots.')
    if docker(['volume', 'inspect', music_vol], check=False).returncode == 0:
        mounts.append(f'{music_vol}:/etc/bots-music:ro')
        extra_env['BOTS_MUSIC_URL'] = 'http://puffle:9104'
        extra_env['BOTS_MUSIC_SECRET_FILE'] = '/etc/bots-music/secret'
    if args.dev_code or args.cast:
        extra_env['BOTS_CAST_DIR'] = '/etc/bots-cast'
    bots_image = pick_image('bots', args.bots_image)
    if not image_ok(bots_image) or args.rebuild_bots:
        docker(['build', '-t', bots_image, str(ROOT / 'containers' / 'bots')])
    run_container(name, bots_image, network=f'{p}-net', alias=['bots'], mounts=mounts, extra_env=extra_env)
    print(f'Bots en marcha ({name}, provider={args.provider}).')


def cmd_watch(args):
    script = ROOT / 'scripts' / 'glaciar-dev-watch.py'
    subprocess.run([sys.executable, str(script), '--prefix', args.prefix], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prefix', default=os.environ.get('GLACIAR_DEV_PREFIX', DEFAULT_PREFIX))
    sub = parser.add_subparsers(dest='cmd', required=True)

    up = sub.add_parser('up', help='create or start the stack')
    up.add_argument('--recreate', action='store_true', help='recreate core containers (keeps named volumes)')
    up.add_argument('--no-dev-code', dest='dev_code', action='store_false', help='use only image-baked files')
    up.add_argument('--port', type=int, default=8080)
    up.add_argument('--runtime-image')
    up.add_argument('--database-image')
    up.add_argument('--web-image')
    up.set_defaults(func=cmd_up)

    down = sub.add_parser('down', help='stop all prefix containers')
    down.set_defaults(func=cmd_down)

    st = sub.add_parser('status', help='container status')
    st.set_defaults(func=cmd_status)

    bots = sub.add_parser('bots', help='register accounts and (re)start the bots service')
    bots.add_argument('--skip-register', action='store_true', help='accounts already in Postgres')
    bots.add_argument('--cast', action='store_true', help='full persona cast instead of 3 work bots')
    bots.add_argument('--provider', choices=('simulated', 'api'), default='simulated')
    bots.add_argument('--no-dev-code', dest='dev_code', action='store_false')
    bots.add_argument('--rebuild-bots', action='store_true')
    bots.add_argument('--bots-image')
    bots.set_defaults(func=cmd_bots)

    watch = sub.add_parser('watch', help='hot-reload helper (delegates to glaciar-dev-watch.py)')
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.dev_code = getattr(args, 'dev_code', True)
    args.func(args)


if __name__ == '__main__':
    main()
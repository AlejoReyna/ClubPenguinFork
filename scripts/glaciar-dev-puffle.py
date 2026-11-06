#!/usr/bin/env python3
"""Turn on the server-side puffle (lab mode) in the running glaciar-dev Docker stack.

No download for the player: the broker and the lab device (production/puffle/host.py)
run in the `puffle` container; the browser uses /puffle/ on the same origin.

What it does, idempotently:
1. Docker volumes for the broker secret, the host encryption key (generated inside
   Docker, never written to the Mac or the repo) and the puffle data.
2. Replaces the `puffle` stub with the runtime image in MODE=puffle + PUFFLE_HOST_MODE=lab.
   With --dev-code (default) the working-tree puffle code is mounted read-only.
3. Recreates the world with PUFFLE_ENABLED=true and the same allowlist.
4. Recreates web (+ webproxy, which shares its network namespace) with the working-tree
   default.conf, index.html and puffle.html mounted.

Re-running it is safe. Players connected to the world must log in again.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CLIENT_ID_FILE = Path.home() / '.config/puffle-lab/spotify-client-id'


def docker(*argv, check=True, capture=True):
    result = subprocess.run(['docker', *argv], capture_output=capture, text=True)
    if check and result.returncode != 0:
        raise SystemExit('docker %s failed:\n%s' % (' '.join(argv[:3]), (result.stderr or '')[-1500:]))
    return result


def inspect(name):
    result = docker('inspect', name, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def aliases(info, network):
    found = ((info['NetworkSettings']['Networks'].get(network) or {}).get('Aliases') or [])
    return [a for a in found if a != info['Config']['Hostname'] and a != info['Id'][:12]]


def run(name, image, *, network, env, alias=(), mounts=(), ports=(), user=None, dry=False):
    """docker run -d with env in a 0600 temp file (database passwords never hit argv)."""
    with tempfile.TemporaryDirectory() as temp:
        env_file = Path(temp) / 'env'
        env_file.write_text(''.join(f'{k}={v}\n' for k, v in env.items()))
        env_file.chmod(0o600)
        argv = ['run', '-d', '--name', name, '--env-file', str(env_file), '--network', network]
        for a in alias:
            argv += ['--network-alias', a]
        for m in mounts:
            argv += ['-v', m]
        for p in ports:
            argv += ['-p', p]
        if user:
            argv += ['--user', user]
        print(('[dry-run] ' if dry else '') + 'docker run', name, image,
              'env=' + ','.join(sorted(env)), 'mounts=' + ','.join(m.split(':')[1] for m in mounts))
        if not dry:
            docker('rm', '-f', name, check=False)
            docker(*argv, image)


def env_of(info):
    return dict(item.split('=', 1) for item in info['Config']['Env'])


def ports_of(info):
    out = []
    for port, binds in (info['HostConfig'].get('PortBindings') or {}).items():
        for b in binds or []:
            out.append(f"{b['HostIp'] + ':' if b.get('HostIp') else ''}{b['HostPort']}:{port.split('/')[0]}")
    return out


def ensure_secret(image, volume, filename, dry):
    script = ('import os,secrets,pathlib\n'
              f'p=pathlib.Path("/v/{filename}")\n'
              'if not p.exists():\n p.write_text(secrets.token_hex(32))\n'
              'os.chown(p,10001,10001);os.chmod(p,0o400);os.chmod("/v",0o755)\n')
    print(('[dry-run] ' if dry else '') + f'secret volume {volume} ({filename})')
    if not dry:
        docker('volume', 'create', volume)
        docker('run', '--rm', '--network', 'none', '--user', '0', '--entrypoint', 'python',
               '-v', f'{volume}:/v', image, '-c', script)


def wait_http(container, url, method='GET', expect=200, tries=60):
    probe = ('import sys,urllib.request\n'
             'r=urllib.request.Request(sys.argv[1],method=sys.argv[2],headers={"X-Puffle":"1"})\n'
             'try:\n s=urllib.request.urlopen(r,timeout=2).status\n'
             'except urllib.error.HTTPError as e:\n s=e.code\n'
             'sys.exit(0 if s==int(sys.argv[3]) else 1)\n')
    for _ in range(tries):
        if docker('exec', container, 'python', '-c', probe, url, method, str(expect), check=False).returncode == 0:
            return True
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prefix', default=os.environ.get('GLACIAR_DEV_PREFIX', 'glaciar-dev'))
    parser.add_argument('--players', required=True,
                        help='comma-separated penguin ids allowed in lab mode (1-5), e.g. your own id')
    parser.add_argument('--origin', default='http://127.0.0.1:8080',
                        help='public origin of the game; Spotify needs 127.0.0.1 (not localhost) or https')
    parser.add_argument('--image', help='runtime image (default: the image of the current world)')
    parser.add_argument('--no-dev-code', dest='dev_code', action='store_false',
                        help='use the code baked in the image instead of the working tree')
    parser.add_argument('--conversation', choices=('simulated', 'silent'), default='simulated')
    parser.add_argument('--spotify-redirect',
                        help='reuse a redirect already registered in the Spotify dashboard, e.g. '
                             'http://127.0.0.1:1455/spotify/callback (a small container on that port '
                             'bounces it to /puffle/spotify/callback)')
    parser.add_argument('--force', action='store_true',
                        help='recreate world and web even if already configured (kicks connected players)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    players = [int(x) for x in args.players.split(',') if x.strip()]
    if not 1 <= len(players) <= 5:
        raise SystemExit('Lab mode needs 1 to 5 player ids (Spotify development mode limit).')
    p = args.prefix
    net = f'{p}-net'
    world, web, webproxy = inspect(f'{p}-glaciar'), inspect(f'{p}-web'), inspect(f'{p}-webproxy')
    if not world or not web:
        raise SystemExit(f'{p}-glaciar and {p}-web must exist (start the glaciar-dev stack first).')
    image = args.image or world['Config']['Image']
    client_id = os.environ.get('PUFFLE_SPOTIFY_CLIENT_ID', '').strip()
    if not client_id and CLIENT_ID_FILE.is_file():
        client_id = CLIENT_ID_FILE.read_text().strip()  # not a secret (PKCE has no client secret)
    if client_id and not (client_id.isalnum() and 16 <= len(client_id) <= 64):
        raise SystemExit('Invalid Spotify client ID.')
    allowed = ','.join(map(str, players))
    dry = args.dry_run

    ensure_secret(image, f'{p}-puffle-secret', 'secret', dry)
    ensure_secret(image, f'{p}-puffle-hostkey', 'key', dry)
    # Only the host and the bots service share this one (bots → /internal/music).
    ensure_secret(image, f'{p}-puffle-bots', 'secret', dry)
    if not dry:
        docker('volume', 'create', f'{p}-puffle-data')

    puffle_env = {
        'MODE': 'puffle', 'PUFFLE_ENABLED': 'true', 'PUFFLE_SECRET_FILE': '/etc/puffle/secret',
        'PUFFLE_HOST_MODE': 'lab', 'PUFFLE_HOST_KEY_FILE': '/etc/puffle-host/key',
        'PUFFLE_ALLOWED_PLAYERS': allowed, 'PUFFLE_PUBLIC_ORIGIN': args.origin,
        'PUFFLE_HOST_CONVERSATION': args.conversation,
        'PUFFLE_BOTS_SECRET_FILE': '/etc/puffle-bots/secret',
    }
    if client_id:
        puffle_env['PUFFLE_SPOTIFY_CLIENT_ID'] = client_id
    if args.spotify_redirect:
        puffle_env['PUFFLE_SPOTIFY_REDIRECT'] = args.spotify_redirect
    mounts = [f'{p}-puffle-secret:/etc/puffle:ro', f'{p}-puffle-hostkey:/etc/puffle-host:ro',
              f'{p}-puffle-bots:/etc/puffle-bots:ro',
              f'{p}-puffle-data:/var/lib/puffle']
    if args.dev_code:
        mounts += [f'{ROOT}/production/puffle:/app/production/puffle:ro',
                   f'{ROOT}/experiments/puffle_sim:/app/experiments/puffle_sim:ro']
    run(f'{p}-puffle', image, network=net, alias=['puffle'], env=puffle_env, mounts=mounts, dry=dry)
    if not dry and not wait_http(f'{p}-puffle', 'http://127.0.0.1:9104/healthz'):
        print(docker('logs', '--tail', '30', f'{p}-puffle', check=False).stderr)
        raise SystemExit('puffle-host did not become healthy')

    env = env_of(world)
    world_ready = (env.get('PUFFLE_ENABLED') == 'true' and env.get('PUFFLE_ALLOWED_PLAYERS') == allowed
                   and any(m.get('Destination') == '/etc/puffle' for m in world.get('Mounts', [])))
    env.update(PUFFLE_ENABLED='true', PUFFLE_SECRET_FILE='/etc/puffle/secret', PUFFLE_ALLOWED_PLAYERS=allowed)
    restart_bots = not (world_ready and not args.force)
    if not restart_bots:
        print(f'{p}-glaciar ya tiene el puffle activado; no se recrea (los jugadores siguen conectados).')
    else:
        run(f'{p}-glaciar', image, network=net, alias=aliases(world, net) or ['glaciar'], env=env,
            mounts=[f'{p}-puffle-secret:/etc/puffle:ro'], user=world['Config'].get('User') or None, dry=dry)

    web_mounts = [m for m in (web['HostConfig'].get('Binds') or [])
                  if not m.split(':')[1] in ('/etc/nginx/conf.d/default.conf', '/srv/play/puffle/index.html',
                                             '/srv/play/index.html')]
    web_mounts += [f'{ROOT}/containers/web/index.html:/srv/play/index.html:ro',
                   f'{ROOT}/containers/web/puffle.html:/srv/play/puffle/index.html:ro',
                   f'{ROOT}/containers/web/default.conf:/etc/nginx/conf.d/default.conf:ro']
    if sorted(web['HostConfig'].get('Binds') or []) == sorted(web_mounts) and not args.force:
        print(f'{p}-web ya monta la configuración del puffle; no se recrea.')
    else:
        run(f'{p}-web', web['Config']['Image'], network=net, alias=aliases(web, net) or ['web'], env=env_of(web),
            mounts=web_mounts, ports=ports_of(web), dry=dry)
        if webproxy:
            run(f'{p}-webproxy', webproxy['Config']['Image'], network=f'container:{p}-web', env=env_of(webproxy),
                user=webproxy['Config'].get('User') or None, dry=dry)
    loopback = f'{p}-spotify-loopback'
    if args.spotify_redirect:
        from urllib.parse import urlsplit
        port = urlsplit(args.spotify_redirect).port
        origin_port = urlsplit(args.origin).port
        if urlsplit(args.spotify_redirect).path != '/spotify/callback' or not port or port == origin_port:
            raise SystemExit('--spotify-redirect must be http://127.0.0.1:PORT/spotify/callback on another port')
        run(loopback, web['Config']['Image'], network=net, env={}, ports=[f'127.0.0.1:{port}:1455'],
            mounts=[f'{ROOT}/containers/web/spotify-loopback.conf:/etc/nginx/conf.d/default.conf:ro'], dry=dry)
    elif not dry:
        docker('rm', '-f', loopback, check=False)
    if not dry:
        if not wait_http(f'{p}-puffle', 'http://web:8080/puffle/api/session/status', 'POST'):
            raise SystemExit('nginx does not route /puffle/api/session/status to puffle-host')
        bots = inspect(f'{p}-bots')
        if restart_bots and bots and bots['State']['Running']:
            docker('restart', f'{p}-bots')  # they were connected to the old world
    print('Listo. Abre %s/puffle/ (redirect de Spotify: %s).' % (
        args.origin, args.spotify_redirect or args.origin + '/puffle/spotify/callback'))
    if not client_id:
        print('Falta el Client ID de Spotify: define PUFFLE_SPOTIFY_CLIENT_ID o %s y vuelve a ejecutar.' % CLIENT_ID_FILE)


if __name__ == '__main__':
    main()

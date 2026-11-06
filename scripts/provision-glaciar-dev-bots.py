#!/usr/bin/env python3
"""Register work bots on a running glaciar-dev Docker stack and start the bots service.

Uses .local/bots/work-credentials.json (create with prepare-work-bots.py if missing).
Does not touch Kubernetes or Moonshot.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bot_accounts  # noqa: E402

PREFIX = os.environ.get('GLACIAR_DEV_PREFIX', 'glaciar-dev')
NET = f'{PREFIX}-net'
CREDENTIALS = ROOT / '.local/bots/work-credentials.json'
MAKEOVER_SQL = ROOT / 'scripts/bot-makeover.sql'
CADENCE_LOCAL_SQL = ROOT / 'scripts/cadence-dj-local.sql'
STARTING_COINS_SQL = ROOT / 'scripts/lab-starting-coins.sql'
BOTS_IMAGE = os.environ.get('GLACIAR_DEV_BOTS_IMAGE', 'clubpenguin/bots:glaciar-dev')


def sync_work_credentials(path):
    """Rewrite config from bot_accounts while keeping existing passwords."""
    fresh = bot_accounts.work_config_document()
    if path.is_file():
        old = json.loads(path.read_text())
        passwords = {b['username']: b['password'] for b in old.get('bots', []) if b.get('password')}
        for bot in fresh['bots']:
            if bot['username'] in passwords:
                bot['password'] = passwords[bot['username']]
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(json.dumps(fresh, indent=2, ensure_ascii=False) + '\n')
    os.chmod(path, 0o600)


def docker(argv, input_text=None, check=True):
    result = subprocess.run(['docker', *argv], capture_output=True, text=True,
                            input=input_text)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or '')[-2000:]
        raise RuntimeError('docker ' + ' '.join(argv[:8]) + ' failed:\n' + detail)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default=PREFIX)
    parser.add_argument('--credentials', type=Path, default=CREDENTIALS)
    parser.add_argument('--skip-register', action='store_true',
                        help='accounts already registered; still syncs config and applies SQL')
    parser.add_argument('--apply-sql', action='store_true',
                        help='run Cadence/makeover SQL even with --skip-register')
    args = parser.parse_args()
    prefix = args.prefix
    cred_path = args.credentials.expanduser().resolve()
    if not cred_path.is_relative_to((ROOT / '.local').resolve()):
        parser.error('Credentials must live under .local/')
    if not cred_path.is_file():
        raise SystemExit('Missing %s — run: python3 scripts/prepare-work-bots.py' % cred_path)
    sync_work_credentials(cred_path)

    postgres = f'{prefix}-postgres'
    dash = f'{prefix}-dash'
    bots_name = f'{prefix}-bots'

    def apply_sql(full_makeover=False):
        paths = [CADENCE_LOCAL_SQL]
        if STARTING_COINS_SQL.is_file():
            paths.append(STARTING_COINS_SQL)
        if full_makeover:
            paths.append(MAKEOVER_SQL)
        for sql_path in paths:
            if sql_path.is_file():
                docker(['exec', '-i', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                        '-v', 'ON_ERROR_STOP=1'],
                       input_text=sql_path.read_text())

    if not args.skip_register:
        document = json.loads(cred_path.read_text())
        program = bot_accounts.registration_program()
        docker(['exec', '-i', dash, 'python', '-c', program],
               input_text=json.dumps({'bots': document['bots']}))
        docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                '-v', 'ON_ERROR_STOP=1', '-c', bot_accounts.nickname_correction_sql()])
        apply_sql(full_makeover=True)
        auth = bot_accounts.authentication_program()
        out = docker(['exec', '-i', dash, 'python', '-c', auth],
                     input_text=cred_path.read_text())
        if json.loads(out.stdout) != {'authenticated': 3}:
            raise RuntimeError('Bot authentication check failed')
    elif args.apply_sql:
        apply_sql(full_makeover=False)

    # Music orders said to a bot («Cobra, pon despacito») go to the puffle host when
    # scripts/glaciar-dev-puffle.py created the shared bots secret volume.
    music_volume = f'{prefix}-puffle-bots'
    music = []
    if docker(['volume', 'inspect', music_volume], check=False).returncode == 0:
        music = ['-e', 'BOTS_MUSIC_URL=http://puffle:9104',
                 '-e', 'BOTS_MUSIC_SECRET_FILE=/etc/bots-music/secret',
                 '-v', f'{music_volume}:/etc/bots-music:ro']
    docker(['build', '-t', BOTS_IMAGE, str(ROOT / 'containers/bots')], check=True)
    docker(['rm', '-f', bots_name], check=False)
    docker(['run', '-d', '--name', bots_name, '--network', NET,
            '--network-alias', 'bots',
            '-e', 'BOTS_PROVIDER_MODE=simulated',
            '-e', 'BOTS_LOGIN_HOST=login',
            '-e', 'BOTS_WORLD_HOST=glaciar',
            '-v', f'{cred_path}:/etc/bots/config.json:ro',
            *music,
            BOTS_IMAGE])
    if music:
        print('Música: los jugadores con Spotify conectado pueden pedírsela a cualquier bot.')
    print('Bots service started (%s). Cadence at the DJ booth (120); Viterbos at 330.'
          % bots_name)


if __name__ == '__main__':
    main()

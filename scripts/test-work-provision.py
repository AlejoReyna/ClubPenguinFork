#!/usr/bin/env python3
"""Prove work-role provisioning on a disposable Houdini, then delete it.

Runs prepare-work-bots.py and create-bot-accounts.py --work-roles. The
Kubernetes client is a shim: it satisfies the script's destination checks and
forwards exec to this run's containers. It never opens the Pi tunnel and never
reads ~/.kube/clubpenguin-pi.yaml. The kubeconfig file on disk points at
127.0.0.1:1 so a real kubectl cannot reach the Pi if the shim is bypassed.

The shim's `config view` still reports https://127.0.0.1:16443, because that
is the check inside create-bot-accounts.py. No packet is sent there.

Images must already be local. This script does not pull, build or publish.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bot_accounts  # noqa: E402

RUNTIME = 'clubpenguin/runtime:rt-origin-001'
DATABASE = 'clubpenguin/database:lab-002'
WEB = 'clubpenguin/web:lab-006'
REDIS = 'redis:7.4-alpine'
BOTS = 'clubpenguin/bots:bots-work-local'
NODE = 'bank-node'
ROLES = ('baristaia', 'djia', 'pizzeroia')


def docker(args, timeout=180, input_text=None, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True,
                            timeout=timeout, input=input_text)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or '')[-1500:]
        raise RuntimeError('docker ' + ' '.join(args[:6]) + ' failed\n' + detail)
    return result


def main():
    missing = [name for name in (RUNTIME, DATABASE, WEB, REDIS) if docker(
        ['image', 'inspect', name], check=False).returncode != 0]
    if missing:
        raise SystemExit('Missing local images (will not pull): ' + ', '.join(missing))
    compile(bot_accounts.registration_program(), 'registration', 'exec')
    compile(bot_accounts.authentication_program(), 'authentication', 'exec')
    compile(bot_accounts.roster_approval_program(), 'roster', 'exec')

    run_id = secrets.token_hex(4)
    prefix = 'penguin-bank-' + run_id
    label = 'clubpenguin.lab.bank=' + run_id
    bank = ROOT / '.local' / 'bots' / ('provision-bank-' + run_id)
    shim_dir = Path('/tmp') / ('penguin-bank-shim-' + run_id)
    secret_body = bank / 'secret-body.json'
    marker = bank / 'secret-applied'
    state_path = bank / 'shim-state.json'
    kubeconfig = bank / 'kubeconfig'
    credentials = bank / 'work-credentials.json'
    created = []

    def cleanup():
        found = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=' + label],
                               capture_output=True, text=True).stdout.split()
        for name in found:
            subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', prefix], capture_output=True)
        shutil.rmtree(bank, ignore_errors=True)
        shutil.rmtree(shim_dir, ignore_errors=True)

    try:
        bank.mkdir(parents=True, mode=0o700)
        bank.chmod(0o700)
        shim_dir.mkdir(mode=0o700)
        kubeconfig.write_text(
            'apiVersion: v1\nkind: Config\ncurrent-context: disposable-bank\n'
            'clusters:\n- name: disposable\n  cluster:\n    server: https://127.0.0.1:1\n')
        kubeconfig.chmod(0o600)
        state = {
            'docker': shutil.which('docker'),
            'postgres': prefix + '-postgres',
            'dash': prefix + '-dash',
            'replicas': 1,
            'node': NODE,
            'secret_path': str(secret_body),
            'marker_path': str(marker),
            'log_path': str(bank / 'shim.log'),
            'allowed_prefix': str(bank.resolve()),
        }
        if not state['docker']:
            raise RuntimeError('docker is not on PATH')
        state_path.write_text(json.dumps(state))
        state_path.chmod(0o600)
        (shim_dir / 'kubectl').write_text(SHIM)
        (shim_dir / 'kubectl').chmod(0o755)

        docker(['network', 'create', '--internal', '--label', label, prefix])
        env_file = bank / 'runtime.env'
        env_file.write_text('POSTGRES_USER=houdini\nPOSTGRES_DB=houdini\nPOSTGRES_PASSWORD='
                            + secrets.token_hex(16) + '\nPOSTGRES_HOST=postgres\nREDIS_HOST=redis\n')
        env_file.chmod(0o600)

        def start(alias, image, mode=None):
            name = prefix + '-' + alias
            command = ['run', '-d', '--pull=never', '--name', name, '--label', label,
                       '--network', prefix, '--network-alias', alias, '--env-file', str(env_file)]
            if mode:
                command += ['-e', 'MODE=' + mode]
            docker(command + [image])
            created.append(name)
            return name

        postgres = start('postgres', DATABASE)
        start('redis', REDIS)
        for _ in range(60):
            if docker(['exec', postgres, 'pg_isready', '-h', '127.0.0.1', '-U', 'houdini', '-d', 'houdini'],
                      check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Postgres did not start')
        start('login', RUNTIME, 'login')
        start('glaciar', RUNTIME, 'world')
        dash = start('dash', RUNTIME, 'dash')
        start('web', WEB)
        wait = ('import socket,time\n'
                'for host,port in [("login",6112),("glaciar",9876),("dash",3000),("web",8080)]:\n'
                '  sock=None\n'
                '  for _attempt in range(80):\n'
                '    try:\n'
                '      sock=socket.create_connection((host,port),1); sock.close(); break\n'
                '    except OSError: time.sleep(0.5)\n'
                '  else:\n'
                '    raise SystemExit("listener down: %s" % host)\n'
                'import socket as s\n'
                'try:\n s.create_connection(("1.1.1.1",443),3)\n'
                'except OSError:\n print("blocked")\n'
                'else:\n raise SystemExit("egress")\n')
        if 'blocked' not in docker(['exec', '-i', dash, 'python', '-c', wait]).stdout:
            raise RuntimeError('The disposable network has outbound access')
        print('PASS: disposable bank is up and has no outbound route.')

        run_py('scripts/prepare-work-bots.py', '--output', str(credentials))
        if not credentials.exists() or credentials.stat().st_mode & 0o077:
            raise RuntimeError('prepare-work-bots did not leave a private credentials file')
        before = role_rows(postgres)
        if before:
            raise RuntimeError('The fresh database already had role accounts')
        preview = run_provision(shim_dir, kubeconfig, credentials, state_path)
        if preview.returncode != 0 or 'Preview: 3 new accounts, 0 existing' not in preview.stdout:
            raise RuntimeError('Preview did not report 3 new accounts\n' + (preview.stderr or preview.stdout)[-800:])
        if role_rows(postgres) or marker.exists():
            raise RuntimeError('Preview created accounts or a Secret')
        print('PASS: prepare-work-bots.py wrote a private candidate; preview changed nothing.')

        blocked = run_provision(shim_dir, kubeconfig, credentials, state_path, execute=True)
        if blocked.returncode == 0 or 'Stop the bots Deployment' not in blocked.stderr:
            raise RuntimeError('Provisioning with replicas=1 was not rejected')
        if role_rows(postgres) or marker.exists():
            raise RuntimeError('A rejected migration created accounts or a Secret')
        print('PASS: create-bot-accounts.py --work-roles --execute refuses a Deployment that is not at 0 replicas.')

        state['replicas'] = 0
        state_path.write_text(json.dumps(state))
        first = run_provision(shim_dir, kubeconfig, credentials, state_path, execute=True)
        require_ok(first, bank / 'shim.log')
        rows = role_rows(postgres)
        assert_roles(rows)
        summary = json.loads((bank / 'provisioning.json').read_text())
        if summary.get('created') != 3 or summary.get('reused') != 0 or summary.get('roster_approved') != 3:
            raise RuntimeError('Provisioning summary did not record 3 new approved accounts')
        if summary.get('moonshot_calls') != 0:
            raise RuntimeError('Provisioning recorded an LLM call')
        secret_check(secret_body)
        ids = {row['username']: row['id'] for row in rows}
        print('PASS: 3 ordinary accounts, nicknames %s, approval_es and roster bit 8; Secret stored by the shim.'
              % ', '.join(row['nickname'] for row in rows))

        second = run_provision(shim_dir, kubeconfig, credentials, state_path, execute=True)
        require_ok(second, bank / 'shim.log')
        again = role_rows(postgres)
        if {row['username']: row['id'] for row in again} != ids or json.loads(
                (bank / 'provisioning.json').read_text()).get('created') != 0:
            raise RuntimeError('A second provision created accounts or changed ids')
        print('PASS: second --execute created 0 accounts and kept the same ids.')

        docker(['restart', '-t', '5', prefix + '-glaciar'])
        docker(['exec', '-i', dash, 'python', '-c', wait])
        third = run_provision(shim_dir, kubeconfig, credentials, state_path, execute=True)
        require_ok(third, bank / 'shim.log')
        if {row['username']: row['id'] for row in role_rows(postgres)} != ids:
            raise RuntimeError('World restart followed by provision changed account ids')
        print('PASS: after a world restart the same three accounts are reused; roster bit 8 still holds.')

        check_cast_provision(shim_dir, kubeconfig, credentials, state_path, state, postgres, bank)

        walk_positions(prefix, label, dash, postgres, credentials, ids)
    finally:
        cleanup()
        left = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=' + label],
                              capture_output=True, text=True).stdout.split()
        if left or bank.exists():
            print('WARNING: disposable bank cleanup left %d containers' % len(left))
        else:
            print('PASS: disposable bank, credentials and shim Secret removed.')


def role_rows(postgres):
    query = ("SELECT COALESCE(json_agg(json_build_object("
             "'id',id,'username',username,'nickname',nickname,'moderator',moderator,"
             "'stealth_moderator',stealth_moderator,'approval_es',approval_es,"
             "'active',active) ORDER BY username), '[]'::json) "
             "FROM penguin WHERE username IN ('baristaia','djia','pizzeroia');")
    raw = docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                  '-At', '-v', 'ON_ERROR_STOP=1', '-c', query]).stdout.strip()
    return json.loads(raw)


def assert_roles(rows):
    expected = {name.lower(): bot_accounts.WORK_VISIBLE[name][0] for name, *_ in bot_accounts.WORK_ROLES}
    if sorted(row['username'] for row in rows) != sorted(expected):
        raise RuntimeError('Role account set mismatch')
    for row in rows:
        if (row['nickname'] != expected[row['username']] or row['moderator']
                or row['stealth_moderator'] or not row['active'] or not row['approval_es']):
            raise RuntimeError('Role row is not an ordinary account with the exact Spanish name')


def secret_check(path):
    if not path.exists() or path.stat().st_mode & 0o077:
        raise RuntimeError('Shim Secret was not stored as a private file')
    body = json.loads(path.read_text())
    encoded = body['data']['config.json']
    import base64
    document = json.loads(base64.b64decode(encoded))
    names = {bot['username']: bot['nickname'] for bot in document['bots']}
    expected = {name.lower(): bot_accounts.WORK_VISIBLE[name][0] for name, *_ in bot_accounts.WORK_ROLES}
    if names != expected:
        raise RuntimeError('Secret config does not name the three roles')
    if any('password' not in bot or len(bot['password']) < 8 for bot in document['bots']):
        raise RuntimeError('Secret config is missing passwords')
    path.unlink()


def run_py(*args):
    result = subprocess.run([sys.executable, '-I', *args], cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or 'command failed')[-1500:])
    return result


def run_provision(shim_dir, kubeconfig, credentials, state_path, execute=False):
    env = os.environ.copy()
    env['PATH'] = str(shim_dir) + os.pathsep + env.get('PATH', '')
    env['CP_BANK_STATE'] = str(state_path)
    command = [sys.executable, '-I', str(ROOT / 'scripts/create-bot-accounts.py'),
               '--kubeconfig', str(kubeconfig), '--expected-node', NODE,
               '--work-roles', '--credentials-file', str(credentials)]
    if execute:
        command.append('--execute')
    return subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, env=env)


def check_cast_provision(shim_dir, kubeconfig, credentials, state_path, state, postgres, bank):
    """All writes go through this bank's shim, never a real Kubernetes client."""
    import hashlib
    cast_file = bank / 'cast.json'
    cast_file.write_text(json.dumps([json.loads(p.read_text())
                                    for p in sorted((ROOT / 'containers/bots/cast').glob('*.json'))]))
    candidate = bank / 'cast-credentials.json'
    run_py('scripts/prepare-work-bots.py', '--cast', str(cast_file),
           '--base-credentials', str(credentials), '--output', str(candidate))
    fingerprint = hashlib.sha256(candidate.read_bytes()).hexdigest()
    document = json.loads(candidate.read_text())
    if len(document['bots']) != 26 or document['bots'][:3] != json.loads(credentials.read_text())['bots']:
        raise RuntimeError('Cast preparation did not preserve the three base identities')
    refused = subprocess.run([sys.executable, '-I', 'scripts/prepare-work-bots.py', '--cast', str(cast_file),
                              '--output', str(candidate)], cwd=ROOT, capture_output=True, text=True)
    if refused.returncode == 0 or hashlib.sha256(candidate.read_bytes()).hexdigest() != fingerprint:
        raise RuntimeError('Existing private candidate was overwritten')

    def provision(execute=False, path=candidate):
        env = os.environ.copy()
        env['PATH'] = str(shim_dir) + os.pathsep + env.get('PATH', '')
        env['CP_BANK_STATE'] = str(state_path)
        command = [sys.executable, '-I', str(ROOT / 'scripts/create-bot-accounts.py'),
                   '--kubeconfig', str(kubeconfig), '--expected-node', NODE,
                   '--cast', str(cast_file), '--credentials-file', str(path)]
        if execute:
            command.append('--execute')
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=env)

    def count():
        return int(docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-At',
                           '-c', 'SELECT count(*) FROM penguin;']).stdout.strip())

    before = count()
    preview = provision()
    require_ok(preview, bank / 'shim.log')
    if 'Preview: 23 new accounts, 3 existing' not in preview.stdout or count() != before:
        raise RuntimeError('Cast preview changed accounts or reported the wrong roster')
    state['replicas'] = 1
    state_path.write_text(json.dumps(state))
    blocked = provision(True)
    if blocked.returncode == 0 or count() != before:
        raise RuntimeError('Cast provisioning did not refuse replicas=1')
    state['replicas'], state['pods'] = 0, 1
    state_path.write_text(json.dumps(state))
    blocked = provision(True)
    if blocked.returncode == 0 or 'no bots pods remain' not in blocked.stderr or count() != before:
        raise RuntimeError('Cast provisioning did not refuse lingering pods')
    state['pods'] = 0
    state_path.write_text(json.dumps(state))
    for created in (23, 0):
        require_ok(provision(True), bank / 'shim.log')
        summary = json.loads((bank / 'provisioning.json').read_text())
        if summary.get('created') != created or summary.get('roster_approved') != 26:
            raise RuntimeError('Cast provisioning summary or roster verification failed')
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != fingerprint:
            raise RuntimeError('Cast provisioner overwrote the private candidate')
    if count() != before + 23:
        raise RuntimeError('Cast retry created duplicate accounts')
    no_private = provision(True, path=bank / 'missing.json')
    if no_private.returncode == 0:
        raise RuntimeError('Cast provisioner adopted accounts without a private file')
    secret_hash = hashlib.sha256(Path(state['secret_path']).read_bytes()).hexdigest()
    for statement, restore in (
        ("UPDATE penguin SET nickname='Renamed' WHERE username='lolaritmo'",
         "UPDATE penguin SET nickname='Lola Ritmo' WHERE username='lolaritmo'"),
        ("UPDATE penguin SET approval_es=false WHERE username='lolaritmo'",
         "UPDATE penguin SET approval_es=true WHERE username='lolaritmo'"),
        ("UPDATE penguin SET moderator=true WHERE username='lolaritmo'",
         "UPDATE penguin SET moderator=false WHERE username='lolaritmo'"),
    ):
        docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-c', statement])
        rejected = provision(True)
        if rejected.returncode == 0 or hashlib.sha256(Path(state['secret_path']).read_bytes()).hexdigest() != secret_hash:
            raise RuntimeError('Invalid name, approval or privilege reached the shim Secret')
        docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-c', restore])
    print('PASS: --cast preview; replicas/pod/file guards; 23 new + 3 preserved accounts; '
          '26 names, rooms and Spanish roster bits verified; retry creates zero; candidate unchanged; '
          'renamed, unapproved and privileged accounts refused before Secret publication.')


def require_ok(result, log_path):
    if result.returncode != 0:
        logged = log_path.read_text()[-1200:] if log_path.exists() else ''
        raise RuntimeError(((result.stderr or result.stdout or 'provision failed') + '\n' + logged)[-1500:])


def walk_positions(prefix, label, dash, postgres, credentials, ids):
    inspected = docker(['image', 'inspect', BOTS], check=False)
    if inspected.returncode != 0:
        print('SKIP: walk capture; bots image is not local and will not be built.')
        return
    import hashlib
    source = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (ROOT / 'containers/bots/bots').glob('*.py')}
    check = ('import pathlib,hashlib,json\n'
             'print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() '
             'for p in pathlib.Path("/app/bots").glob("*.py")}))')
    actual = docker(['run', '--rm', '--pull=never', '--network=none', '--read-only',
                     '--entrypoint', 'python', BOTS, '-c', check])
    if json.loads(actual.stdout) != source:
        print('SKIP: walk capture; bots image does not match the current source.')
        return
    document = json.loads(credentials.read_text())
    document['behavior'].update(walk_min_delay=0.4, walk_max_delay=0.8,
                                first_move_delay_min=0.2, first_move_delay_max=0.4)
    config_path = credentials.with_name('walk-config.json')
    config_path.write_text(json.dumps(document))
    config_path.chmod(0o444)
    service = prefix + '-bots'
    docker(['run', '-d', '--pull=never', '--name', service, '--label', label,
            '--network', prefix, '--network-alias', 'bots', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--mount', 'type=bind,source=%s,target=/etc/bots/config.json,readonly' % config_path,
            '-e', 'BOTS_PROVIDER_MODE=simulated', BOTS])
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        metrics = docker(['exec', dash, 'python', '-c',
                          'import urllib.request\n'
                          'print(urllib.request.urlopen("http://bots:9102/metrics",timeout=3).read().decode())'],
                         check=False)
        connected = [line for line in metrics.stdout.splitlines() if line.startswith('bots_connected{')]
        if len(connected) == 3 and all(line.rsplit(' ', 1)[-1] == '1' for line in connected):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError('Bots service did not connect in the disposable bank')
    before = {row['username']: row['id'] for row in role_rows(postgres)}
    if before != ids:
        raise RuntimeError('Starting the bots service created or replaced accounts')
    observer = {'username': 'auditwk', 'nickname': 'AuditWk', 'password': secrets.token_urlsafe(16)}
    register = (ROOT / 'scripts/bot_accounts.py').read_text() + '''
import sys, json
payload = json.load(sys.stdin)
register("http://dash:3000", payload["nickname"], payload["password"])
print("registered")
'''
    registered = docker(['exec', '-i', dash, 'python', '-c', register], input_text=json.dumps(observer))
    if registered.stdout.strip() != 'registered':
        raise RuntimeError('Walk observer was not registered')
    protocol = (ROOT / 'containers/bots/bots/as2.py').read_text()
    observe = protocol + '''
import sys, json
async def observe():
    doc = json.load(sys.stdin)
    identity = doc["observer"]
    login = await AS2Connection.connect("login", 6112)
    try:
        rndk = await login.handshake(5)
        fields = await login.login_legacy(identity["username"], login_server_password(identity["password"], rndk), 5)
    finally:
        await login.close()
    logged = parse_login(fields)
    pid, key = logged.player_id, logged.login_key
    conn = await AS2Connection.connect("glaciar", 9876)
    found = {}
    try:
        rndk = await conn.handshake(5)
        await conn.login_world(identity["username"], logged, rndk, 5)
        await conn.join_world(int(pid), key, 5)
        areas = doc["areas"]
        stationary = set(doc.get("stationary") or [])
        for name, room in doc["rooms"]:
            await conn.join_room(room, 5)
            points = []
            until = asyncio.get_running_loop().time() + 8
            need = 0 if name in stationary else 2
            while asyncio.get_running_loop().time() < until and len(points) < need:
                try:
                    raw = await conn.read_line(0.5)
                except TimeoutError:
                    continue
                if not raw.startswith("%"):
                    continue
                frame = Frame.parse(raw)
                if frame.op == "sp" and frame.args and frame.args[0] == str(doc["ids"][name]):
                    x, y = int(frame.args[1]), int(frame.args[2])
                    box = areas[name]
                    if not (box[0] <= x <= box[1] and box[2] <= y <= box[3]):
                        raise SystemExit("outside")
                    points.append([x, y])
            found[name] = points
        print(json.dumps({"points": found}))
    finally:
        await conn.close()
asyncio.run(observe())
'''
    areas = {name.lower(): [bot_accounts.MEASURED_WORK_AREAS[name][key]
                            for key in ('min_x', 'max_x', 'min_y', 'max_y')]
             for name in bot_accounts.MEASURED_WORK_AREAS}
    stationary = [name.lower() for name, post in bot_accounts.MEASURED_WORK_POSTS.items()
                  if post is not None]
    result = docker(['exec', '-i', dash, 'python', '-c', observe], input_text=json.dumps({
        'observer': observer, 'ids': ids,
        'rooms': [['baristaia', 110], ['djia', 120], ['pizzeroia', 330]],
        'areas': areas, 'stationary': stationary}))
    payload = json.loads(result.stdout)
    points = payload['points']
    if len(points.get('baristaia', [])) < 1:
        raise RuntimeError('Barista did not walk inside the measured area')
    if points.get('djia') or points.get('pizzeroia'):
        raise RuntimeError('A stationary work bot moved on the room floor')
    print('PASS: Juanito walked; Cadence and Viterbos stayed put: '
          + ', '.join('%s %s' % (name, points.get(name, [])) for name in ROLES))
    docker(['restart', '-t', '8', service])
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        metrics = docker(['exec', dash, 'python', '-c',
                          'import urllib.request\n'
                          'print(urllib.request.urlopen("http://bots:9102/metrics",timeout=3).read().decode())'],
                         check=False)
        connected = [line for line in metrics.stdout.splitlines() if line.startswith('bots_connected{')]
        if len(connected) == 3 and all(line.rsplit(' ', 1)[-1] == '1' for line in connected):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError('Bots service did not reconnect after restart')
    if {row['username']: row['id'] for row in role_rows(postgres)} != ids:
        raise RuntimeError('Restarting the bots service created or replaced accounts')
    print('PASS: restarting the bots service reused the same three accounts.')


SHIM = r'''#!/usr/bin/env python3
"""Disposable-bank kubectl. Never contacts a real API server."""
import base64
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(os.environ['CP_BANK_STATE']).read_text())
args = sys.argv[1:]

def note(text):
    path = state.get('log_path')
    if not path:
        return
    safe = '\n'.join(line for line in text.splitlines() if 'password' not in line.lower())
    with open(path, 'a') as handle:
        handle.write(safe[-800:] + '\n')

def drop_flag(name, takes_value):
    global args
    kept = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == name:
            index += 2 if takes_value else 1
            continue
        if takes_value and token.startswith(name + '='):
            index += 1
            continue
        kept.append(token)
        index += 1
    args = kept

drop_flag('--kubeconfig', True)
drop_flag('--context', True)
drop_flag('--request-timeout', True)
drop_flag('-n', True)
drop_flag('--namespace', True)

def emit(document):
    json.dump(document, sys.stdout)
    sys.stdout.write('\n')

note('cmd ' + ' '.join(part for part in args[:5] if len(part) < 40))
if args[:2] == ['config', 'view']:
    emit({'current-context': 'default', 'clusters': [
        {'name': 'bank', 'cluster': {'server': 'https://127.0.0.1:16443'}}]})
elif args[:2] == ['get', 'nodes']:
    emit({'items': [{'metadata': {'name': state['node']}, 'status': {
        'nodeInfo': {'architecture': 'arm64'},
        'conditions': [{'type': 'Ready', 'status': 'True'}]}}]})
elif args[:3] == ['get', 'deployment', 'bots']:
    replicas = int(state['replicas'])
    emit({'spec': {'replicas': replicas}, 'status': {'replicas': replicas}})
elif args[:2] == ['get', 'pods']:
    emit({'items': [{'metadata': {'name': 'lingering-bot'}}] if state.get('pods') else []})
elif args[:3] == ['get', 'secret', 'bot-credentials']:
    if Path(state['marker_path']).exists():
        sys.stdout.write('secret/bot-credentials\n')
elif 'exec' in args and '--' in args:
    import subprocess
    tail = args[args.index('--') + 1:]
    target = state['postgres'] if 'psql' in tail else state['dash']
    proc = subprocess.run([state['docker'], 'exec', '-i', target, *tail],
                          input=sys.stdin.read(), text=True, capture_output=True)
    if proc.returncode:
        note('exec rc=%s target=%s\n%s\n%s' % (proc.returncode, target, proc.stdout[-400:], proc.stderr[-400:]))
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    raise SystemExit(proc.returncode)
elif args[:3] == ['create', 'secret', 'generic']:
    marker = '--from-file=config.json='
    source = next(token[len(marker):] for token in args if token.startswith(marker))
    path = Path(source).resolve()
    try:
        path.relative_to(state['allowed_prefix'])
    except ValueError:
        note('secret path refused')
        raise SystemExit(2)
    emit({'apiVersion': 'v1', 'kind': 'Secret',
          'metadata': {'name': 'bot-credentials', 'namespace': 'clubpenguin'},
          'data': {'config.json': base64.b64encode(path.read_bytes()).decode('ascii')}})
elif args[:1] == ['apply']:
    body = json.loads(sys.stdin.read())
    if body.get('kind') != 'Secret' or body.get('metadata', {}).get('name') != 'bot-credentials':
        note('apply refused kind=%s' % body.get('kind'))
        raise SystemExit(2)
    target = Path(state['secret_path'])
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(body, handle)
    Path(state['marker_path']).write_text('applied\n')
else:
    note('unsupported ' + ' '.join(part for part in args[:6] if len(part) < 40))
    sys.stderr.write('shim: unsupported kubectl invocation\n')
    raise SystemExit(2)
'''


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
            json.JSONDecodeError, AssertionError) as error:
        print('FAIL: ' + str(error)[-1500:], file=sys.stderr)
        raise SystemExit(1) from None

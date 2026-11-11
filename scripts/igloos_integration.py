"""Igloo acceptance on disposable Houdini; no cluster or paid provider access."""
import importlib.util
import json
import secrets
import time

import bot_accounts
from bots_image import verify_bots_image


def check_igloos(root, docker, postgres, network, label, tag, temp):
    image = 'clubpenguin/bots:' + tag
    verify_bots_image(root, docker, image)
    names = ('lolaritmo', 'titogroove', 'mambo', 'peperina', 'pacopregon',
             'baristaia', 'pizzeroia', 'djia')
    identities = [{'username': name, 'password': secrets.token_urlsafe(16)}
                  for name in (*names, 'igloohuman')]
    for work in bot_accounts.work_config_document()['bots']:
        identity = next(i for i in identities if i['username'] == work['username'])
        identity.update({k: v for k, v in work.items() if k not in ('username', 'password')})
    register = (root / 'scripts/bot_accounts.py').read_text() + '''
import json,sys
for bot in json.load(sys.stdin):
    register('http://dash:3000',bot['username'],bot['password'])
print('registered')
'''
    docker(['exec', '-i', network + '-dash', 'python', '-I', '-c', register],
           input=json.dumps(identities))
    spec = importlib.util.spec_from_file_location('provision', root / 'scripts/provision-igloos.py')
    provision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provision)
    sql = provision.provision_sql(names)

    def psql(statement):
        return docker(['exec', '-i', postgres, 'psql', '-U', 'houdini', '-d', 'houdini',
                       '-At', '-v', 'ON_ERROR_STOP=1'], input=statement).stdout

    snapshot = "SELECT id,igloo FROM penguin WHERE username IN (" + ','.join("'" + n + "'" for n in names) + ') ORDER BY id;'
    psql(sql)
    before = psql(snapshot)
    psql(sql)
    assert before == psql(snapshot), 'Provision is not idempotent'
    psql(provision.rollback_sql())
    assert all(line.endswith('|') for line in psql(snapshot).splitlines()), 'Rollback left an active igloo'
    psql(sql)
    print('PASS: disposable igloo SQL applied twice, rolled back, and reprovisioned.')
    path = temp / 'igloos.json'
    path.write_text(json.dumps(identities))
    path.chmod(0o444)
    service = network + '-igloos'
    command = ['run', '-d', '--pull=never', '--name', service, '--label', label,
               '--network', network, '--read-only', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--entrypoint', 'python',
               '-v', f'{root / "scripts/igloos_probe.py"}:/probe.py:ro',
               '-v', f'{path}:/identities.json:ro',
               '-v', f'{root / "containers/bots/cast"}:/cast:ro', image, '/probe.py']
    docker(command)
    def read_logs():
        result = docker(['logs', service])
        return result.stdout + result.stderr

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        logs = read_logs()
        if 'READY_RESTART' in logs:
            break
        if docker(['inspect', '-f', '{{.State.Running}}', service]).stdout.strip() != 'true':
            raise RuntimeError('Igloo probe failed: ' + logs[-4000:])
        time.sleep(.5)
    else:
        raise RuntimeError('Igloo probe did not reach the mid-lease restart')
    print(logs.strip())
    docker(['kill', service])  # abrupt death, not graceful Travel.close()
    docker(['rm', service])
    docker(command + ['--restart'])
    result = docker(['wait', service], timeout=120).stdout.strip()
    logs = read_logs()
    if result != '0':
        raise RuntimeError('Igloo restart probe failed: ' + logs[-4000:])
    print(logs.strip())

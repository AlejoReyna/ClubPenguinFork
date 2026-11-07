#!/usr/bin/env python3
"""Deployment steps for the Raspberry Pi, run by the trusted Mac agent.

  preflight   Azure identity, tunnel, dedicated kubeconfig target and Ansible preflight
  adopt       import the manually created Cloudflare connector into the state (dry run first)
  plan        saved Terraform plan for infra/cluster + policy gate (no apply)
  apply       backup if required, then apply exactly the saved plan
  verify      rollout, images, HTTP, WebSocket, game journey, Prometheus, public edge
  record      store the release manifest in Blob after a verified deployment
  evidence    copy non-secret evidence to a folder, refusing if it looks sensitive
  cleanup     close a tunnel this run opened and delete this run's private workspace

Only `infra/cluster` is managed here. `infra/cloud` (ACR token, backup container) needs
broader Azure rights and is applied by an operator, never by the release identity.
Every command is meant to run inside an AzureCLI@2 task when it talks to Azure, so the
federated login is isolated from the operator's own `az` session.

Nothing here prints credentials, plans, state, Secret contents or the environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402
import plan_gate  # noqa: E402
import release  # noqa: E402
from lablib import ROOT, fail, run  # noqa: E402

CLUSTER = ROOT / 'infra/cluster'
NAMESPACE = 'clubpenguin'
STATIC_VARS = ['-var=game_enabled=true', '-var=web_enabled=true',
               # Explicit on purpose: a clean checkout must not retire the (adopted) tunnel connector.
               '-var=cloudflared_enabled=true']
ROLLOUTS = [('statefulset', 'postgres'), ('deployment', 'redis'), ('deployment', 'login'), ('deployment', 'glaciar'),
            ('deployment', 'dash'), ('deployment', 'exporter'), ('deployment', 'web')]


# --- helpers -----------------------------------------------------------------

def workspace(create=True):
    return lablib.RunDir(run_id=os.environ.get('PLAN_RUN_ID') or None, create=create)


def kubectl(cfg, *args, timeout=60, check=True):
    return run(['kubectl', '--kubeconfig', cfg['CP_KUBECONFIG'], '--context', 'default'] + list(args),
               timeout=timeout, check=check, quiet=True)


def azure_identity(cfg):
    result = run(['az', 'account', 'show', '--query', '{id:id,tenant:tenantId}', '-o', 'json'], quiet=True)
    info = json.loads(result.stdout)
    wanted = cfg.get('CP_AZURE_SUBSCRIPTION_ID')
    if wanted and wanted.lower() != info['id'].lower():
        fail('The Azure login is not the expected subscription (CP_AZURE_SUBSCRIPTION_ID)')
    return info


def terraform_env(cfg, ws):
    info = azure_identity(cfg)
    cache = Path.home() / '.clubpenguin-lab/plugin-cache'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {'TF_DATA_DIR': str(ws.sub('tfdata')), 'TF_IN_AUTOMATION': '1', 'TF_INPUT': '0',
            'TF_PLUGIN_CACHE_DIR': str(cache), 'ARM_SUBSCRIPTION_ID': info['id'], 'ARM_TENANT_ID': info['tenant'],
            'ARM_USE_AZUREAD': 'true'}


def terraform(env, *args, check=True, timeout=1800):
    return run(['terraform', f'-chdir={CLUSTER}'] + list(args), env=env, check=check, timeout=timeout, quiet=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def terraform_version(env):
    return json.loads(run(['terraform', 'version', '-json'], env=env, quiet=True).stdout)['terraform_version']


def evidence(ws, name, data):
    lablib.write_json(ws.sub('evidence') / name, data)


def tunnel(ws, action):
    return run([sys.executable, str(ROOT / 'scripts/tunnel.py'), action, '--state-file', str(ws.path / 'tunnel.state')],
               check=(action != 'stop'), timeout=120, quiet=True)


# --- preflight -----------------------------------------------------------------

def cmd_preflight(args):
    cfg = lablib.load_config('plan')
    ws = workspace()
    print('Pruned stale run directories:', ws.prune_stale() or 'none')
    azure_identity(cfg)
    print('Azure identity confirmed (subscription id not printed).')
    print(tunnel(ws, 'start').stdout.strip())
    verified = run([sys.executable, str(ROOT / 'scripts/tunnel.py'), 'verify', '--kubeconfig', cfg['CP_KUBECONFIG']], quiet=True)
    print(verified.stdout.strip())
    command = ['ansible-playbook', 'preflight.yml',
               '-e', f'ansible_host={cfg["CP_PI_HOST"]}', '-e', f'ansible_user={cfg["CP_PI_USER"]}']
    if cfg['CP_SSH_CONFIG']:
        command += ['-e', f'ansible_ssh_common_args=-F {cfg["CP_SSH_CONFIG"]}']
    result = run(command, timeout=300, quiet=True, cwd=ROOT / 'ansible',
                 env={'ANSIBLE_STDOUT_CALLBACK': 'default', 'ANSIBLE_NOCOLOR': '1'})
    recap = [line for line in result.stdout.splitlines() if 'ok=' in line]
    print('Ansible preflight passed:', ' '.join(recap[-1].split()[1:]) if recap else 'ok')
    evidence(ws, 'preflight.json', {'azure_subscription_matches': True, 'node_verified': True,
                                    'ansible_preflight': 'passed', 'created_utc': time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())})


# --- plan ----------------------------------------------------------------------

def live_objects(cfg):
    """What exists in the cluster that Terraform might try to create (names only)."""
    live = {}
    for kind, name in (('deployment', 'cloudflared'), ('networkpolicy', 'web-from-cloudflared')):
        found = kubectl(cfg, '-n', NAMESPACE, 'get', kind, name, '-o', 'name', check=False)
        live[f'{kind}/{name}'] = found.returncode == 0
    secret = kubectl(cfg, '-n', NAMESPACE, 'get', 'secret', 'cloudflared-token', '-o', 'name', check=False)
    live['secret/cloudflared-token'] = secret.returncode == 0 and bool(secret.stdout.strip())
    return live


def puffle_settings(manifest, cfg):
    supported = manifest.get('features', {}).get('puffle') is True
    return {'puffle_enabled': supported and cfg['CP_PUFFLE_ENABLED'] == 'true',
            'puffle_broker_replicas': int(cfg['CP_PUFFLE_BROKER_REPLICAS']) if supported else 0}


def cmd_plan(args):
    cfg = lablib.load_config('plan')
    ws = workspace()
    env = terraform_env(cfg, ws)
    manifest = json.loads(Path(args.manifest).read_text())
    values = release.manifest_to_tfvars(manifest, cfg['CP_PUBLIC_HOSTNAME'])
    values.update({
        **puffle_settings(manifest, cfg),
        'puffle_allowed_players': [int(x) for x in cfg['CP_PUFFLE_ALLOWED_PLAYERS'].split(',') if x],
        'proxy_extra_hosts': [x for x in cfg['CP_PROXY_EXTRA_HOSTS'].split(',') if x],
        'public_hostname': cfg['CP_PUBLIC_HOSTNAME'], 'kubeconfig_path': cfg['CP_KUBECONFIG'], 'lan_cidr': cfg['CP_LAN_CIDR'],
        'cloud_state': {'resource_group_name': cfg['CP_STATE_RESOURCE_GROUP'],
                        'storage_account_name': cfg['CP_STATE_STORAGE_ACCOUNT'],
                        'container_name': cfg['CP_STATE_CONTAINER'], 'key': cfg['CP_CLOUD_STATE_KEY']},
    })
    lablib.write_private(ws.sub('config') / 'backend.hcl', lablib.render_hcl(lablib.backend_config(cfg, 'cluster')))
    lablib.write_private(ws.sub('config') / 'release.tfvars.json', json.dumps(values))
    print(tunnel(ws, 'start').stdout.strip())
    ok = run([sys.executable, str(ROOT / 'scripts/tunnel.py'), 'verify', '--kubeconfig', cfg['CP_KUBECONFIG']], quiet=True)
    print(ok.stdout.strip())

    terraform(env, 'init', '-input=false', '-reconfigure', '-lockfile=readonly', '-no-color',
              f'-backend-config={ws.sub("config") / "backend.hcl"}')
    live = live_objects(cfg)
    if not live['secret/cloudflared-token'] and not live['deployment/cloudflared']:
        print('NOTE: secret cloudflared-token is missing; the connector cannot start without it.')
    plan_path = ws.sub('plan') / 'release.tfplan'
    plan_path.unlink(missing_ok=True)
    old_umask = os.umask(0o077)
    try:
        result = terraform(env, 'plan', '-input=false', '-lock-timeout=10m', '-detailed-exitcode', '-no-color',
                           f'-out={plan_path}', *STATIC_VARS, f'-var-file={ws.sub("config") / "release.tfvars.json"}',
                           check=False)
    finally:
        os.umask(old_umask)
    if result.returncode not in (0, 2):
        fail('terraform plan failed:\n' + lablib.redact(result.stderr[-1500:]))
    plan_path.chmod(0o600)
    shown = terraform(env, 'show', '-json', '-no-color', str(plan_path))
    changes = plan_gate.summarize(json.loads(shown.stdout))  # kept in memory; never written or printed
    verdict = plan_gate.classify(changes, live, [a for a in (args.allow_destructive or '').split(',') if a], args.expect)
    print(plan_gate.render(changes, verdict))
    digest = sha256_file(plan_path)
    summary = {'changes': changes, 'verdict': verdict, 'has_changes': bool(changes), 'plan_sha256': digest,
               'terraform_version': terraform_version(env), 'live_checks': {k: bool(v) for k, v in live.items()},
               'manifest_commit': manifest['commit']}
    evidence(ws, 'plan-summary.json', summary)
    lablib.write_private(ws.sub('evidence') / 'plan-summary.md',
                         '# Terraform plan (infra/cluster)\n\n```\n' + plan_gate.render(changes, verdict) + '\n```\n')
    lablib.write_private(ws.path / 'allow-destructive', args.allow_destructive or '')
    lablib.set_pipeline_variable('planSha256', digest)
    lablib.set_pipeline_variable('planHasChanges', str(bool(changes)).lower())
    lablib.set_pipeline_variable('backupRequired', str(verdict['backup_required']).lower())
    if verdict['blocked']:
        lablib.log_issue('error', 'Plan blocked by policy: ' + '; '.join(verdict['blocked'])[:600])
        raise SystemExit(2)
    print(f'Saved plan {digest[:16]}… is private to this run ({ws.name}) and is not published.')


# --- adopt the manually created connector into the state --------------------------

ADOPTABLE = [('kubernetes_deployment_v1.cloudflared[0]', 'deployment', 'cloudflared', 'clubpenguin/cloudflared'),
             ('kubernetes_network_policy_v1.web_from_cloudflared[0]', 'networkpolicy', 'web-from-cloudflared',
              'clubpenguin/web-from-cloudflared')]


def cmd_adopt(args):
    """Import the hand-made Cloudflare connector and its network policy into the state.

    Declaring them in HCL does not import them. Dry-run by default; with --execute it
    saves a private copy of the state first, then runs `terraform import` (state-only:
    the live objects are neither modified nor deleted). Run a plan afterwards and review
    it: expect at most an image/argument alignment of the connector.
    """
    cfg = lablib.load_config('plan')
    ws = workspace()
    env = terraform_env(cfg, ws)
    lablib.write_private(ws.sub('config') / 'backend.hcl', lablib.render_hcl(lablib.backend_config(cfg, 'cluster')))
    base_vars = {'public_hostname': cfg['CP_PUBLIC_HOSTNAME'], 'kubeconfig_path': cfg['CP_KUBECONFIG'],
                 'lan_cidr': cfg['CP_LAN_CIDR'],
                 'cloud_state': {'resource_group_name': cfg['CP_STATE_RESOURCE_GROUP'],
                                 'storage_account_name': cfg['CP_STATE_STORAGE_ACCOUNT'],
                                 'container_name': cfg['CP_STATE_CONTAINER'], 'key': cfg['CP_CLOUD_STATE_KEY']}}
    lablib.write_private(ws.sub('config') / 'import.tfvars.json', json.dumps(base_vars))
    print(tunnel(ws, 'start').stdout.strip())
    terraform(env, 'init', '-input=false', '-reconfigure', '-lockfile=readonly', '-no-color',
              f'-backend-config={ws.sub("config") / "backend.hcl"}')
    in_state = set(terraform(env, 'state', 'list', '-no-color').stdout.split())
    live = live_objects(cfg)
    todo = []
    for address, kind, name, ident in ADOPTABLE:
        if address in in_state:
            print(f'already in state: {address}')
        elif not live.get(f'{kind}/{name}'):
            print(f'not found in the cluster, nothing to adopt: {kind}/{name}')
        else:
            todo.append((address, ident))
    flags = ['-input=false', '-no-color', '-lock-timeout=10m', *STATIC_VARS,
             f'-var-file={ws.sub("config") / "import.tfvars.json"}']
    for address, ident in todo:
        print(f'{"IMPORT" if args.execute else "would import"}: terraform import {address} {ident}')
    if not todo:
        print('Nothing to adopt.')
        return
    if not args.execute:
        print('Dry run. Re-run with --execute to save the state and import (state change only).')
        return
    backup = ws.path / ('state-before-adopt-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '.json')
    lablib.write_private(backup, terraform(env, 'state', 'pull', '-no-color').stdout)
    print(f'Private state copy: {backup} (contains secrets; keep it out of Git and artifacts, delete after review)')
    for address, ident in todo:
        terraform(env, 'import', *flags, address, ident)
        print(f'imported {address}')
    evidence(ws, 'adopt.json', {'imported': [a for a, _ in todo], 'created_utc': time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())})
    print('Now run a plan and review it before any apply (see docs/operations.md).')


# --- apply ---------------------------------------------------------------------

def cmd_apply(args):
    cfg = lablib.load_config('plan')
    ws = workspace(create=False)
    plan_path = ws.sub('plan') / 'release.tfplan'
    if not plan_path.is_file() or not ws.sub('tfdata').is_dir():
        fail('No saved plan for this run on this agent. Plan and apply must run on the same agent '
             '(pin the pool and agent name) and the plan must not have been cleaned up.')
    if sha256_file(plan_path) != args.expect_sha256:
        fail('The saved plan differs from the one that was reviewed; refusing to apply.')
    env = terraform_env(cfg, ws)
    print(tunnel(ws, 'start').stdout.strip())
    # Re-evaluate the policy on the stored plan: the decision must not depend on the earlier job alone.
    shown = terraform(env, 'show', '-json', '-no-color', str(plan_path))
    allow = (ws.path / 'allow-destructive').read_text() if (ws.path / 'allow-destructive').exists() else ''
    changes = plan_gate.summarize(json.loads(shown.stdout))
    verdict = plan_gate.classify(changes, live_objects(cfg), [a for a in allow.split(',') if a], args.expect)
    if verdict['blocked']:
        fail('Plan blocked by policy at apply time: ' + '; '.join(verdict['blocked']))

    mode = args.backup
    if verdict['backup_required'] and mode == 'never':
        fail('This plan touches data-bearing resources; --backup never is not allowed for it.')
    if mode == 'always' or (mode == 'auto' and verdict['backup_required']):
        reason = f'pre-deploy {ws.name}'
        print('Taking a verified backup BEFORE applying (' + ', '.join(verdict['backup_reasons'] or ['requested']) + ').')
        run([sys.executable, str(ROOT / 'scripts/backup.py'), '--reason', reason,
             '--json-out', str(ws.sub('evidence') / 'backup-pre-deploy.json')], timeout=1800, quiet=True)
        print('Backup verified (restored into a throwaway database and read back).')
        run([sys.executable, str(ROOT / 'scripts/backup-operator-state.py')], timeout=900, quiet=True)
        print('External configuration and bot ledger backed up privately.')
    else:
        print('Backup before apply: not required for this plan.')

    started = time.time()
    result = terraform(env, 'apply', '-input=false', '-lock-timeout=10m', '-no-color', str(plan_path), check=False, timeout=3600)
    outcome = {'exit_code': result.returncode, 'seconds': round(time.time() - started),
               'applied_plan_sha256': args.expect_sha256, 'created_utc': time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}
    evidence(ws, 'apply.json', outcome)
    if result.returncode != 0:
        print(lablib.redact(result.stderr[-1500:]))
        lablib.log_issue('error', 'terraform apply failed; see docs/operations.md (Rollback)')
        raise SystemExit(1)
    print(f'Applied the reviewed plan in {outcome["seconds"]}s.')


# --- verify --------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.items = []

    def run(self, name, function, blocking=True):
        started = time.time()
        try:
            detail = function() or 'ok'
            status = 'pass'
        except (SystemExit, Exception) as exc:  # noqa: BLE001 - every failure becomes a recorded result
            detail = lablib.redact(str(exc) or type(exc).__name__)[:300]
            status = 'fail' if blocking else 'warn'
        self.items.append({'name': name, 'status': status, 'detail': detail, 'seconds': round(time.time() - started, 1)})
        print(f'[{status.upper():4}] {name}: {detail}')

    @property
    def failed(self):
        return [i for i in self.items if i['status'] == 'fail']


def expected_images(manifest):
    comps = manifest['components']
    expected = {
        'postgres': {'postgres': comps['database']}, 'login': {'login': comps['runtime']},
        'glaciar': {'glaciar': comps['runtime']}, 'dash': {'dash': comps['runtime']},
        'exporter': {'exporter': comps['runtime']},
        'web': {'web': comps['web'], 'socket-proxy': comps['runtime']},
    }
    if 'bots' in comps:
        expected['bots'] = {'bots': comps['bots']}
    return expected


def check_images(cfg, manifest):
    pods = json.loads(kubectl(cfg, '-n', NAMESPACE, 'get', 'pods', '-o', 'json').stdout)['items']
    expected = expected_images(manifest)
    if any(p['metadata'].get('labels', {}).get('app') == 'puffle' for p in pods):
        expected['puffle'] = {'puffle': manifest['components']['runtime']}
    seen = set()
    for pod in pods:
        app = pod['metadata'].get('labels', {}).get('app')
        if app not in expected:
            continue
        for status in pod['status'].get('containerStatuses', []):
            want = expected[app].get(status['name'])
            if not want:
                continue
            tag = status['image'].rsplit(':', 1)[-1]
            digest = status.get('imageID', '').split('@')[-1]
            if tag != want['tag']:
                raise RuntimeError(f'{app}/{status["name"]} runs tag {tag}, expected {want["tag"]}')
            if want.get('digest') and digest != want['digest']:
                raise RuntimeError(f'{app}/{status["name"]} runs an image whose digest differs from the release manifest')
            if not status.get('ready'):
                raise RuntimeError(f'{app}/{status["name"]} is not ready')
            seen.add((app, status['name']))
    missing = {(a, c) for a, cs in expected.items() for c in cs} - seen
    if missing:
        raise RuntimeError('containers not found: ' + ', '.join(f'{a}/{c}' for a, c in sorted(missing)))
    return f'{len(seen)} containers match the manifest tags and digests'


def exec_in_dash(cfg, script, *argv, timeout=180):
    return run(['kubectl', '--kubeconfig', cfg['CP_KUBECONFIG'], '--context', 'default', '-n', NAMESPACE, 'exec', '-i',
                'deploy/dash', '--', 'python', '-'] + list(argv), input_text=script, timeout=timeout, quiet=True)


def prometheus_query(cfg, query):
    path = '/api/v1/namespaces/monitoring-lab/services/prometheus:80/proxy/api/v1/query'
    out = kubectl(cfg, 'get', '--raw', path + '?query=' + re.sub(r'([^A-Za-z0-9_])', lambda m: '%%%02X' % ord(m.group(1)), query),
                  timeout=30)
    return json.loads(out.stdout)['data']['result']


def cmd_verify(args):
    cfg = lablib.load_config('plan')
    ws = workspace(create=False)
    manifest = json.loads(Path(args.manifest).read_text())
    print(tunnel(ws, 'start').stdout.strip())
    checks = Checks()
    host = cfg['CP_PUBLIC_HOSTNAME']

    def rollout():
        targets = list(ROLLOUTS)
        if kubectl(cfg, '-n', NAMESPACE, 'get', 'deployment', 'puffle', '-o', 'name', check=False).returncode == 0:
            targets.append(('deployment', 'puffle'))
        if 'bots' in manifest['components']:
            targets.append(('deployment', 'bots'))
        if kubectl(cfg, '-n', NAMESPACE, 'get', 'deployment', 'cloudflared', '-o', 'name', check=False).returncode == 0:
            targets.append(('deployment', 'cloudflared'))
        for kind, name in targets:
            kubectl(cfg, '-n', NAMESPACE, 'rollout', 'status', f'{kind}/{name}', '--timeout=300s', timeout=330)
        return f'{len(targets)} workloads rolled out'

    def pods_ready():
        kubectl(cfg, '-n', NAMESPACE, 'wait', '--for=condition=Ready', 'pod', '--all', '--timeout=120s', timeout=150)
        restarts = {}
        for pod in json.loads(kubectl(cfg, '-n', NAMESPACE, 'get', 'pods', '-o', 'json').stdout)['items']:
            restarts[pod['metadata']['name']] = sum(c.get('restartCount', 0) for c in pod['status'].get('containerStatuses', []))
        evidence(ws, 'pod-restarts.json', restarts)
        return f'all pods Ready; restart counts recorded for {len(restarts)} pods'

    def http():
        script = ('import sys,urllib.request\nfor path,needle in (("/healthz","ok"),("/","<"),("/servers.xml","id=\\"3101\\"")):\n'
                  ' r=urllib.request.urlopen("http://web:8080"+path,timeout=10)\n assert r.status==200,path\n'
                  ' assert needle in r.read().decode(),path\nprint("ok")\n')
        exec_in_dash(cfg, script)
        return 'web answers /healthz, / and /servers.xml with the expected content'

    def websocket():
        probe = (ROOT / 'scripts/ws_probe.py').read_text()
        exec_in_dash(cfg, probe, '--url', 'ws://web:8080/socket/login', '--origin', f'https://{host}')
        exec_in_dash(cfg, probe, '--url', 'ws://web:8080/socket/login', '--origin', 'https://not-allowed.invalid', '--expect-reject')
        return 'proxy accepts the public origin and refuses an unknown one'

    def puffle():
        exists = kubectl(cfg, '-n', NAMESPACE, 'get', 'deployment', 'puffle', '-o', 'name', check=False)
        if exists.returncode != 0:
            raise RuntimeError('Puffle deployment is missing')
        script = ('import json,urllib.request,hashlib\n'
                  'base="http://web:8080"\n'
                  'data=json.load(urllib.request.urlopen(base+"/puffle/build.json",timeout=10))\n'
                  'for path,key in (("/media/play/v2/client/engine.swf","engine.swf"),("/media/play/v2/client/login.swf","login.swf"),\n'
                  '                 ("/puffle/companion.zip","companion.zip")):\n'
                  ' raw=urllib.request.urlopen(base+path,timeout=10).read()\n'
                  ' assert hashlib.sha256(raw).hexdigest()==data[key],"puffle artifact hash mismatch"\n'
                  'print("puffle artifacts match")\n')
        exec_in_dash(cfg, script)
        health = ('import json,urllib.request; '
                  'd=json.load(urllib.request.urlopen("http://127.0.0.1:9103/healthz",timeout=10)); '
                  f'assert d["ok"] is True and d["enabled"] is {(cfg["CP_PUFFLE_ENABLED"] == "true")!r}')
        kubectl(cfg, '-n', NAMESPACE, 'exec', 'deploy/puffle', '--', 'python', '-c', health)
        return 'puffle artifact hashes and active capability match'

    def journey():
        out = exec_in_dash(cfg, (ROOT / 'scripts/game-smoke.py').read_text(), '--dash-url', 'http://web:8080', timeout=240)
        return out.stdout.strip().splitlines()[-1]

    def prometheus():
        deadline = time.time() + 180
        while True:
            result = prometheus_query(cfg, 'clubpenguin_redis_up')
            if result and all(float(r['value'][1]) == 1.0 for r in result):
                break
            if time.time() > deadline:
                raise RuntimeError('clubpenguin_redis_up is not 1 after 180s')
            time.sleep(15)
        firing = prometheus_query(cfg, 'ALERTS{alertstate="firing"}')
        names = sorted({r['metric'].get('alertname', '?') for r in firing})
        if names:
            raise RuntimeError('alerts firing: ' + ', '.join(names))
        return 'redis_up=1 and no alert firing'

    def public_edge():
        import urllib.request
        request = urllib.request.Request(f'https://{host}/healthz', headers={'User-Agent': 'clubpenguin-lab-probe/1'})
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f'HTTP {response.status}')
        run([sys.executable, str(ROOT / 'scripts/ws_probe.py'), '--url', f'wss://{host}/socket/login',
             '--origin', f'https://{host}'], timeout=40, quiet=True)
        return 'public /healthz is 200 and the public WebSocket reaches the login server'

    checks.run('rollout', rollout)
    checks.run('pods-ready', pods_ready)
    checks.run('image-tags-and-digests', lambda: check_images(cfg, manifest))
    checks.run('http-through-web', http)
    checks.run('websocket-proxy', websocket)
    if manifest.get('features', {}).get('puffle') is True:
        checks.run('puffle-artifacts-and-service', puffle)
    if not args.skip_game_journey:
        checks.run('game-journey', journey)
    checks.run('prometheus-signals', prometheus, blocking=args.require_prometheus)
    if not args.skip_public:
        checks.run('public-edge', public_edge, blocking=args.require_public)
    evidence(ws, 'verify.json', {'checks': checks.items, 'created_utc': time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())})
    if checks.failed:
        lablib.log_issue('error', 'Verification failed: ' + ', '.join(c['name'] for c in checks.failed))
        collect_diagnostics(cfg, ws)
        raise SystemExit(1)
    warns = [c['name'] for c in checks.items if c['status'] == 'warn']
    if warns:
        lablib.log_issue('warning', 'Non-blocking checks failed: ' + ', '.join(warns))


def collect_diagnostics(cfg, ws):
    """Pod status and events only: no logs and no Secret data."""
    try:
        pods = kubectl(cfg, '-n', NAMESPACE, 'get', 'pods', '-o', 'wide', check=False).stdout
        events = kubectl(cfg, '-n', NAMESPACE, 'get', 'events', '--sort-by=.lastTimestamp', check=False).stdout
        lablib.write_private(ws.sub('evidence') / 'diagnostics.txt',
                             lablib.redact(pods + '\n' + '\n'.join(events.splitlines()[-40:])) + '\n')
    except SystemExit:
        pass


# --- record / evidence / cleanup ----------------------------------------------

def cmd_record(args):
    cfg = lablib.load_config('backup')
    data = json.loads(Path(args.manifest).read_text())
    data['recorded_utc'] = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    data['kind'] = 'rollback' if args.rollback else 'release'
    data['run'] = dict(data.get('run') or {}, recorded_by_build=os.environ.get('BUILD_BUILDID', 'local'))
    stamped = Path(args.manifest).with_name('recorded-' + Path(args.manifest).name)
    lablib.write_json(stamped, data)
    name = release.upload_manifest(stamped, cfg)
    print(f'Release manifest stored as {cfg["CP_BACKUP_CONTAINER"]}/{name}')


def cmd_evidence(args):
    ws = workspace(create=False)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems = []
    for source in sorted(ws.sub('evidence').glob('*')) + [Path(p) for p in args.extra]:
        if not source.is_file() or source.suffix not in ('.json', '.txt', '.md'):
            continue
        text = source.read_text()
        if any(pattern.search(text) for pattern in lablib.SECRET_PATTERNS[:1] + lablib.SECRET_PATTERNS[2:]):
            problems.append(source.name)
            continue
        shutil.copyfile(source, out / source.name)
    if problems:
        shutil.rmtree(out, ignore_errors=True)
        fail('Evidence looks sensitive and was not published: ' + ', '.join(problems))
    print('Evidence files:', ', '.join(sorted(p.name for p in out.iterdir())) or 'none')


def cmd_cleanup(args):
    ws = workspace(create=False)
    if ws.path.exists():
        print(tunnel(ws, 'stop').stdout.strip())
    if not args.keep_workspace and ws.remove():
        print(f'Removed private workspace {ws.name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('preflight').set_defaults(func=cmd_preflight)
    p = sub.add_parser('adopt', help='import the manually created tunnel connector into the state (dry run by default)')
    p.add_argument('--execute', action='store_true')
    p.set_defaults(func=cmd_adopt)
    p = sub.add_parser('plan')
    p.add_argument('--manifest', required=True, help='release manifest with the image tags to deploy')
    p.add_argument('--expect', choices=['images-only'], help='fail unless the plan only changes images (rollback)')
    p.add_argument('--allow-destructive', default='', help='exact addresses allowed to be deleted/replaced')
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser('apply')
    p.add_argument('--expect-sha256', required=True, help='hash of the reviewed plan')
    p.add_argument('--backup', choices=['auto', 'always', 'never'], default='auto')
    p.add_argument('--expect', choices=['images-only'])
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser('verify')
    p.add_argument('--manifest', required=True)
    p.add_argument('--skip-public', action='store_true')
    p.add_argument('--skip-game-journey', action='store_true', help='do not register disposable accounts in the lab')
    p.add_argument('--require-public', action='store_true')
    p.add_argument('--require-prometheus', action='store_true')
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser('record')
    p.add_argument('--manifest', required=True)
    p.add_argument('--rollback', action='store_true', help='mark the entry as a rollback to an older release')
    p.set_defaults(func=cmd_record)
    p = sub.add_parser('evidence')
    p.add_argument('--out', required=True)
    p.add_argument('--extra', nargs='*', default=[])
    p.set_defaults(func=cmd_evidence)
    p = sub.add_parser('cleanup')
    p.add_argument('--keep-workspace', action='store_true', help='close the tunnel only')
    p.set_defaults(func=cmd_cleanup)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

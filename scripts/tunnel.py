#!/usr/bin/env python3
"""Manage the lab's private SSH tunnel to the Kubernetes API.

  start    open the tunnel, or reuse one that already reaches the Pi
  verify   prove the dedicated kubeconfig points at the Pi (not AKS) and the node is ARM64
  status   report whether this lab's tunnel (control socket) is active
  stop     close the tunnel

Sharing with other sessions: `start --state-file F` records whether *this* call
opened the tunnel; `stop --state-file F` closes it only in that case, so a
pipeline never tears down a tunnel an operator is using. Without --state-file,
`stop` keeps its original behaviour.

Host and user come from --host/--user or private configuration (CP_PI_HOST,
CP_PI_USER); the committed repository holds only anonymized examples.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402

LOCAL_PORT = 16443
EXPECTED_SERVER = f'https://127.0.0.1:{LOCAL_PORT}'
root = Path(__file__).resolve().parents[1]
private = root / '.local'


def ssh_base(control, ssh_config):
    command = ['ssh']
    if ssh_config:
        command += ['-F', ssh_config]
    return command + ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                      '-o', 'ControlPath=' + control]


def kubectl(kubeconfig, *args, timeout=8):
    return subprocess.run(['kubectl', '--kubeconfig', kubeconfig, '--context', 'default',
                           f'--request-timeout={timeout}s'] + list(args), capture_output=True, text=True)


def verify_target(kubeconfig, node_name):
    """Return (ok, message). Never prints credentials; checks destination identity."""
    if not Path(kubeconfig).is_file():
        return False, f'kubeconfig not found: {kubeconfig}'
    mode = Path(kubeconfig).stat().st_mode & 0o077
    if mode:
        return False, f'{kubeconfig} must not be readable by group/others (chmod 600)'
    server = kubectl(kubeconfig, 'config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}')
    if server.returncode != 0 or server.stdout.strip() != EXPECTED_SERVER:
        return False, f'kubeconfig context "default" must point at {EXPECTED_SERVER}'
    nodes = kubectl(kubeconfig, 'get', 'nodes', '-o', 'json')
    if nodes.returncode != 0:
        return False, 'the Kubernetes API is not reachable through the tunnel'
    items = json.loads(nodes.stdout)['items']
    if len(items) != 1 or items[0]['metadata']['name'] != node_name:
        return False, f'expected exactly one node named {node_name}'
    node = items[0]
    labels = node['metadata'].get('labels', {})
    if any(key.startswith('kubernetes.azure.com') for key in labels):
        return False, 'the API belongs to an AKS cluster, not the Raspberry Pi'
    if node['status']['nodeInfo']['architecture'] != 'arm64':
        return False, 'the node is not ARM64'
    ready = [c for c in node['status']['conditions'] if c['type'] == 'Ready']
    if not ready or ready[0]['status'] != 'True':
        return False, 'the node is not Ready'
    return True, f'node {node_name} is Ready and ARM64 at {EXPECTED_SERVER}'


def record(state_file, started):
    """Remember whether this run opened the tunnel; a later `start` must not erase that."""
    if not state_file:
        return
    try:
        if json.loads(Path(state_file).read_text()).get('started') is True:
            started = True
    except (OSError, ValueError):
        pass
    lablib.write_private(state_file, json.dumps({'started': started}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['start', 'stop', 'status', 'verify'])
    parser.add_argument('--host')
    parser.add_argument('--user')
    parser.add_argument('--ssh-config', help='ssh_config to use instead of ~/.ssh/config')
    parser.add_argument('--kubeconfig')
    parser.add_argument('--state-file', help='records/consumes whether this call opened the tunnel')
    args = parser.parse_args()

    needs_host = args.action in ('start', 'stop', 'status')
    cfg = lablib.load_config('agent' if needs_host and not (args.host and args.user) else None)
    host = args.host or cfg['CP_PI_HOST']
    user = args.user or cfg['CP_PI_USER']
    ssh_config = args.ssh_config or cfg['CP_SSH_CONFIG'] or ''
    kubeconfig = args.kubeconfig or cfg['CP_KUBECONFIG']

    if args.action == 'verify':
        ok, message = verify_target(kubeconfig, cfg['CP_NODE_NAME'])
        print(('OK: ' if ok else 'ERROR: ') + message)
        raise SystemExit(0 if ok else 1)

    private.mkdir(mode=0o700, exist_ok=True)
    control = str(private / 'pi-ssh.sock')
    destination = f'{user}@{host}'
    base = ssh_base(control, ssh_config)
    check = subprocess.run(base + ['-O', 'check', destination], capture_output=True)

    if args.action == 'status':
        print('Tunnel active' if check.returncode == 0 else 'Tunnel inactive')
        raise SystemExit(check.returncode)

    if args.action == 'stop':
        owned = True
        if args.state_file:
            try:
                owned = json.loads(Path(args.state_file).read_text()).get('started') is True
            except (OSError, ValueError):
                owned = False
        if not owned:
            print('Leaving the tunnel open: this run did not start it')
        elif check.returncode == 0:
            subprocess.run(base + ['-O', 'exit', destination], check=True)
            print('Tunnel closed')
        if args.state_file:
            Path(args.state_file).unlink(missing_ok=True)
        return

    # start
    if check.returncode == 0:
        record(args.state_file, False)
        print('Tunnel already active')
        return
    # A trusted Mac agent can reuse an already-running, authenticated tunnel. The
    # dedicated kubeconfig verifies the Pi's TLS certificate and node identity.
    ok, _ = verify_target(kubeconfig, cfg['CP_NODE_NAME'])
    if ok:
        record(args.state_file, False)
        print('Existing authenticated connection to the Pi is available')
        return
    result = subprocess.run(base + ['-fNT', '-o', 'ControlMaster=yes', '-o', 'ExitOnForwardFailure=yes',
                                    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
                                    '-L', f'127.0.0.1:{LOCAL_PORT}:127.0.0.1:6443', destination],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print('ERROR: could not open the SSH tunnel: ' + lablib.redact(result.stderr.strip()[-400:]), file=sys.stderr)
        raise SystemExit(1)
    record(args.state_file, True)
    print(f'Private Kubernetes API tunnel: {EXPECTED_SERVER}')


if __name__ == '__main__':
    main()

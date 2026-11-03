#!/usr/bin/env python3
"""Manage the lab's private SSH tunnel to the Kubernetes API."""
import argparse
from pathlib import Path
import subprocess
import json

root = Path(__file__).resolve().parents[1]
private = root / '.local'
private.mkdir(mode=0o700, exist_ok=True)
control = str(private / 'pi-ssh.sock')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('action', choices=['start', 'stop', 'status'])
parser.add_argument('--host', default='192.168.0.10')
parser.add_argument('--user', default='pi')
args = parser.parse_args()
destination = args.user + '@' + args.host
base = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ControlPath=' + control]
check = subprocess.run(base + ['-O', 'check', destination], capture_output=True)
if args.action == 'status':
    print('Tunnel active' if check.returncode == 0 else 'Tunnel inactive')
    raise SystemExit(check.returncode)
if args.action == 'stop':
    if check.returncode == 0:
        subprocess.run(base + ['-O', 'exit', destination], check=True)
elif check.returncode != 0:
    # A trusted Mac agent can reuse the operator's already-running tunnel.
    # The dedicated kubeconfig verifies the Pi's TLS certificate and identity.
    kubeconfig = Path.home() / '.kube/clubpenguin-pi.yaml'
    if kubeconfig.exists():
        probe = subprocess.run(['kubectl', '--kubeconfig', str(kubeconfig),
                                '--request-timeout=3s', 'get', 'nodes', '-o', 'json'], capture_output=True, text=True)
        if probe.returncode == 0:
            nodes = json.loads(probe.stdout)['items']
            if any(n['metadata']['name'] == 'penguin-pi' and n['status']['nodeInfo']['architecture'] == 'arm64' for n in nodes):
                print('Existing authenticated connection to the Pi is available')
                raise SystemExit(0)
    subprocess.run(base + ['-fNT', '-o', 'ControlMaster=yes', '-o', 'ExitOnForwardFailure=yes',
                          '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
                          '-L', '127.0.0.1:16443:127.0.0.1:6443', destination], check=True)
    print('Private Kubernetes API tunnel: https://127.0.0.1:16443')
else:
    print('Tunnel already active')

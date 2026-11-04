#!/usr/bin/env python3
"""Copy the user's existing Pi kubeconfig without displaying its credentials."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', default='192.168.0.10')
parser.add_argument('--user', default='pi')
parser.add_argument('--output', type=Path, default=Path.home() / '.kube/clubpenguin-pi.yaml')
args = parser.parse_args()
args.output.parent.mkdir(parents=True, exist_ok=True)
if args.output.exists():
    raise SystemExit(f'Keeping existing configuration: {args.output}')
result = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
     f'{args.user}@{args.host}', 'cat ~/.kube/config'],
    check=True, capture_output=True,
)
fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as target:
    target.write(result.stdout)
subprocess.run(
    ['kubectl', '--kubeconfig', str(args.output), 'config', 'set-cluster',
     'default', '--server=https://127.0.0.1:16443'], check=True,
)
subprocess.run([sys.executable, str(Path(__file__).with_name('tunnel.py')), 'start',
                '--host', args.host, '--user', args.user], check=True)
subprocess.run(['kubectl', '--kubeconfig', str(args.output), 'get', 'nodes'], check=True)


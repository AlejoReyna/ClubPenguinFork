#!/usr/bin/env python3
"""Back up the lab database, verify a separate restore, then upload to Azure."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kubeconfig', default=str(Path.home() / '.kube/clubpenguin-pi.yaml'))
parser.add_argument('--account', default='sttfstate2188627882')
parser.add_argument('--container', default='clubpenguin-backups')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / '.local/backups'
root.mkdir(parents=True, mode=0o700, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
dump = root / (stamp + '.dump')
base = ['kubectl', '--kubeconfig', args.kubeconfig, '-n', 'clubpenguin', 'exec']
database = 'verify_' + stamp.lower()


def postgres(command, **kwargs):
    return subprocess.run(base + ['postgres-0', '--'] + command, check=True, capture_output=True, **kwargs)


fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as target:
    subprocess.run(base + ['postgres-0', '--', 'pg_dump', '-U', 'houdini', '-d', 'houdini',
                           '-Fc', '--no-owner', '--no-acl'], stdout=target, check=True)
postgres(['createdb', '-U', 'houdini', database])
try:
    with dump.open('rb') as source:
        subprocess.run(base + ['-i', 'postgres-0', '--', 'pg_restore', '-U', 'houdini',
                              '--no-owner', '--no-acl', '--exit-on-error', '-d', database],
                       stdin=source, check=True, capture_output=True)
    result = postgres(['psql', '-U', 'houdini', '-d', database, '-At', '-v', 'ON_ERROR_STOP=1',
                       '-c', 'SELECT count(*) FROM penguin;'])
    accounts = int(result.stdout.decode().strip())
finally:
    # This database was created only for verification, never the live database.
    if not database.startswith('verify_'):
        raise RuntimeError('Refusing to remove a database outside the verification prefix')
    postgres(['dropdb', '-U', 'houdini', database])

metadata = {
    'created_utc': stamp,
    'sha256': hashlib.sha256(dump.read_bytes()).hexdigest(),
    'bytes': dump.stat().st_size,
    'restore_verified': True,
    'restored_accounts': accounts,
}
manifest = dump.with_suffix('.json')
manifest.write_text(json.dumps(metadata, indent=2) + '\n')
manifest.chmod(0o600)
for file in [dump, manifest]:
    subprocess.run(['az', 'storage', 'blob', 'upload', '--account-name', args.account,
                    '--container-name', args.container, '--auth-mode', 'login',
                    '--name', 'postgres/' + file.name, '--file', str(file),
                    '--overwrite', 'false', '--only-show-errors', '-o', 'none'], check=True)
print(f'Backup and separate restore verified: {accounts} accounts, {metadata["bytes"]} bytes.')
print(f'Uploaded: {args.container}/postgres/{dump.name}')
print(f'Local copy: {dump}')

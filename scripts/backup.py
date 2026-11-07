#!/usr/bin/env python3
"""Back up the lab database, verify a separate restore, then upload to Azure.

Steps: pg_dump from postgres-0, restore into a throwaway `verify_*` database,
read the data back, drop only that database, then upload the dump and a SHA256
manifest to Blob. The live database is never written to.

A backup taken *after* a change does not protect against that change, so the
deploy flow runs this *before* applying any plan that touches data
(see docs/operations.md).

Must run under an Azure identity with data access to the backup container (in a
pipeline: inside an AzureCLI@2 task).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--kubeconfig')
parser.add_argument('--account', help='storage account (default: CP_STATE_STORAGE_ACCOUNT)')
parser.add_argument('--container', help='blob container (default: CP_BACKUP_CONTAINER)')
parser.add_argument('--local-dir', help='private local copy (default: ~/.clubpenguin-lab/backups)')
parser.add_argument('--reason', default='manual', help='why this backup exists (stored in the manifest)')
parser.add_argument('--json-out', help='write the non-secret manifest here as pipeline evidence')
parser.add_argument('--no-upload', action='store_true', help='verify the restore but skip Azure (offline tests)')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9 _.:/@=-]{1,100}', args.reason):
    raise SystemExit('Invalid --reason')

cfg = lablib.load_config(None if args.no_upload else 'backup')
account = args.account or cfg['CP_STATE_STORAGE_ACCOUNT']
container = args.container or cfg['CP_BACKUP_CONTAINER']
kubeconfig = args.kubeconfig or cfg['CP_KUBECONFIG']
local_root = Path(args.local_dir or os.environ.get('CP_BACKUP_LOCAL_DIR') or Path.home() / '.clubpenguin-lab/backups').expanduser()
local_root.mkdir(parents=True, mode=0o700, exist_ok=True)
local_root.chmod(0o700)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
dump = local_root / (stamp + '.dump')
base = ['kubectl', '--kubeconfig', kubeconfig, '--context', 'default', '-n', 'clubpenguin', 'exec']
database = 'verify_' + stamp.lower()


def postgres(command, timeout=300, **kwargs):
    return subprocess.run(base + ['postgres-0', '--'] + command, check=True, capture_output=True, timeout=timeout, **kwargs)


# The pod must be serving before we trust its dump.
postgres(['pg_isready', '-U', 'houdini', '-d', 'houdini'], timeout=30)
version = postgres(['pg_dump', '--version'], timeout=30).stdout.decode().strip()

fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, 'wb') as target:
        subprocess.run(base + ['postgres-0', '--', 'pg_dump', '-U', 'houdini', '-d', 'houdini',
                               '-Fc', '--no-owner', '--no-acl'], stdout=target, check=True, timeout=900)
except BaseException:
    dump.unlink(missing_ok=True)  # never leave a partial dump that looks like a backup
    raise
if dump.stat().st_size == 0:
    dump.unlink()
    raise SystemExit('pg_dump produced an empty file')

postgres(['createdb', '-U', 'houdini', database])
try:
    with dump.open('rb') as source:
        subprocess.run(base + ['-i', 'postgres-0', '--', 'pg_restore', '-U', 'houdini',
                              '--no-owner', '--no-acl', '--exit-on-error', '-d', database],
                       stdin=source, check=True, capture_output=True, timeout=900)
    result = postgres(['psql', '-U', 'houdini', '-d', database, '-At', '-v', 'ON_ERROR_STOP=1',
                       '-c', 'SELECT count(*) FROM penguin;'])
    accounts = int(result.stdout.decode().strip())
finally:
    # This database was created only for verification, never the live database.
    if not database.startswith('verify_'):
        raise RuntimeError('Refusing to remove a database outside the verification prefix')
    postgres(['dropdb', '-U', 'houdini', '--if-exists', database])

metadata = {
    'created_utc': stamp,
    'reason': args.reason,
    'sha256': hashlib.sha256(dump.read_bytes()).hexdigest(),
    'bytes': dump.stat().st_size,
    'restore_verified': True,
    'restored_accounts': accounts,
    'pg_dump': version,
    'blob': f'postgres/{dump.name}',
}
manifest = dump.with_suffix('.json')
lablib.write_private(manifest, json.dumps(metadata, indent=2) + '\n')
if not args.no_upload:
    for file in [dump, manifest]:
        subprocess.run(['az', 'storage', 'blob', 'upload', '--account-name', account,
                        '--container-name', container, '--auth-mode', 'login',
                        '--name', 'postgres/' + file.name, '--file', str(file),
                        '--overwrite', 'false', '--only-show-errors', '-o', 'none'], check=True, timeout=600)
if args.json_out:
    lablib.write_json(args.json_out, metadata)
print(f'Backup and separate restore verified: {accounts} accounts, {metadata["bytes"]} bytes.')
if args.no_upload:
    print('Upload skipped (--no-upload).')
else:
    print(f'Uploaded: {container}/postgres/{dump.name}')
print(f'Local copy: {dump}')

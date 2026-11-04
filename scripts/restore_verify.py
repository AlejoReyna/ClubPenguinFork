#!/usr/bin/env python3
"""Prove that a recorded backup can be restored, WITHOUT touching the live database.

Downloads (or reads) a dump and its manifest, checks size and SHA-256 against the
manifest, restores it into a throwaway `restore_*` database inside postgres-0, reads the
data back and drops that database. The live `houdini` database is never written; replacing
it is a separate, manual and explicit procedure (docs/ci-cd/respaldos-y-rollback.md).
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
src = parser.add_mutually_exclusive_group(required=True)
src.add_argument('--blob', help='blob name, e.g. postgres/20261103T155347Z.dump')
src.add_argument('--dump', help='local dump file (its .json manifest must sit next to it)')
parser.add_argument('--kubeconfig')
parser.add_argument('--keep', action='store_true', help='keep the scratch database for inspection')
args = parser.parse_args()

cfg = lablib.load_config('backup' if args.blob else None)
kubeconfig = args.kubeconfig or cfg['CP_KUBECONFIG']
base = ['kubectl', '--kubeconfig', kubeconfig, '--context', 'default', '-n', 'clubpenguin', 'exec']

with tempfile.TemporaryDirectory(prefix='cp-restore-') as temp:
    if args.blob:
        if not re.fullmatch(r'postgres/\d{8}T\d{6}Z\.dump', args.blob):
            raise SystemExit('Invalid blob name')
        dump = Path(temp) / Path(args.blob).name
        for name in (args.blob, args.blob[:-5] + '.json'):
            subprocess.run(['az', 'storage', 'blob', 'download', '--account-name', cfg['CP_STATE_STORAGE_ACCOUNT'],
                            '--container-name', cfg['CP_BACKUP_CONTAINER'], '--auth-mode', 'login', '--name', name,
                            '--file', str(Path(temp) / Path(name).name), '--only-show-errors', '-o', 'none'],
                           check=True, timeout=600)
    else:
        dump = Path(args.dump)
    manifest = json.loads(dump.with_suffix('.json').read_text())
    data = dump.read_bytes()
    if len(data) != manifest['bytes'] or hashlib.sha256(data).hexdigest() != manifest['sha256']:
        raise SystemExit('FAIL: the dump does not match its manifest (size or SHA-256)')
    print('Dump matches its manifest (size and SHA-256).')

    scratch = 'restore_' + time.strftime('%Y%m%dt%H%M%S', time.gmtime())
    if scratch == 'houdini' or not scratch.startswith('restore_'):
        raise SystemExit('Refusing to use a non-scratch database name')

    def postgres(command, **kwargs):
        return subprocess.run(base + ['postgres-0', '--'] + command, check=True, capture_output=True, timeout=900, **kwargs)

    postgres(['createdb', '-U', 'houdini', scratch])
    try:
        with dump.open('rb') as handle:
            subprocess.run(base + ['-i', 'postgres-0', '--', 'pg_restore', '-U', 'houdini', '--no-owner', '--no-acl',
                                   '--exit-on-error', '-d', scratch], stdin=handle, check=True, capture_output=True, timeout=900)
        accounts = int(postgres(['psql', '-U', 'houdini', '-d', scratch, '-At', '-v', 'ON_ERROR_STOP=1',
                                 '-c', 'SELECT count(*) FROM penguin;']).stdout.decode().strip())
    finally:
        if not args.keep:
            postgres(['dropdb', '-U', 'houdini', '--if-exists', scratch])
    print(f'PASS: restored into scratch database {scratch}; {accounts} accounts read back '
          f'(manifest recorded {manifest.get("restored_accounts")}).')
    print('The live database was not touched.' + (f' Scratch database kept: {scratch}.' if args.keep else ''))

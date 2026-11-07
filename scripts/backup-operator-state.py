#!/usr/bin/env python3
"""Private backup of external Secrets, bot ledger and device pairing database."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess

import lablib
import tunnel


def main():
    cfg = lablib.load_config('backup')
    ok, message = tunnel.verify_target(cfg['CP_KUBECONFIG'], cfg['CP_NODE_NAME'])
    if not ok:
        raise SystemExit(message)
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = Path.home() / '.clubpenguin-lab/backups/operator' / stamp
    directory.mkdir(parents=True, mode=0o700)
    command = ['kubectl', '--kubeconfig', cfg['CP_KUBECONFIG'], '--context', 'default', '-n', 'clubpenguin']
    written = []

    def capture(name, args, optional=False):
        result = subprocess.run(command + args, capture_output=True, timeout=60)
        if result.returncode:
            if optional and b'NotFound' in result.stderr:
                return
            raise SystemExit('Private operator backup failed: ' + name)
        target = directory / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(result.stdout)
        written.append(target)

    for secret in ('bot-credentials', 'moonshot-api', 'puffle-service', 'cloudflared-token'):
        capture(secret + '.json', ['get', 'secret', secret, '-o', 'json'], optional=True)
    found = subprocess.run(command + ['get', 'deploy', 'bots', '-o', 'name'], capture_output=True)
    if found.returncode == 0:
        capture('bots-budget.json', ['exec', 'deploy/bots', '--', 'cat', '/var/lib/bots/budget.json'])
    found = subprocess.run(command + ['get', 'deploy', 'puffle', '-o', 'name'], capture_output=True)
    if found.returncode == 0:
        # sqlite.backup takes a consistent snapshot, never a torn live file copy.
        program = ('import sqlite3,tempfile,pathlib,sys; '
                   'source=sqlite3.connect("file:/var/lib/puffle/devices.sqlite?mode=ro",uri=True); '
                   'destination=sqlite3.connect(":memory:"); source.backup(destination); '
                   'sys.stdout.buffer.write(destination.serialize())')
        capture('puffle-devices.sqlite', ['exec', 'deploy/puffle', '--', 'python', '-c', program])
    manifest = {'created_utc': stamp, 'files': {p.name: {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                                                        'bytes': p.stat().st_size} for p in written}}
    lablib.write_json(directory / 'manifest.json', manifest)
    for path in written + [directory / 'manifest.json']:
        result = subprocess.run(['az', 'storage', 'blob', 'upload', '--account-name', cfg['CP_STATE_STORAGE_ACCOUNT'],
                                 '--container-name', cfg['CP_BACKUP_CONTAINER'], '--auth-mode', 'login',
                                 '--name', f'operator/{stamp}/{path.name}', '--file', str(path),
                                 '--overwrite', 'false', '--only-show-errors', '-o', 'none'], capture_output=True, timeout=120)
        if result.returncode:
            raise SystemExit('Operator backup upload failed; private local copy retained.')
    print(f'Private operator backup: {len(written)} files saved and uploaded; values not printed.')


if __name__ == '__main__':
    main()

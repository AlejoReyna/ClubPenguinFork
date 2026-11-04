"""End-to-end check of scripts/backup.py against a throwaway PostgreSQL container.

A `kubectl` shim maps `kubectl ... exec postgres-0 -- CMD` onto `docker exec` in the
test container, so the real dump / restore-into-scratch-database / read-back logic runs
without a cluster, Azure or any live data. Skipped unless Docker and an image are available
(set CP_TEST_DB_IMAGE, e.g. clubpenguin/database:<tag>).
"""
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get('CP_TEST_DB_IMAGE', '')
LABEL = 'clubpenguin.lab.test-run=backup-' + secrets.token_hex(3)


def docker_ok():
    return shutil.which('docker') and subprocess.run(['docker', 'info'], capture_output=True).returncode == 0


@unittest.skipUnless(IMAGE and docker_ok(), 'set CP_TEST_DB_IMAGE and run Docker to execute this test')
class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'penguin-test-' + secrets.token_hex(4)
        cls.network = cls.name
        subprocess.run(['docker', 'network', 'create', '--internal', '--label', LABEL, cls.network], check=True, capture_output=True)
        subprocess.run(['docker', 'run', '-d', '--name', cls.name, '--label', LABEL, '--network', cls.network,
                        '-e', 'POSTGRES_USER=houdini', '-e', 'POSTGRES_DB=houdini',
                        '-e', 'POSTGRES_PASSWORD=' + secrets.token_hex(8), IMAGE], check=True, capture_output=True)
        for _ in range(120):
            # initdb temporarily accepts Unix-socket connections, then restarts.
            # TCP readiness proves the final server is running before backup tests.
            if subprocess.run(['docker', 'exec', cls.name, 'pg_isready', '-h', '127.0.0.1', '-U', 'houdini', '-d', 'houdini'],
                              capture_output=True).returncode == 0:
                # initdb restarts the server once; require it to answer a real query.
                if subprocess.run(['docker', 'exec', cls.name, 'psql', '-h', '127.0.0.1', '-U', 'houdini', '-d', 'houdini', '-Atc',
                                   'SELECT count(*) FROM penguin'], capture_output=True).returncode == 0:
                    break
            time.sleep(1)
        else:
            raise RuntimeError('test PostgreSQL did not start')

    @classmethod
    def tearDownClass(cls):
        found = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label={LABEL}'], capture_output=True, text=True).stdout.split()
        for name in found:
            subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', cls.network], capture_output=True)

    def shim(self, directory, container):
        path = Path(directory) / 'kubectl'
        path.write_text('#!/bin/sh\nwhile [ "$1" != "exec" ]; do shift; done; shift\nflag=""\n'
                        'if [ "$1" = "-i" ]; then flag="-i"; shift; fi\nshift; shift\n'
                        f'exec docker exec $flag {container} "$@"\n')
        path.chmod(0o755)

    def run_backup(self, container, local):
        with tempfile.TemporaryDirectory() as shim_dir:
            self.shim(shim_dir, container)
            env = dict(os.environ, PATH=f'{shim_dir}:{os.environ["PATH"]}', CP_CONFIG_FILE='/nonexistent')
            return subprocess.run([sys.executable, str(ROOT / 'scripts/backup.py'), '--no-upload', '--kubeconfig', '/dev/null',
                                   '--local-dir', str(local), '--reason', 'unit test'], capture_output=True, text=True, env=env)

    def test_dump_is_restored_into_a_scratch_database_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / 'backups'
            result = self.run_backup(self.name, local)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            self.assertIn('Backup and separate restore verified', result.stdout)
            manifest = json.loads(next(local.glob('*.json')).read_text())
            self.assertTrue(manifest['restore_verified'])
            self.assertEqual(manifest['reason'], 'unit test')
            self.assertGreater(manifest['bytes'], 1000)
            for file in local.iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600, file.name)
            self.assertEqual(stat.S_IMODE(local.stat().st_mode), 0o700)
            databases = subprocess.run(['docker', 'exec', self.name, 'psql', '-U', 'houdini', '-d', 'houdini', '-Atc',
                                        'SELECT datname FROM pg_database'], capture_output=True, text=True).stdout.split()
            self.assertEqual([d for d in databases if d.startswith('verify_')], [], 'scratch database must be dropped')
            self.assertIn('houdini', databases, 'the live database is untouched')

    def test_recorded_backup_is_verifiable_without_touching_the_live_database(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as shim_dir:
            local = Path(temp) / 'backups'
            self.assertEqual(self.run_backup(self.name, local).returncode, 0)
            dump = next(local.glob('*.dump'))
            self.shim(shim_dir, self.name)
            env = dict(os.environ, PATH=f'{shim_dir}:{os.environ["PATH"]}', CP_CONFIG_FILE='/nonexistent')
            script = str(ROOT / 'scripts/restore_verify.py')
            ok = subprocess.run([sys.executable, script, '--dump', str(dump), '--kubeconfig', '/dev/null'],
                                capture_output=True, text=True, env=env)
            self.assertEqual(ok.returncode, 0, ok.stderr[-600:])
            self.assertIn('The live database was not touched', ok.stdout)
            # a corrupted dump must be refused before anything is restored
            dump.write_bytes(dump.read_bytes()[:-10] + b'corrupted!')
            bad = subprocess.run([sys.executable, script, '--dump', str(dump), '--kubeconfig', '/dev/null'],
                                 capture_output=True, text=True, env=env)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('does not match its manifest', bad.stdout + bad.stderr)
            databases = subprocess.run(['docker', 'exec', self.name, 'psql', '-U', 'houdini', '-d', 'houdini', '-Atc',
                                        'SELECT datname FROM pg_database'], capture_output=True, text=True).stdout.split()
            self.assertEqual([d for d in databases if d.startswith('restore_')], [])

    def test_unreachable_database_fails_without_uploading_or_leaving_a_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / 'backups'
            result = self.run_backup('no-such-container', local)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(local.glob('*.json')), [], 'no manifest means nothing would be uploaded')


if __name__ == '__main__':
    unittest.main()

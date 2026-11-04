import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lablib  # noqa: E402

GOOD = {
    'CP_PUBLIC_HOSTNAME': 'club.lab.dev', 'CP_PI_HOST': '10.1.2.3', 'CP_PI_USER': 'operator',
    'CP_ACR_NAME': 'acrlabtest', 'CP_LAN_CIDR': '10.1.2.0/24', 'CP_STATE_RESOURCE_GROUP': 'rg-state', 'CP_STATE_STORAGE_ACCOUNT': 'sttest12345',
}


class ConfigTests(unittest.TestCase):
    def load(self, env, phase='plan', config_file=None):
        return lablib.load_config(phase, environ=env, config_file=config_file or Path('/nonexistent'))

    def test_complete_configuration_loads_with_defaults(self):
        cfg = self.load(GOOD)
        self.assertEqual(cfg['CP_STATE_CONTAINER'], 'tfstate-clubpenguin')
        self.assertEqual(cfg['CP_NODE_NAME'], 'penguin-pi')
        self.assertTrue(cfg['CP_KUBECONFIG'].endswith('.kube/clubpenguin-pi.yaml'))

    def test_missing_required_value_fails_before_anything_runs(self):
        env = dict(GOOD)
        del env['CP_ACR_NAME']
        with self.assertRaises(SystemExit):
            self.load(env)

    def test_anonymized_sample_values_are_rejected(self):
        for key, value in (('CP_PUBLIC_HOSTNAME', 'club.example.com'), ('CP_PI_HOST', '192.168.0.10'), ('CP_LAN_CIDR', '192.168.0.0/24')):
            env = dict(GOOD, **{key: value})
            with self.assertRaises(SystemExit, msg=key):
                self.load(env)

    def test_invalid_formats_are_rejected(self):
        for key, value in (('CP_ACR_NAME', 'Bad Name;rm'), ('CP_PI_USER', 'a b'), ('CP_PUBLIC_HOSTNAME', 'http://x.dev'), ('CP_LAN_CIDR', '10.0.0.0/99')):
            with self.assertRaises(SystemExit, msg=key):
                self.load(dict(GOOD, **{key: value}))

    def test_file_is_read_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'deploy.env'
            path.write_text('# comment\n' + '\n'.join(f'{k}={v}' for k, v in GOOD.items()) + '\n')
            path.chmod(0o600)
            cfg = self.load({'CP_PI_USER': 'other'}, config_file=path)
            self.assertEqual(cfg['CP_PI_USER'], 'other')
            self.assertEqual(cfg['CP_ACR_NAME'], 'acrlabtest')

    def test_file_readable_by_others_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'deploy.env'
            path.write_text('CP_PI_USER=operator\n')
            path.chmod(0o644)
            with self.assertRaises(SystemExit):
                self.load({}, phase=None, config_file=path)

    def test_file_rejects_non_cp_keys_and_shell_syntax(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'deploy.env'
            path.write_text('PATH=/evil\n')
            path.chmod(0o600)
            with self.assertRaises(SystemExit):
                self.load({}, phase=None, config_file=path)

    def test_backend_config_has_no_literal_defaults(self):
        backend = lablib.backend_config(self.load(GOOD), 'cluster')
        self.assertEqual(backend['key'], 'cluster.tfstate')
        self.assertEqual(backend['storage_account_name'], 'sttest12345')
        self.assertIn('use_azuread_auth = true', lablib.render_hcl(backend))


class RunDirTests(unittest.TestCase):
    def test_private_modes_and_removal_scoped_to_the_run(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = lablib.RunDir(base=Path(temp) / 'runs', run_id='run-12-r')
            self.assertEqual(stat.S_IMODE(ws.path.stat().st_mode), 0o700)
            other = lablib.RunDir(base=Path(temp) / 'runs', run_id='run-13-r')
            self.assertTrue(ws.remove())
            self.assertFalse(ws.path.exists())
            self.assertTrue(other.path.exists(), 'another run must not be touched')

    def test_invalid_run_ids_are_refused(self):
        for bad in ('../x', 'run-1', 'run-1-r/..', 'plan'):
            with self.assertRaises(SystemExit):
                lablib.RunDir(base='/tmp/x', run_id=bad, create=False)

    def test_prune_only_removes_old_run_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'runs'
            current = lablib.RunDir(base=base, run_id='run-3-r')
            old = lablib.RunDir(base=base, run_id='run-1-r')
            recent = lablib.RunDir(base=base, run_id='run-2-r')
            unrelated = base / 'keep-me'
            unrelated.mkdir()
            past = time.time() - 10 * 86400
            for path in (old.path, unrelated):
                os.utime(path, (past, past))
            self.assertEqual(current.prune_stale(days=3), ['run-1-r'])
            self.assertTrue(recent.path.exists() and unrelated.exists())

    def test_symlinked_run_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'runs'
            ws = lablib.RunDir(base=base, run_id='run-5-r')
            victim = Path(temp) / 'victim'
            victim.mkdir()
            (victim / 'file').write_text('data')
            ws.remove()
            ws.path.symlink_to(victim)
            self.assertFalse(ws.remove())
            self.assertTrue((victim / 'file').exists())

    def test_write_private_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'real'
            target.write_text('x')
            link = Path(temp) / 'link'
            link.symlink_to(target)
            with self.assertRaises(SystemExit):
                lablib.write_private(link, 'secret')
            self.assertEqual(target.read_text(), 'x')


class RedactionTests(unittest.TestCase):
    def test_tokens_and_passwords_are_masked(self):
        text = ('token=abcd1234efgh Authorization: Bearer abcdef0123456789 '
                'cloudflare eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl password: "hunter22" sk-ABCDEFGHIJKLMNOPQRSTUV')
        masked = lablib.redact(text)
        for leaked in ('abcd1234efgh', 'abcdef0123456789', 'eyJhbGciOiJI', 'hunter22', 'sk-ABCDEFGH'):
            self.assertNotIn(leaked, masked)

    def test_pipeline_variable_rejects_injection(self):
        for bad in ('x\n##vso[task.setvariable variable=y]z', 'a;b', 'x$(y)'):
            with self.assertRaises(SystemExit):
                lablib.set_pipeline_variable('v', bad)


if __name__ == '__main__':
    unittest.main()

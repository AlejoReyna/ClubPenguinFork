import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import changed  # noqa: E402
import tunnel  # noqa: E402


def fake_kubectl(directory, server, nodes):
    """A kubectl stand-in that answers the two queries verify_target makes."""
    script = Path(directory) / 'kubectl'
    script.write_text('#!/bin/sh\ncase "$*" in\n'
                      f'  *"config view"*) printf %s \'{server}\' ;;\n'
                      f'  *"get nodes"*) cat <<\'EOF\'\n{json.dumps({"items": nodes})}\nEOF\n;;\n'
                      '  *) exit 1 ;;\nesac\n')
    script.chmod(0o755)


def node(name='penguin-pi', arch='arm64', ready='True', labels=None):
    return {'metadata': {'name': name, 'labels': labels or {}},
            'status': {'nodeInfo': {'architecture': arch}, 'conditions': [{'type': 'Ready', 'status': ready}]}}


class VerifyTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.kubeconfig = self.dir / 'kube.yaml'
        self.kubeconfig.write_text('x')
        self.kubeconfig.chmod(0o600)
        self.old_path = os.environ['PATH']
        os.environ['PATH'] = f'{self.dir}:{self.old_path}'

    def tearDown(self):
        os.environ['PATH'] = self.old_path
        self.temp.cleanup()

    def check(self, server=tunnel.EXPECTED_SERVER, nodes=None):
        fake_kubectl(self.dir, server, nodes if nodes is not None else [node()])
        return tunnel.verify_target(str(self.kubeconfig), 'penguin-pi')

    def test_the_pi_passes(self):
        ok, message = self.check()
        self.assertTrue(ok, message)

    def test_an_aks_api_is_refused(self):
        self.assertFalse(self.check(server='https://aks-1.hcp.eastus.azmk8s.io:443')[0])
        self.assertFalse(self.check(nodes=[node(name='aks-nodepool1', labels={'kubernetes.azure.com/cluster': 'x'})])[0])

    def test_wrong_node_architecture_or_state_is_refused(self):
        self.assertFalse(self.check(nodes=[node(arch='amd64')])[0])
        self.assertFalse(self.check(nodes=[node(ready='False')])[0])
        self.assertFalse(self.check(nodes=[node(), node(name='extra')])[0])

    def test_kubeconfig_readable_by_others_is_refused(self):
        self.kubeconfig.chmod(0o644)
        self.assertFalse(self.check()[0])


class OwnershipTests(unittest.TestCase):
    def test_a_reused_tunnel_is_never_recorded_as_ours_and_ownership_is_sticky(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'tunnel.state'
            tunnel.record(str(state), False)
            self.assertFalse(json.loads(state.read_text())['started'])
            tunnel.record(str(state), True)
            self.assertTrue(json.loads(state.read_text())['started'])
            tunnel.record(str(state), False)  # a later `start` in the same run must not lose ownership
            self.assertTrue(json.loads(state.read_text())['started'])
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)


class ChangeClassificationTests(unittest.TestCase):
    def runs(self, *files):
        return changed.classify(list(files))['runContainers']

    def test_docs_infra_and_monitoring_alone_skip_container_tests(self):
        self.assertFalse(self.runs('docs/ci-cd/x.md', 'README.md', 'infra/cluster/web.tf', 'monitoring/grafana.yaml'))

    def test_anything_shared_or_unknown_runs_them(self):
        for path in ('scripts/test-containers.py', 'sources.json', 'azure-pipelines.yml', 'containers/web/index.html',
                     'containers/runtime/socket_proxy.py', 'tests/test_x.py', 'new-top-level-file.bin', 'containers/web/NOTES.md'):
            self.assertTrue(self.runs(path), path)

    def test_mixed_changes_run_them(self):
        self.assertTrue(self.runs('README.md', 'sources.json'))

    def test_main_always_runs_everything(self):
        self.assertTrue(changed.classify([], everything=True)['runContainers'])


if __name__ == '__main__':
    unittest.main()

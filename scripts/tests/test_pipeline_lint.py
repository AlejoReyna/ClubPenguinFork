import copy
import sys
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # the validation entry point installs PyYAML before importing this module
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(yaml is None, 'PyYAML not available')
class PipelinePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lint_pipeline
        cls.lint = staticmethod(lint_pipeline.lint)
        cls.base = yaml.safe_load((ROOT / 'azure-pipelines.yml').read_text())
        cls.deploy = (ROOT / 'scripts/deploy.py').read_text()

    def problems(self, mutate=None, deploy=None):
        document = copy.deepcopy(self.base)
        if mutate:
            mutate(document)
        return self.lint(document, self.deploy if deploy is None else deploy)

    def stage(self, document, name):
        for item in document['stages']:
            candidates = [item] if 'stage' in item else [s for v in item.values() for s in v]
            for candidate in candidates:
                if candidate.get('stage') == name:
                    return candidate

    def test_the_shipped_pipeline_passes(self):
        self.assertEqual(self.problems(), [])

    def test_deploy_must_default_to_false(self):
        def mutate(d):
            next(p for p in d['parameters'] if p['name'] == 'deploy')['default'] = True
        self.assertTrue(any('deploy' in p for p in self.problems(mutate)))

    def test_auto_approve_and_direct_terraform_are_rejected(self):
        def mutate(d):
            self.stage(d, 'Validate')['jobs'][0]['steps'].append({'bash': 'terraform apply -auto-approve'})
        found = ' '.join(self.problems(mutate))
        self.assertIn('auto-approve', found)
        self.assertIn('Terraform only through', found)

    def test_secret_leaking_commands_are_rejected(self):
        for command in ('printenv', 'set -x', 'kubectl -n clubpenguin get secret grafana-admin -o yaml', 'docker run img:latest'):
            def mutate(d, command=command):
                self.stage(d, 'Validate')['jobs'][0]['steps'].append({'bash': command})
            self.assertTrue(self.problems(mutate), command)

    def test_pull_requests_must_not_reach_the_release_stage(self):
        def mutate(d):
            self.stage(d, 'Release')['condition'] = "succeeded()"
        self.assertTrue(any('Release' in p for p in self.problems(mutate)))

    def test_credentials_on_a_hosted_stage_are_rejected(self):
        def mutate(d):
            self.stage(d, 'Test')['jobs'][0]['steps'].append(
                {'task': 'AzureCLI@2', 'inputs': {'azureSubscription': 'prod', 'scriptType': 'bash'}})
        self.assertTrue(any('hosted agents must never receive credentials' in p or 'PullRequest' in p for p in self.problems(mutate)))

    def test_exposing_the_federated_token_is_rejected(self):
        def mutate(d):
            self.stage(d, 'Release')['jobs'][0]['steps'].append(
                {'task': 'AzureCLI@2', 'inputs': {'azureSubscription': 'x', 'addSpnToEnvironment': 'true'}})
        self.assertTrue(any('federated token' in p for p in self.problems(mutate)))

    def test_apply_outside_a_deployment_job_is_rejected(self):
        def mutate(d):
            self.stage(d, 'Release')['jobs'][0]['steps'].append({'bash': 'python3 scripts/deploy.py apply --expect-sha256 x'})
        self.assertTrue(any('deployment job' in p for p in self.problems(mutate)))

    def test_apply_without_the_reviewed_plan_hash_is_rejected(self):
        def mutate(d):
            job = self.stage(d, 'Apply')['jobs'][0]
            for step in job['strategy']['runOnce']['deploy']['steps']:
                inputs = step.get('inputs', {})
                if 'deploy.py apply' in inputs.get('inlineScript', ''):
                    inputs['inlineScript'] = inputs['inlineScript'].replace('--expect-sha256 "$(planSha256)"', '')
        self.assertTrue(any('hash of the reviewed plan' in p for p in self.problems(mutate)))

    def test_jobs_need_timeouts_and_an_environment(self):
        def mutate(d):
            del self.stage(d, 'Test')['jobs'][0]['timeoutInMinutes']
            del self.stage(d, 'Apply')['jobs'][0]['environment']
        found = ' '.join(self.problems(mutate))
        self.assertIn('timeoutInMinutes', found)
        self.assertIn('environment', found)

    def test_losing_the_serialization_lock_is_rejected(self):
        def mutate(d):
            del d['lockBehavior']
        self.assertTrue(any('lockBehavior' in p for p in self.problems(mutate)))

    def test_cloudflared_must_stay_explicitly_enabled(self):
        self.assertTrue(any('cloudflared' in p for p in self.problems(deploy=self.deploy.replace('-var=cloudflared_enabled=true', ''))))
        self.assertTrue(any('never disable' in p for p in self.problems(deploy=self.deploy + '\n# cloudflared_enabled=false\n')))

    def test_provider_credentials_cannot_be_wired_in(self):
        def mutate(d):
            self.stage(d, 'Test')['jobs'][0]['steps'].append({'bash': 'echo hi', 'env': {'MOONSHOT_API_KEY': '$(key)'}})
        self.assertTrue(any('paid-provider' in p for p in self.problems(mutate)))


if __name__ == '__main__':
    unittest.main()

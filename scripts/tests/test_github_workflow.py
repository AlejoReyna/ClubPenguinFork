import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class WorkflowTests(unittest.TestCase):
    def test_untrusted_prs_never_use_the_runner_or_azure(self):
        workflow = yaml.load((ROOT / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['push']['branches'], ['main'])
        self.assertEqual(workflow['on']['pull_request']['branches'], ['main'])
        jobs = workflow['jobs']
        for name in ('validate', 'build-and-test'):
            self.assertEqual(jobs[name]['runs-on'], 'ubuntu-24.04')
            self.assertNotIn('id-token', jobs[name].get('permissions', {}))
        job = jobs['deploy']
        self.assertIn("github.event_name == 'push'", job['if'])
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertIn('CP_AUTO_DEPLOY_ENABLED', job['if'])
        self.assertEqual(job['needs'], 'build-and-test')
        self.assertEqual(job['concurrency']['cancel-in-progress'], 'false')
        runs = '\n'.join(s.get('run', '') for s in job['steps'])
        for required in ('github_deploy_guard.py', '--expect images-only', '--expect-sha256',
                         '--skip-game-journey', 'deploy.py cleanup'):
            self.assertIn(required, runs)
        self.assertNotIn('terraform apply', runs)

    def test_isolated_ci_runs_real_bot_fixture(self):
        workflow = yaml.load((ROOT / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        runs = '\n'.join(s.get('run', '') for s in workflow['jobs']['build-and-test']['steps'])
        self.assertIn('--bots-tag', runs)
        self.assertIn('run-bot-tests.sh', runs)


if __name__ == '__main__':
    unittest.main()

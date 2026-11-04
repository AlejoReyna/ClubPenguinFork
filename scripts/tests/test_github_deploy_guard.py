import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_deploy_guard


class BotDeploymentGuardTests(unittest.TestCase):
    def test_live_provider_and_single_owner_must_match(self):
        deployment = {'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
                                'template': {'spec': {'containers': [
                                    {'name': 'bots', 'env': [{'name': 'BOTS_PROVIDER_MODE', 'value': 'api'}]}]}}}}
        github_deploy_guard.check(deployment, 'api')
        for change in ('replicas', 'strategy', 'mode'):
            candidate = copy.deepcopy(deployment)
            if change == 'replicas':
                candidate['spec']['replicas'] = 2
            elif change == 'strategy':
                candidate['spec']['strategy']['type'] = 'RollingUpdate'
            else:
                candidate['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'simulated'
            with self.assertRaises(ValueError, msg=change):
                github_deploy_guard.check(candidate, 'api')
        with self.assertRaises(ValueError):
            github_deploy_guard.check(deployment, '')

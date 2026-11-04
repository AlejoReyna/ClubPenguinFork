import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plan_gate  # noqa: E402

SECRET = 'S3CR3T-VALUE-THAT-MUST-NEVER-APPEAR'
REG = 'registry.invalid/clubpenguin'


def deployment(address, old_tag, new_tag, name='x', actions=('update',), extra_after=None):
    def doc(tag, **more):
        body = {'metadata': [{'name': name, 'namespace': 'clubpenguin', 'annotations': {}}],
                'spec': [{'template': [{'spec': [{'container': [{'name': name, 'image': f'{REG}/{name}:{tag}'}]}]}]}]}
        body.update(more)
        return body
    after = doc(new_tag, **(extra_after or {}))
    return {'address': address, 'type': address.split('.')[0], 'change': {
        'actions': list(actions), 'before': doc(old_tag), 'after': after, 'after_unknown': {}}}


def resource(address, actions, before=None, after=None, namespace='clubpenguin'):
    meta = [{'name': 'n', 'namespace': namespace}]
    return {'address': address, 'type': address.split('.')[0], 'change': {
        'actions': list(actions), 'before': before if before is not None else {'metadata': meta},
        'after': after if after is not None else {'metadata': meta}, 'after_unknown': {}}}


def gate(resources, live=None, allow=(), expect=None):
    changes = plan_gate.summarize({'resource_changes': resources})
    return changes, plan_gate.classify(changes, live, allow, expect)


class ScopeTests(unittest.TestCase):
    def test_frontend_only_release_restarts_web_and_requires_no_backup(self):
        changes, verdict = gate([
            deployment('kubernetes_deployment_v1.web[0]', 'web-old', 'web-new', 'web'),
            resource('kubernetes_stateful_set_v1.postgres[0]', ['no-op']),
            resource('kubernetes_config_map_v1.release[0]', ['update'], {'metadata': [{'namespace': 'clubpenguin'}], 'data': {'a': '1'}},
                     {'metadata': [{'namespace': 'clubpenguin'}], 'data': {'a': '2'}}),
        ])
        self.assertEqual([c['address'] for c in changes], ['kubernetes_deployment_v1.web[0]', 'kubernetes_config_map_v1.release[0]'])
        self.assertFalse(verdict['backup_required'])
        self.assertEqual(verdict['blocked'], [])
        self.assertEqual(changes[0]['images'], {'spec[0].template[0].spec[0].container[0].image': ['web-old', 'web-new']})

    def test_runtime_image_change_requires_a_backup_first(self):
        _, verdict = gate([deployment('kubernetes_deployment_v1.runtime["glaciar"]', 'rt-1', 'rt-2', 'glaciar')])
        self.assertTrue(verdict['backup_required'])
        self.assertIn('runtime["glaciar"]', verdict['backup_reasons'][0])

    def test_exporter_and_annotation_only_changes_do_not_force_a_backup(self):
        _, verdict = gate([deployment('kubernetes_deployment_v1.runtime["exporter"]', 'rt-1', 'rt-2', 'exporter')])
        self.assertFalse(verdict['backup_required'])
        change = resource('kubernetes_deployment_v1.runtime["login"]', ['update'],
                          {'metadata': [{'namespace': 'clubpenguin', 'annotations': {}}]},
                          {'metadata': [{'namespace': 'clubpenguin', 'annotations': {'a': 'b'}}]})
        _, verdict = gate([change])
        self.assertFalse(verdict['backup_required'], 'metadata-only change does not endanger data')

    def test_database_image_change_requires_a_backup(self):
        _, verdict = gate([deployment('kubernetes_stateful_set_v1.postgres[0]', 'db-1', 'db-2', 'postgres')])
        self.assertTrue(verdict['backup_required'])


class DestructiveChangeTests(unittest.TestCase):
    def test_replacing_postgres_or_its_volume_is_blocked(self):
        for address in ('kubernetes_stateful_set_v1.postgres[0]', 'kubernetes_persistent_volume_claim_v1.postgres[0]',
                        'random_password.postgres[0]', 'kubernetes_secret_v1.postgres[0]'):
            _, verdict = gate([resource(address, ['delete', 'create'])])
            self.assertTrue(verdict['blocked'], address)

    def test_namespaces_and_helm_releases_are_protected(self):
        for address in ('kubernetes_namespace_v1.lab["clubpenguin"]', 'helm_release.grafana'):
            _, verdict = gate([resource(address, ['delete'])])
            self.assertTrue(verdict['blocked'], address)

    def test_retiring_the_tunnel_is_blocked(self):
        for address in ('kubernetes_deployment_v1.cloudflared[0]', 'kubernetes_network_policy_v1.web_from_cloudflared[0]'):
            _, verdict = gate([resource(address, ['delete'])])
            self.assertTrue(verdict['blocked'], address)

    def test_explicit_allow_unblocks_only_that_address(self):
        address = 'kubernetes_stateful_set_v1.postgres[0]'
        _, verdict = gate([resource(address, ['delete', 'create'])], allow=[address])
        self.assertEqual(verdict['blocked'], [])
        _, verdict = gate([resource(address, ['delete', 'create']),
                           resource('kubernetes_persistent_volume_claim_v1.postgres[0]', ['delete'])], allow=[address])
        self.assertEqual(len(verdict['blocked']), 1)

    def test_foreign_namespace_and_unknown_types_are_blocked(self):
        _, verdict = gate([resource('kubernetes_secret_v1.other', ['create'], namespace='kube-system')])
        self.assertTrue(any('namespace outside the lab' in b for b in verdict['blocked']))
        _, verdict = gate([resource('azurerm_kubernetes_cluster.aks', ['update'])])
        self.assertTrue(any('outside the lab scope' in b for b in verdict['blocked']))


class AdoptionTests(unittest.TestCase):
    LIVE = {'deployment/cloudflared': True, 'networkpolicy/web-from-cloudflared': True, 'secret/cloudflared-token': True}

    def test_creating_a_connector_that_already_exists_is_blocked_with_import_hint(self):
        changes, verdict = gate([resource('kubernetes_deployment_v1.cloudflared[0]', ['create'], {},
                                          {'metadata': [{'namespace': 'clubpenguin'}]}),
                                 resource('kubernetes_network_policy_v1.web_from_cloudflared[0]', ['create'], {},
                                          {'metadata': [{'namespace': 'clubpenguin'}]})], live=self.LIVE)
        self.assertEqual(len(verdict['adoption_required']), 2)
        self.assertTrue(any('terraform import' in b for b in verdict['blocked']))

    def test_creating_it_on_a_cluster_without_it_is_allowed(self):
        _, verdict = gate([resource('kubernetes_deployment_v1.cloudflared[0]', ['create'], {},
                                    {'metadata': [{'namespace': 'clubpenguin'}]})],
                          live={'deployment/cloudflared': False})
        self.assertEqual(verdict['blocked'], [])

    def test_adopted_connector_with_image_update_is_a_normal_change(self):
        _, verdict = gate([deployment('kubernetes_deployment_v1.cloudflared[0]', 'latest', '2026.10.7', 'cloudflared')],
                          live=self.LIVE)
        self.assertEqual(verdict['blocked'], [])


class RollbackScopeTests(unittest.TestCase):
    def test_images_only_accepts_tag_changes_and_release_info(self):
        _, verdict = gate([deployment('kubernetes_deployment_v1.web[0]', 'a', 'b', 'web'),
                           resource('kubernetes_config_map_v1.release[0]', ['update'])], expect='images-only')
        self.assertEqual(verdict['blocked'], [])

    def test_images_only_rejects_other_attribute_changes(self):
        change = deployment('kubernetes_deployment_v1.web[0]', 'a', 'a', 'web', extra_after={'spec2': 1})
        _, verdict = gate([change], expect='images-only')
        self.assertTrue(verdict['blocked'])


class NoSecretsTests(unittest.TestCase):
    def test_rendering_and_summary_never_include_attribute_values(self):
        after = {'metadata': [{'namespace': 'clubpenguin'}], 'data': {'POSTGRES_PASSWORD': SECRET}}
        before = {'metadata': [{'namespace': 'clubpenguin'}], 'data': {'POSTGRES_PASSWORD': 'old-' + SECRET}}
        changes, verdict = gate([resource('kubernetes_secret_v1.postgres[0]', ['update'], before, after)])
        output = plan_gate.render(changes, verdict) + json.dumps(changes) + json.dumps(verdict)
        self.assertNotIn(SECRET, output)
        self.assertIn('data.POSTGRES_PASSWORD', output, 'the changed path is reported, not its value')


if __name__ == '__main__':
    unittest.main()

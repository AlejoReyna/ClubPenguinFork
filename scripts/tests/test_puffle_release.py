import unittest

import deploy


class PuffleReleaseTests(unittest.TestCase):
    def test_legacy_rollback_disables_capability_and_retains_stopped_broker(self):
        cfg = {'CP_PUFFLE_ENABLED': 'true', 'CP_PUFFLE_BROKER_REPLICAS': '1'}
        self.assertEqual(deploy.puffle_settings({}, cfg),
                         {'puffle_enabled': False, 'puffle_broker_replicas': 0})

    def test_install_keeps_service_ready_without_enabling_conversation(self):
        cfg = {'CP_PUFFLE_ENABLED': 'false', 'CP_PUFFLE_BROKER_REPLICAS': '1'}
        self.assertEqual(deploy.puffle_settings({'features': {'puffle': True}}, cfg),
                         {'puffle_enabled': False, 'puffle_broker_replicas': 1})

    def test_enabled_release_requires_explicit_operator_flag(self):
        cfg = {'CP_PUFFLE_ENABLED': 'true', 'CP_PUFFLE_BROKER_REPLICAS': '1'}
        self.assertEqual(deploy.puffle_settings({'features': {'puffle': True}}, cfg),
                         {'puffle_enabled': True, 'puffle_broker_replicas': 1})

    def test_unrecognized_feature_marker_does_not_activate_legacy_images(self):
        cfg = {'CP_PUFFLE_ENABLED': 'true', 'CP_PUFFLE_BROKER_REPLICAS': '1'}
        self.assertEqual(deploy.puffle_settings({'features': {'puffle': 'true'}}, cfg),
                         {'puffle_enabled': False, 'puffle_broker_replicas': 0})

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy  # noqa: E402

MANIFEST = {'components': {'web': {'tag': 'web-new', 'registry_digest': 'sha256:new'}}}
EXPECTED = {'web': {'web': {'tag': 'web-new', 'digest': 'sha256:new'}}}


def pod(tag, digest, ready=True, terminating=False):
    metadata = {'name': 'web-' + tag, 'labels': {'app': 'web'}}
    if terminating:
        metadata['deletionTimestamp'] = '2026-11-10T03:03:20Z'
    return {'metadata': metadata, 'status': {'containerStatuses': [
        {'name': 'web', 'image': f'registry/clubpenguin/web:{tag}', 'imageID': f'registry/clubpenguin/web@{digest}', 'ready': ready}]}}


class FakeKubectl:
    def __init__(self, pods):
        self.stdout = json.dumps({'items': pods})


def run_check(pods):
    with patch.object(deploy, 'kubectl', return_value=FakeKubectl(pods)), \
         patch.object(deploy, 'expected_images', return_value=EXPECTED):
        return deploy.check_images({}, MANIFEST)


class CheckImagesTests(unittest.TestCase):
    def test_matching_pod_passes(self):
        self.assertIn('1 containers match', run_check([pod('web-new', 'sha256:new')]))

    def test_old_pod_still_terminating_after_the_rollout_is_ignored(self):
        # Regression: the run that shipped the ES/EN switch applied correctly, then failed here because the
        # replaced web pod was still Terminating with the old tag.
        pods = [pod('web-old', 'sha256:old', terminating=True), pod('web-new', 'sha256:new')]
        self.assertIn('1 containers match', run_check(pods))

    def test_a_live_pod_with_the_old_tag_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'runs tag web-old, expected web-new'):
            run_check([pod('web-old', 'sha256:old'), pod('web-new', 'sha256:new')])

    def test_a_digest_mismatch_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'digest differs'):
            run_check([pod('web-new', 'sha256:other')])

    def test_only_terminating_pods_means_the_container_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, 'containers not found'):
            run_check([pod('web-new', 'sha256:new', terminating=True)])


if __name__ == '__main__':
    unittest.main()

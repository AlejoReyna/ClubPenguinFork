import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def make_repo(temp):
    """A minimal Git repo with the files the fingerprints read."""
    repo = Path(temp) / 'repo'
    for relative in ('containers/runtime', 'containers/database', 'containers/web', 'scripts'):
        (repo / relative).mkdir(parents=True)
    (repo / 'containers/runtime/Dockerfile').write_text('FROM python\n')
    (repo / 'containers/runtime/run.py').write_text('print(1)\n')
    (repo / 'containers/database/Dockerfile').write_text('FROM postgres\n')
    (repo / 'containers/web/Dockerfile').write_text('FROM nginx\n')
    (repo / 'containers/web/index.html').write_text('<html>1</html>\n')
    shutil.copy(ROOT / 'scripts/fetch-sources.py', repo / 'scripts/fetch-sources.py')
    (repo / 'sources.json').write_text(json.dumps({
        'houdini': {'url': 'u', 'revision': 'a' * 40}, 'dash': {'url': 'u', 'revision': 'b' * 40},
        'legacy-media': {'url': 'u', 'revision': 'c' * 40}}))
    for command in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'x']):
        subprocess.run(['git', '-C', str(repo)] + command, check=True, capture_output=True)
    return repo


def tags(repo):
    return {name: release.tag_for(name, repo) for name in ('runtime', 'database', 'web')}


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def commit(self):
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'y'],
                       check=True, capture_output=True)

    def test_tags_are_stable_and_never_latest(self):
        first = tags(self.repo)
        self.assertEqual(first, tags(self.repo))
        for tag in first.values():
            self.assertRegex(tag, r'^(rt|db|web)-[0-9a-f]{12}$')
            self.assertNotEqual(tag, 'latest')

    def test_frontend_change_leaves_database_and_runtime_untouched(self):
        before = tags(self.repo)
        (self.repo / 'containers/web/index.html').write_text('<html>2</html>\n')
        self.commit()
        after = tags(self.repo)
        self.assertNotEqual(before['web'], after['web'])
        self.assertEqual(before['database'], after['database'], 'a web-only change must not restart PostgreSQL')
        self.assertEqual(before['runtime'], after['runtime'])

    def test_runtime_change_does_not_change_database(self):
        before = tags(self.repo)
        (self.repo / 'containers/runtime/run.py').write_text('print(2)\n')
        self.commit()
        after = tags(self.repo)
        self.assertNotEqual(before['runtime'], after['runtime'])
        self.assertEqual(before['database'], after['database'])
        self.assertEqual(before['web'], after['web'])

    def test_shared_upstream_pin_changes_every_component_that_uses_it(self):
        before = tags(self.repo)
        sources = json.loads((self.repo / 'sources.json').read_text())
        sources['houdini']['revision'] = 'd' * 40
        (self.repo / 'sources.json').write_text(json.dumps(sources))
        self.commit()
        after = tags(self.repo)
        self.assertNotEqual(before['runtime'], after['runtime'])
        self.assertNotEqual(before['database'], after['database'])
        self.assertEqual(before['web'], after['web'], 'houdini does not feed the web image')

    def test_dash_pin_does_not_restart_the_database(self):
        before = tags(self.repo)
        sources = json.loads((self.repo / 'sources.json').read_text())
        sources['dash']['revision'] = 'e' * 40
        (self.repo / 'sources.json').write_text(json.dumps(sources))
        self.commit()
        self.assertEqual(before['database'], tags(self.repo)['database'])

    def test_media_pin_only_affects_web(self):
        before = tags(self.repo)
        sources = json.loads((self.repo / 'sources.json').read_text())
        sources['legacy-media']['revision'] = 'f' * 40
        (self.repo / 'sources.json').write_text(json.dumps(sources))
        self.commit()
        after = tags(self.repo)
        self.assertNotEqual(before['web'], after['web'])
        self.assertEqual((before['runtime'], before['database']), (after['runtime'], after['database']))

    def test_salt_forces_new_tags(self):
        self.assertNotEqual(release.tag_for('database', self.repo), release.tag_for('database', self.repo, salt='base-refresh'))

    def test_puffle_sources_change_runtime_and_web_but_preserve_database(self):
        before = tags(self.repo)
        path = self.repo / 'production/puffle'
        path.mkdir(parents=True)
        (path / 'world.py').write_text('production version 1')
        self.commit()
        after = tags(self.repo)
        self.assertNotEqual(before['runtime'], after['runtime'])
        self.assertNotEqual(before['web'], after['web'])
        self.assertEqual(before['database'], after['database'])

    def test_untracked_bots_component_is_not_part_of_the_release(self):
        (self.repo / 'containers/bots').mkdir()
        (self.repo / 'containers/bots/Dockerfile').write_text('FROM python\n')
        self.assertNotIn('bots', release.enabled_components(self.repo))
        self.commit()
        self.assertIn('bots', release.enabled_components(self.repo))


class PinAndTfvarsTests(unittest.TestCase):
    def test_registry_missing_tag_is_buildable_but_auth_failure_is_not(self):
        with patch.object(release, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'the specified tag does not exist')):
            self.assertIsNone(release.registry_tag('registry', 'runtime', 'new-tag'))
        with patch.object(release, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'authentication failed')):
            with self.assertRaises(SystemExit):
                release.registry_tag('registry', 'runtime', 'new-tag')

    def test_pins_accept_component_equals_tag_only(self):
        self.assertEqual(release.parse_pins(['database=lab-002']), {'database': 'lab-002'})
        for bad in ('database=latest', 'nope=lab-1', 'database', 'web=bad tag', 'web='):
            with self.assertRaises(SystemExit, msg=bad):
                release.parse_pins([bad])

    def manifest(self, **tag_overrides):
        comps = {n: {'tag': f'{n}-t', 'digest': 'sha256:' + n} for n in ('runtime', 'database', 'web')}
        for name, tag in tag_overrides.items():
            comps[name]['tag'] = tag
        return {'commit': 'a' * 40, 'created_utc': '20261103T000000Z', 'run': {'build_id': '7'}, 'components': comps}

    def test_manifest_becomes_per_component_variables(self):
        values = release.manifest_to_tfvars(self.manifest(), 'h')
        self.assertEqual(values['database_image_tag'], 'database-t')
        self.assertEqual(values['release_info']['commit'], 'a' * 40)
        self.assertNotIn('image_tag', values)

    def test_manifest_with_latest_is_refused(self):
        with self.assertRaises(SystemExit):
            release.manifest_to_tfvars(self.manifest(web='latest'), 'h')

    def test_bot_image_is_passed_to_terraform_and_validated(self):
        manifest = self.manifest()
        manifest['components']['bots'] = {'tag': 'bots-123'}
        self.assertEqual(release.manifest_to_tfvars(manifest, 'h')['bots_image_tag'], 'bots-123')
        manifest['components']['bots']['tag'] = 'latest'
        with self.assertRaises(SystemExit):
            release.manifest_to_tfvars(manifest, 'h')

    def test_blob_names_are_validated_before_download(self):
        for bad in ('../x.json', 'releases/x.json', 'postgres/20261103T000000Z.dump'):
            with self.assertRaises(SystemExit):
                release.fetch_manifest({'CP_STATE_STORAGE_ACCOUNT': 'a', 'CP_BACKUP_CONTAINER': 'c'}, bad, '/tmp/never')


if __name__ == '__main__':
    unittest.main()

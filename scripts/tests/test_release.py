import argparse
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
        'vanilla-media': {'url': 'u', 'revision': 'c' * 40}}))
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
        sources['vanilla-media']['revision'] = 'f' * 40
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


class SourcesToFetchTests(unittest.TestCase):
    def test_a_web_only_release_still_fetches_what_the_isolated_tests_read(self):
        # Regression: web-only builds fetched just media/ruffle, and the bot tests then
        # failed on a clean workspace because vendor/houdini was missing.
        sources = release.sources_to_fetch(['web'])
        self.assertTrue({'houdini', 'dash', 'vanilla-media', 'ruffle'} <= set(sources))

    def test_a_bots_only_release_fetches_the_runtime_sources_for_its_tests(self):
        self.assertEqual(release.sources_to_fetch(['bots']), ['dash', 'houdini'])

    def test_stub_web_adds_ruffle_without_vanilla_media(self):
        self.assertEqual(release.sources_to_fetch(['bots'], stub_web=True), ['dash', 'houdini', 'ruffle'])

    def test_result_is_sorted_and_without_duplicates(self):
        sources = release.sources_to_fetch(['runtime', 'database', 'web'])
        self.assertEqual(sources, sorted(set(sources)))


class LocalImageMatchesTests(unittest.TestCase):
    def test_matches_by_id_or_repo_digest_for_matching_non_stub_platform(self):
        digest = 'sha256:' + 'a' * 64
        by_id = json.dumps({'Os': 'linux', 'Architecture': 'arm64', 'Id': digest,
                            'RepoDigests': [], 'Config': {'Labels': {}}})
        with patch.object(release, 'run', return_value=subprocess.CompletedProcess([], 0, by_id, '')):
            self.assertTrue(release.local_image_matches('clubpenguin/runtime:rt-1', digest, 'linux/arm64'))
        by_repo = json.dumps({'Os': 'linux', 'Architecture': 'arm64', 'Id': 'sha256:' + 'b' * 64,
                              'RepoDigests': [f'acr.azurecr.io/clubpenguin/runtime@{digest}'],
                              'Config': {'Labels': None}})
        with patch.object(release, 'run', return_value=subprocess.CompletedProcess([], 0, by_repo, '')):
            self.assertTrue(release.local_image_matches('clubpenguin/runtime:rt-1', digest, 'linux/arm64'))

    def test_rejects_wrong_platform_mismatch_stub_or_missing(self):
        digest = 'sha256:' + 'a' * 64
        self.assertFalse(release.local_image_matches('clubpenguin/web:web-1', '', 'linux/arm64'))
        cases = [
            subprocess.CompletedProcess([], 1, '', 'No such image'),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, 'not-json', ''),
            subprocess.CompletedProcess([], 0, json.dumps({
                'Os': 'linux', 'Architecture': 'amd64', 'Id': digest, 'RepoDigests': [], 'Config': {'Labels': {}},
            }), ''),
            subprocess.CompletedProcess([], 0, json.dumps({
                'Os': 'linux', 'Architecture': 'arm64', 'Id': digest, 'RepoDigests': [],
                'Config': {'Labels': {'clubpenguin.test-only': 'stub-media'}},
            }), ''),
            subprocess.CompletedProcess([], 0, json.dumps({
                'Os': 'linux', 'Architecture': 'arm64', 'Id': 'sha256:' + 'c' * 64,
                'RepoDigests': ['acr.azurecr.io/clubpenguin/web@sha256:' + 'd' * 64],
                'Config': {'Labels': {}},
            }), ''),
        ]
        for result in cases:
            with patch.object(release, 'run', return_value=result):
                self.assertFalse(release.local_image_matches('clubpenguin/web:web-1', digest, 'linux/arm64'))


class BuildReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.release_path = Path(self.temp.name) / 'release.json'

    def tearDown(self):
        self.temp.cleanup()

    def write_release(self, *, web_source='computed'):
        data = {
            'schema': 1,
            'commit': 'a' * 40,
            'dirty': False,
            'components': {
                'runtime': {
                    'tag': 'rt-111111111111', 'source': 'computed', 'needs_build': False,
                    'registry_digest': 'sha256:' + '1' * 64,
                },
                'database': {
                    'tag': 'lab-002', 'source': 'pinned', 'needs_build': False,
                    'registry_digest': 'sha256:' + '2' * 64,
                },
                'web': {
                    'tag': 'web-333333333333', 'source': web_source, 'needs_build': False,
                    'registry_digest': 'sha256:' + '3' * 64,
                },
                'bots': {
                    'tag': 'bots-444444444444', 'source': 'computed', 'needs_build': True,
                    'registry_digest': None,
                },
            },
        }
        self.release_path.write_text(json.dumps(data))
        return argparse.Namespace(release=str(self.release_path), platform='linux/arm64')

    def run_build_with(self, args, cached_refs):
        run_calls = []
        visible_calls = []

        def fake_run(command, **kwargs):
            cmd = [str(part) for part in command]
            run_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='ok\n', stderr='')

        def fake_visible(command, timeout):
            visible_calls.append([str(part) for part in command])

        def fake_matches(image, digest, platform='linux/arm64'):
            return image in cached_refs

        with patch.object(release, 'local_image_matches', side_effect=fake_matches), \
                patch.object(release.lablib, 'load_config', return_value={'CP_ACR_NAME': 'acrtest'}) as mock_cfg, \
                patch.object(release, 'acr_credentials', return_value=('acrtest.azurecr.io', 'tok')) as mock_acr, \
                patch.object(release, 'isolated_docker_config') as mock_iso, \
                patch.object(release, 'run', side_effect=fake_run), \
                patch.object(release, 'run_visible', side_effect=fake_visible), \
                patch.object(release, 'require_arm64'), \
                patch.object(release, 'local_platform', return_value='linux/arm64'):
            release.build(args)
        return run_calls, visible_calls, mock_cfg, mock_acr, mock_iso

    def test_bots_only_build_uses_local_runtime_db_and_ephemeral_stub_web(self):
        args = self.write_release(web_source='computed')
        cached = {'clubpenguin/runtime:rt-111111111111', 'clubpenguin/database:lab-002'}
        run_calls, visible_calls, mock_cfg, mock_acr, _ = self.run_build_with(args, cached)

        mock_cfg.assert_not_called()
        mock_acr.assert_not_called()
        self.assertFalse(any(len(c) > 2 and c[2] == 'pull' for c in visible_calls))

        fetch_cmd = next(c for c in run_calls if any(p.endswith('fetch-sources.py') for p in c))
        self.assertIn('dash,houdini,ruffle', fetch_cmd)
        self.assertNotIn('vanilla-media', ','.join(fetch_cmd))

        build_cmds = [c for c in visible_calls if any(p.endswith('build-images.py') for p in c)]
        self.assertEqual(len(build_cmds), 2)
        self.assertIn('bots', build_cmds[0])
        self.assertIn('--web-media', build_cmds[1])
        self.assertIn('stub', build_cmds[1])
        self.assertIn('web=web-333333333333-test-stub', build_cmds[1])

        test_cmd = next(c for c in run_calls if any(p.endswith('test-containers.py') for p in c))
        idx = test_cmd.index('--web-tag')
        self.assertEqual(test_cmd[idx + 1], 'web-333333333333-test-stub')

        self.assertIn(['docker', 'rmi', 'clubpenguin/web:web-333333333333-test-stub'], run_calls)

    def test_ephemeral_stub_web_image_is_removed_even_when_tests_fail(self):
        args = self.write_release(web_source='computed')
        cached = {'clubpenguin/runtime:rt-111111111111', 'clubpenguin/database:lab-002'}
        run_calls = []

        def fake_run(command, **kwargs):
            cmd = [str(part) for part in command]
            run_calls.append(cmd)
            if any(part.endswith('test-containers.py') for part in cmd):
                raise SystemExit(1)
            return subprocess.CompletedProcess(cmd, 0, stdout='ok\n', stderr='')

        with patch.object(release, 'local_image_matches', side_effect=lambda img, *_a: img in cached), \
                patch.object(release, 'acr_credentials') as mock_acr, \
                patch.object(release, 'run', side_effect=fake_run), \
                patch.object(release, 'run_visible'), \
                patch.object(release, 'require_arm64'), \
                patch.object(release, 'local_platform', return_value='linux/arm64'):
            with self.assertRaises(SystemExit):
                release.build(args)

        mock_acr.assert_not_called()
        self.assertIn(['docker', 'rmi', 'clubpenguin/web:web-333333333333-test-stub'], run_calls)

    def test_cached_reused_web_skips_stub_and_pull(self):
        args = self.write_release(web_source='computed')
        cached = {
            'clubpenguin/runtime:rt-111111111111',
            'clubpenguin/database:lab-002',
            'clubpenguin/web:web-333333333333',
        }
        run_calls, visible_calls, _, mock_acr, _ = self.run_build_with(args, cached)

        mock_acr.assert_not_called()
        build_cmds = [c for c in visible_calls if any(p.endswith('build-images.py') for p in c)]
        self.assertEqual(len(build_cmds), 1)
        self.assertNotIn('--web-media', build_cmds[0])
        test_cmd = next(c for c in run_calls if any(p.endswith('test-containers.py') for p in c))
        idx = test_cmd.index('--web-tag')
        self.assertEqual(test_cmd[idx + 1], 'web-333333333333')
        self.assertFalse(any(c[:2] == ['docker', 'rmi'] for c in run_calls))

    def test_pinned_reused_web_absent_locally_is_pulled_from_registry(self):
        args = self.write_release(web_source='pinned')
        cached = {'clubpenguin/runtime:rt-111111111111', 'clubpenguin/database:lab-002'}
        run_calls, visible_calls, _, mock_acr, mock_iso = self.run_build_with(args, cached)

        mock_acr.assert_called_once_with('acrtest')
        mock_iso.assert_called_once()
        pull_cmd = next(c for c in visible_calls if 'pull' in c)
        self.assertIn(f'acrtest.azurecr.io/clubpenguin/web@sha256:{"3" * 64}', pull_cmd)
        self.assertIn('linux/arm64', pull_cmd)
        build_cmds = [c for c in visible_calls if any(p.endswith('build-images.py') for p in c)]
        self.assertEqual(len(build_cmds), 1)
        test_cmd = next(c for c in run_calls if any(p.endswith('test-containers.py') for p in c))
        idx = test_cmd.index('--web-tag')
        self.assertEqual(test_cmd[idx + 1], 'web-333333333333')
        self.assertFalse(any(c[:2] == ['docker', 'rmi'] for c in run_calls))

    def test_missing_reused_runtime_is_pulled_and_tagged(self):
        args = self.write_release(web_source='computed')
        cached = {'clubpenguin/database:lab-002', 'clubpenguin/web:web-333333333333'}
        run_calls, visible_calls, _, mock_acr, _ = self.run_build_with(args, cached)

        mock_acr.assert_called_once_with('acrtest')
        pull_cmd = next(c for c in visible_calls if 'pull' in c)
        origin = f'acrtest.azurecr.io/clubpenguin/runtime@sha256:{"1" * 64}'
        self.assertIn(origin, pull_cmd)
        tag_cmd = next(c for c in run_calls if 'tag' in c and origin in c)
        self.assertEqual(tag_cmd[-1], 'clubpenguin/runtime:rt-111111111111')


if __name__ == '__main__':
    unittest.main()

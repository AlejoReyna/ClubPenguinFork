import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('fetch_sources', Path(__file__).resolve().parents[1] / 'fetch-sources.py')
fetch_sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_sources)


class FetchSourcesTests(unittest.TestCase):
    def test_retries_partial_clone_and_reuses_verified_checkout_through_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / 'upstream'
            upstream.mkdir()
            fetch_sources.git('-C', str(upstream), 'init', '-q')
            data = upstream / 'media.txt'
            pinned = self.commit(upstream, data, 'pinned')
            self.commit(upstream, data, 'newer')

            vendor = root / 'vendor'
            vendor.mkdir()
            cache = root / 'cache'
            (vendor / 'vanilla-media').symlink_to(cache, target_is_directory=True)
            source = {'url': upstream.as_uri(), 'revision': pinned}
            git = fetch_sources.git
            clone_paths = []

            def fail_first_clone(*args, **kwargs):
                if args[0] == 'clone':
                    target = Path(args[-1])
                    clone_paths.append(target)
                    if len(clone_paths) == 1:
                        target.mkdir()
                        raise subprocess.CalledProcessError(128, 'git clone')
                return git(*args, **kwargs)

            with patch.object(fetch_sources, 'git', side_effect=fail_first_clone), patch.object(fetch_sources.time, 'sleep'):
                fetch_sources.ensure_source('vanilla-media', source, vendor)
                fetch_sources.ensure_source('vanilla-media', source, vendor)

            self.assertEqual(len(clone_paths), 2)
            self.assertTrue(all(not path.parent.exists() for path in clone_paths))
            self.assertEqual((cache / data.name).read_text(), 'pinned')
            (cache / data.name).write_text('modified')
            with self.assertRaisesRegex(SystemExit, 'tracked files differ'):
                fetch_sources.ensure_source('vanilla-media', source, vendor)
            self.assertEqual((cache / data.name).read_text(), 'modified')

    @staticmethod
    def commit(repo, file, content):
        file.write_text(content)
        fetch_sources.git('-C', str(repo), 'add', file.name)
        fetch_sources.git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                          'commit', '-qm', content)
        return fetch_sources.revision(repo)

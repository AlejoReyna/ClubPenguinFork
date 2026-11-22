import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('build_images', ROOT / 'scripts/build-images.py')
build_images = importlib.util.module_from_spec(SPEC)
sys.modules['build_images'] = build_images
SPEC.loader.exec_module(build_images)

PUFFLE_SPEC = importlib.util.spec_from_file_location('build_puffle_client', ROOT / 'scripts/build-puffle-client.py')
build_puffle_client = importlib.util.module_from_spec(PUFFLE_SPEC)
sys.modules['build_puffle_client'] = build_puffle_client
PUFFLE_SPEC.loader.exec_module(build_puffle_client)


class CopyMediaTreeTests(unittest.TestCase):
    def test_copy_keeps_symlinks_and_leaves_the_source_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / 'src'
            (src / 'media').mkdir(parents=True)
            (src / 'media' / 'a.txt').write_text('a')
            (src / 'media' / 'link.txt').symlink_to('a.txt')
            dest = Path(temp) / 'nested' / 'dest'
            build_images.copy_media_tree(src, dest)
            self.assertEqual((dest / 'media' / 'a.txt').read_text(), 'a')
            self.assertTrue((dest / 'media' / 'link.txt').is_symlink())
            self.assertEqual((dest / 'media' / 'link.txt').read_text(), 'a')
            (dest / 'media' / 'a.txt').write_text('patched')
            self.assertEqual((src / 'media' / 'a.txt').read_text(), 'a')

    def test_build_puffle_client_patches_engine_and_records_patched_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            vendor_client = temp_path / 'vendor/vanilla-media/media/play/v2/client'
            vendor_client.mkdir(parents=True)
            (vendor_client / 'engine.swf').write_bytes(b'original-engine')
            (vendor_client / 'login.swf').write_bytes(b'login-bytes')
            (vendor_client / 'create.swf').write_bytes(b'create-bytes')
            fake_jar = temp_path / 'ffdec.jar'
            fake_jar.write_bytes(b'fake-ffdec')
            output = temp_path / 'out'

            def fake_run(cmd, **_kwargs):
                out_idx = cmd.index('--output') + 1
                Path(cmd[out_idx]).write_bytes(b'patched-glaciar-engine')
                return subprocess.CompletedProcess(cmd, 0)

            with mock.patch.object(build_puffle_client, 'ROOT', temp_path), \
                    mock.patch.object(build_puffle_client, 'ensure_ffdec', return_value=fake_jar) as mock_ffdec, \
                    mock.patch.object(build_puffle_client.subprocess, 'run', side_effect=fake_run) as mock_run, \
                    mock.patch.object(sys, 'argv', ['build-puffle-client.py', '--output', str(output)]):
                (temp_path / 'production/puffle').mkdir(parents=True)
                (temp_path / 'experiments/puffle_sim/assets').mkdir(parents=True)
                build_puffle_client.main()

            mock_ffdec.assert_called_once_with(temp_path)
            mock_run.assert_called_once()
            self.assertEqual((output / 'engine.swf').read_bytes(), b'patched-glaciar-engine')
            build_json = json.loads((output / 'build.json').read_text())
            self.assertEqual(
                build_json['engine.swf'],
                hashlib.sha256(b'patched-glaciar-engine').hexdigest(),
            )


if __name__ == '__main__':
    unittest.main()


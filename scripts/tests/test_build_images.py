import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('build_images', ROOT / 'scripts/build-images.py')
build_images = importlib.util.module_from_spec(SPEC)
sys.modules['build_images'] = build_images
SPEC.loader.exec_module(build_images)


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


if __name__ == '__main__':
    unittest.main()

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    'lab_overlay_patch', ROOT / 'containers/runtime/lab_overlay_patch.py')
patch = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = patch
_spec.loader.exec_module(patch)

HOUDINI_REV = '36eaa5cbd342d743c40a8c47fe140abc710eb069'
DASH_REV = '89ccac34883088f176c594ae55e0e93a29dac265'


class LabOverlayPatchTest(unittest.TestCase):
    def test_starting_coins_on_pinned_sources(self):
        # The same pinned bytes are already vendored; the bot suite runs with
        # --network none and must never download fixtures during a test.
        houdini_penguin = (ROOT / 'vendor/houdini/houdini/data/penguin.py').read_bytes()
        legacy = (ROOT / 'vendor/dash/dash/routes/create/legacy.py').read_bytes()
        self.assertEqual(hashlib.sha256(houdini_penguin).hexdigest(), patch.HOUDINI_FILES['data/penguin.py'])
        self.assertEqual(hashlib.sha256(legacy).hexdigest(), patch.DASH_FILES['routes/create/legacy.py'])

        houdini_out = patch.transform_houdini('data/penguin.py', houdini_penguin)
        self.assertIn('server_default=db.text("10000")', houdini_out)
        self.assertNotIn('server_default=db.text("500")', houdini_out)

        dash_out = patch.transform_dash('routes/create/legacy.py', legacy)
        self.assertIn('coins=app.config.STARTING_COINS', dash_out)
        self.assertIn("request.form.get('nickname', '')", dash_out)
        self.assertIn('nickname=display_name', dash_out)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data').mkdir()
            (root / 'routes/create').mkdir(parents=True)
            (root / 'data/penguin.py').write_bytes(houdini_penguin)
            (root / 'routes/create/legacy.py').write_bytes(legacy)
            patch.apply_tree(root, patch.HOUDINI_FILES, patch.transform_houdini)
            patch.apply_tree(root, patch.DASH_FILES, patch.transform_dash)
            self.assertIn('10000', (root / 'data/penguin.py').read_text())
            self.assertIn('STARTING_COINS', (root / 'routes/create/legacy.py').read_text())


if __name__ == '__main__':
    unittest.main()

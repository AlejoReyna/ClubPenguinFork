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


def _schema_text(name):
    return (ROOT / name).read_text()


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
        self.assertIn('grant_for_penguin(db, penguin.id, puffle_name)', dash_out)
        self.assertIn('async with db.transaction():', dash_out)
        self.assertIn("request.form.get('puffle_name', '')", dash_out)
        self.assertIn('normalize_welcome_puffle_name', dash_out)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data').mkdir()
            (root / 'routes/create').mkdir(parents=True)
            (root / 'data/penguin.py').write_bytes(houdini_penguin)
            (root / 'routes/create/legacy.py').write_bytes(legacy)
            patch.apply_tree(root, {'data/penguin.py': patch.HOUDINI_FILES['data/penguin.py']},
                             patch.transform_houdini)
            patch.apply_tree(root, {'routes/create/legacy.py': patch.DASH_FILES['routes/create/legacy.py']},
                             patch.transform_dash)
            self.assertIn('10000', (root / 'data/penguin.py').read_text())
            self.assertIn('STARTING_COINS', (root / 'routes/create/legacy.py').read_text())
            self.assertIn('grant_for_penguin', (root / 'routes/create/legacy.py').read_text())

    def test_vanilla_create_also_grants_the_welcome_puffle(self):
        vanilla = (ROOT / 'vendor/dash/dash/routes/create/vanilla.py').read_bytes()
        self.assertEqual(hashlib.sha256(vanilla).hexdigest(), patch.DASH_FILES['routes/create/vanilla.py'])
        out = patch.transform_dash('routes/create/vanilla.py', vanilla)
        self.assertIn('from lab_welcome_puffle import grant_for_penguin', out)
        self.assertIn('async with db.transaction():', out)
        self.assertIn('grant_for_penguin(db, penguin.id, puffle_name)', out)
        self.assertIn('nickname=username', out)
        self.assertIn("request.form.get('puffle_name', '')", out)
        self.assertIn('puffle_name = WELCOME_PUFFLE_NAME', out)

    def test_pet_handlers_protect_the_welcome_instance(self):
        pet = (ROOT / 'vendor/houdini/houdini/handlers/play/pet.py').read_bytes()
        self.assertEqual(hashlib.sha256(pet).hexdigest(), patch.HOUDINI_FILES['handlers/play/pet.py'])
        out = patch.transform_houdini('handlers/play/pet.py', pet)
        self.assertIn('from houdini.lab_welcome_puffle import is_protected_puffle_id', out)
        return_at = out.index('async def handle_return_puffle')
        walking_at = out.index('if p.walking == puffle.id:', return_at)
        protect_at = out.index('if await is_protected_puffle_id(db, puffle.id):', return_at)
        self.assertLess(protect_at, walking_at)
        self.assertIn('if not await is_protected_puffle_id(db, puffle.id):', out)
        self.assertIn('await penguin.puffles.delete(puffle.id)', out)
        rename_at = out.index('async def handle_rename_puffle')
        check_at = out.index('async def handle_check_puffle_name')
        self.assertGreater(rename_at, check_at)
        rename_fn = out[rename_at:out.index('async def handle_get_player_puffles_vanilla')]
        self.assertIn("await puffle.update(name=name.title()).apply()", rename_fn)
        self.assertNotIn('is_protected_puffle_id', rename_fn)

    def test_register_form_keeps_welcome_puffle_name_fixed(self):
        html = (ROOT / 'containers/web/register.html').read_text()
        self.assertIn('Dubstep', html)
        self.assertNotIn('id="puffle"', html)
        self.assertNotIn('puffle_name:', html)

    def test_welcome_schema_is_identical_for_clean_and_existing_installs(self):
        init = _schema_text('containers/database/003-lab-welcome-puffle.sql')
        existing = _schema_text('scripts/lab-welcome-puffle.sql')
        self.assertIn('CREATE TABLE IF NOT EXISTS penguin_welcome_puffle', init)
        self.assertIn('ON DELETE RESTRICT', init)
        self.assertIn('PRIMARY KEY (penguin_id)', init)
        self.assertIn(init[init.index('CREATE TABLE'):], existing)


if __name__ == '__main__':
    unittest.main()

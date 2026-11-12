"""The create patch (experiments/puffle_sim/client/build_create.py) skips the activation
screen only after the password step, and only on the pinned create.swf."""
import importlib.util
from pathlib import Path
import sys
import unittest

CLIENT = Path(__file__).resolve().parents[2] / 'experiments/puffle_sim/client'
sys.path.insert(0, str(CLIENT))
spec = importlib.util.spec_from_file_location('build_create', CLIENT / 'build_create.py')
build_create = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_create)

ORIGINAL = build_create.ADVANCE + build_create.INSERTION


class CreateLoginReturnTests(unittest.TestCase):
    def test_password_step_returns_to_login_and_other_steps_still_advance(self):
        patched = build_create.patch_source(ORIGINAL)
        self.assertEqual(patched.count(build_create.RETURN_TO_LOGIN), 1)
        self.assertIn('this.requestVars.action == VALIDATE_PASSWORD_EMAIL', patched)
        self.assertIn('this.returnToLogin();\n         return undefined;', patched)
        self.assertIn('shell.loadLoginDependencies();', patched)
        self.assertIn('shell.hideLoading();', patched)
        self.assertEqual(patched.count('this.nextView();'), 1)

    def test_refuses_a_different_create_swf(self):
        missing = ORIGINAL.replace('this.nextView();', 'this.showAlmost();')
        with self.assertRaises(SystemExit):
            build_create.patch_source(missing)
        doubled = ORIGINAL + build_create.ADVANCE
        with self.assertRaises(SystemExit):
            build_create.patch_source(doubled)


if __name__ == '__main__':
    unittest.main()

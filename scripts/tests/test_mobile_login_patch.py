"""The phone login patch (experiments/puffle_sim/client/build_login.py) edits the decompiled
ViewManager only where the pinned login.swf has exactly the code it was written for."""
import importlib.util
from pathlib import Path
import sys
import unittest

CLIENT = Path(__file__).resolve().parents[2] / 'experiments/puffle_sim/client'
sys.path.insert(0, str(CLIENT))
spec = importlib.util.spec_from_file_location('build_login', CLIENT / 'build_login.py')
build_login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_login)

# The fragments of the decompiled ViewManager that the patch touches, in their original form.
ORIGINAL = '\n'.join(original for original, _ in build_login.PATCHES) + '\n' + build_login.INSERTION
METHODS = '   function glaciarSkipStart()\n   {\n   }\n'


class MobileLoginPatchTests(unittest.TestCase):
    def test_every_fragment_is_patched_once_and_the_methods_are_added(self):
        patched = build_login.patch_source(ORIGINAL, METHODS)
        for _, replacement in build_login.PATCHES:
            self.assertEqual(patched.count(replacement), 1)
        self.assertIn(METHODS + build_login.INSERTION, patched)

    def test_phone_flow_is_wired_in(self):
        patched = build_login.patch_source(ORIGINAL, METHODS)
        self.assertIn('this.setupGlaciarLoginReport();\n      this.showInitialView();', patched)
        self.assertIn('else if(this.glaciarSkipStart())', patched)
        start = patched.index('function gotoStart()')
        self.assertLess(patched.index('this.glaciarFirstView();', start), patched.index('return undefined;', start))
        self.assertEqual(patched.count('note_mc._visible = !'), 2)

    def test_refuses_a_different_login_swf(self):
        missing = ORIGINAL.replace('this.showView(this._startScreen);', 'this.showStart();')
        with self.assertRaises(SystemExit):
            build_login.patch_source(missing, METHODS)
        doubled = ORIGINAL + build_login.PATCHES[0][0]
        with self.assertRaises(SystemExit):
            build_login.patch_source(doubled, METHODS)

    def test_methods_report_views_only_never_typed_text(self):
        methods = (CLIENT / 'mobile_login.as').read_text(encoding='utf-8')
        for name in ('glaciarSkipStart', 'glaciarFirstView', 'setupGlaciarLoginReport', 'glaciarLoginState'):
            self.assertIn(f'function {name}(', methods)
        self.assertNotIn('.text', methods)
        self.assertNotIn('Password', methods)
        self.assertEqual(methods.count('fscommand('), 2)


if __name__ == '__main__':
    unittest.main()

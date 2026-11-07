"""Build the mobile login.swf copy; never writes to the source SWF.

Phones skip the start banner and the page frames the visible login view (mobile_login.as).
Requires the complete, locally supplied FFDec 26.4.0 distribution. This tool does not
download software or deploy anything.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_engine import FFDEC_SHA256, sha256  # noqa: E402


LOGIN_SHA256 = '297f751d2db35ebb5d88050c37eab2e1868ddc1b7cc9633240c099719b676349'
CLASS_PATH = 'scripts/__Packages/com/clubpenguin/login/views/ViewManager.as'
CLASS_NAME = '/__Packages/com/clubpenguin/login/views/ViewManager'

# Each original fragment must appear exactly once, or the SWF is not the one this was written for.
PATCHES = [
    # The page reports which view is showing from the moment the views exist.
    ('      this.initViews();\n      this.showInitialView();',
     '      this.initViews();\n      this.setupGlaciarLoginReport();\n      this.showInitialView();'),
    # First view on a phone: the form (or the saved penguins), not the banner.
    ('      else\n      {\n         this.showView(this._startScreen);',
     '      else if(this.glaciarSkipStart())\n      {\n         this.glaciarFirstView();\n      }\n'
     '      else\n      {\n         this.showView(this._startScreen);'),
    # "Back", forgetting the last saved penguin, or an empty list never return to the banner.
    ('   function gotoStart()\n   {\n',
     '   function gotoStart()\n   {\n      if(this.glaciarSkipStart())\n      {\n'
     '         this.glaciarFirstView();\n         return undefined;\n      }\n'),
    # With no saved penguins the form is the first view: "Back" would lead nowhere, so hide it.
    # The decorative "keep your password secret" note sits right against the inputs, so a frame
    # that fits the form on a phone would cut it in half: hide it there too.
    ('      _loc2_.setup({});\n      _loc2_.show();',
     '      _loc2_.setup({});\n      _loc2_.show();\n'
     '      var glaciarPhone = this.glaciarSkipStart();\n'
     '      var glaciarBack = !glaciarPhone || com.clubpenguin.login.LocalData.getSavedPlayers().length > 0;\n'
     '      Object(_loc2_).back_btn._visible = glaciarBack;\n'
     '      Object(_loc2_).back_txt._visible = glaciarBack;\n'
     '      Object(_loc2_).note_mc._visible = !glaciarPhone;'),
    ('      _loc2_.setup(obj);\n      _loc2_.show();',
     '      _loc2_.setup(obj);\n      _loc2_.show();\n'
     '      Object(_loc2_).note_mc._visible = !this.glaciarSkipStart();'),
]
INSERTION = '   function getQueryParams(queryString)\n'


def patch_source(source, methods):
    for original, _ in PATCHES + [(INSERTION, None)]:
        if source.count(original) != 1:
            raise SystemExit('Unexpected script structure; refusing to patch')
    for original, replacement in PATCHES:
        source = source.replace(original, replacement, 1)
    return source.replace(INSERTION, methods + INSERTION, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffdec', required=True, type=Path, help='path to FFDec 26.4.0 ffdec.jar')
    parser.add_argument('--login', required=True, type=Path, help='original login.swf')
    parser.add_argument('--output', required=True, type=Path, help='new SWF path; must not exist')
    args = parser.parse_args()
    if sha256(args.login) != LOGIN_SHA256 or sha256(args.ffdec) != FFDEC_SHA256:
        raise SystemExit('Unexpected source SWF or FFDec jar; refusing to patch')
    if args.output.exists():
        raise SystemExit('Output exists; refusing to overwrite')

    java = ['java', '-Djava.awt.headless=true', '-jar', str(args.ffdec.resolve())]
    methods = Path(__file__).with_name('mobile_login.as').read_text(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='login-swf-') as tmp:
        tmp = Path(tmp)
        subprocess.run(java + ['-export', 'script', str(tmp / 'export'), str(args.login.resolve())],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        source = (tmp / 'export' / CLASS_PATH).read_text(encoding='utf-8')
        modified = tmp / 'ViewManager.as'
        modified.write_text(patch_source(source, methods), encoding='utf-8')
        output = tmp / 'login.swf'
        subprocess.run(java + ['-replace', str(args.login.resolve()), str(output), CLASS_NAME, str(modified)],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        # Exclusive creation protects the original and existing user files.
        with args.output.open('xb') as dest, output.open('rb') as src:
            shutil.copyfileobj(src, dest)
    print('Built the mobile login.swf copy.')


if __name__ == '__main__':
    main()

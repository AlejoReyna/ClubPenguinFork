"""Build the create.swf copy that returns to login after the account exists.

The password step used to open the "almost done, check the parent email" screen.
This build never sends that mail and the penguin is already active, so Continuar
on that step reloads the login form instead. Never writes to the source SWF.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_engine import FFDEC_SHA256, sha256  # noqa: E402


CREATE_SHA256 = 'c1e5a8423b94a6618e1380cee16ec623f27bf3bfea932c0f03ad5cff6caa0b81'
CLASS_PATH = 'scripts/__Packages/com/clubpenguin/web/create/MainView.as'
CLASS_NAME = '/__Packages/com/clubpenguin/web/create/MainView'

# The original fragment must appear exactly once, or the SWF is not the one this was written for.
ADVANCE = '''      if(this.responseVars.sid.length)
      {
         this.sessionID = this.responseVars.sid;
      }
      this.nextView();
      this.trackCurrentView();
'''
ADVANCE_PATCHED = '''      if(this.responseVars.sid.length)
      {
         this.sessionID = this.responseVars.sid;
      }
      if(this.requestVars.action == VALIDATE_PASSWORD_EMAIL)
      {
         this.returnToLogin();
         return undefined;
      }
      this.nextView();
      this.trackCurrentView();
'''
INSERTION = '   function handleResponseVars(loadSuccess)\n'
RETURN_TO_LOGIN = '''   function returnToLogin()
   {
      var shell = this.shell;
      var done = shell.onLoginDependencyLoaderComplete;
      shell.onLoginDependencyLoaderComplete = function()
      {
         done.call(shell);
         shell.hideLoading();
      };
      shell.login_init = false;
      shell.loadLoginDependencies();
      shell.dependencyHolder.create.removeMovieClip();
   }
'''


def patch_source(source):
    if source.count(ADVANCE) != 1 or source.count(INSERTION) != 1:
        raise SystemExit('Unexpected script structure; refusing to patch')
    source = source.replace(ADVANCE, ADVANCE_PATCHED, 1)
    return source.replace(INSERTION, RETURN_TO_LOGIN + INSERTION, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffdec', required=True, type=Path, help='path to FFDec 26.4.0 ffdec.jar')
    parser.add_argument('--create', required=True, type=Path, help='original create.swf')
    parser.add_argument('--output', required=True, type=Path, help='new SWF path; must not exist')
    args = parser.parse_args()
    if sha256(args.create) != CREATE_SHA256 or sha256(args.ffdec) != FFDEC_SHA256:
        raise SystemExit('Unexpected source SWF or FFDec jar; refusing to patch')
    if args.output.exists():
        raise SystemExit('Output exists; refusing to overwrite')

    java = ['java', '-Djava.awt.headless=true', '-jar', str(args.ffdec.resolve())]
    with tempfile.TemporaryDirectory(prefix='create-swf-') as tmp:
        tmp = Path(tmp)
        subprocess.run(java + ['-export', 'script', str(tmp / 'export'), str(args.create.resolve())],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        source = (tmp / 'export' / CLASS_PATH).read_text(encoding='utf-8')
        modified = tmp / 'MainView.as'
        modified.write_text(patch_source(source), encoding='utf-8')
        output = tmp / 'create.swf'
        subprocess.run(java + ['-replace', str(args.create.resolve()), str(output), CLASS_NAME, str(modified)],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        with args.output.open('xb') as dest, output.open('rb') as src:
            shutil.copyfileobj(src, dest)
    print('Built the create.swf copy that returns to login.')


if __name__ == '__main__':
    main()

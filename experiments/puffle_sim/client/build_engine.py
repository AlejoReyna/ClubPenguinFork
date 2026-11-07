"""Build the patched engine.swf (puffle renderer); never writes to the source SWF.

Requires the complete FFDec 26.4.0 distribution; scripts/build-puffle-client.py
downloads it and verifies it by SHA-256. This tool downloads and deploys nothing.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile


ENGINE_SHA256 = '4f98c5df71002c9ddcaaa9b5b26094d395f6ff94d7c04ac2741f2188f5d56b32'
FFDEC_SHA256 = '80f8ced0bfde98b434c28e9807e91fe5558255b2f712051a0e65221519bd392a'
CLASS_PATH = 'scripts/__Packages/com/clubpenguin/engine/PuffleManager.as'
CLASS_NAME = '/__Packages/com/clubpenguin/engine/PuffleManager'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffdec', required=True, type=Path, help='path to FFDec 26.4.0 ffdec.jar')
    parser.add_argument('--engine', required=True, type=Path, help='original engine.swf')
    parser.add_argument('--output', required=True, type=Path, help='new SWF path; must not exist')
    args = parser.parse_args()
    if sha256(args.engine) != ENGINE_SHA256 or sha256(args.ffdec) != FFDEC_SHA256:
        raise SystemExit('Unexpected source SWF or FFDec jar; refusing to patch')
    if args.output.exists():
        raise SystemExit('Output exists; refusing to overwrite')

    java = ['java', '-Djava.awt.headless=true', '-jar', str(args.ffdec.resolve())]
    constructor = '      this._puffles = [];\n      this._pathEngine'
    registration = '''      this._puffles = [];
      if(this._shell.__puffleTalkHandler != undefined)
      {
         this._shell.AIRTOWER.removeListener("puffletalk",this._shell.__puffleTalkHandler);
      }
      this._shell.__puffleTalkHandler = this.handlePuffleChat;
      this._shell.AIRTOWER.addListener("puffletalk",this.handlePuffleChat,this);
      this.setupMobileCamera();
      this._pathEngine'''
    insertion = '   function setupPathEngine()\n'
    renderer = Path(__file__).with_name('renderer.as').read_text(encoding='utf-8')
    camera = Path(__file__).with_name('mobile_camera.as').read_text(encoding='utf-8')
    camera += Path(__file__).with_name('mobile_dock.as').read_text(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='puffle-swf-') as tmp:
        tmp = Path(tmp)
        subprocess.run(java + ['-export', 'script', str(tmp / 'export'), str(args.engine.resolve())],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        source = (tmp / 'export' / CLASS_PATH).read_text(encoding='utf-8')
        if source.count(constructor) != 1 or source.count(insertion) != 1:
            raise SystemExit('Unexpected script structure; refusing to patch')
        source = source.replace(constructor, registration, 1).replace(insertion, renderer + camera + insertion, 1)
        modified = tmp / 'PuffleManager.as'
        modified.write_text(source, encoding='utf-8')
        output = tmp / 'engine.swf'
        subprocess.run(java + ['-replace', str(args.engine.resolve()), str(output), CLASS_NAME, str(modified)],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        # Exclusive creation protects the original and existing user files.
        with args.output.open('xb') as dest, output.open('rb') as src:
            shutil.copyfileobj(src, dest)
    print('Built an experimental local SWF copy; no visual verification was performed.')


if __name__ == '__main__':
    main()

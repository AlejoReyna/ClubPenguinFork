"""Patch the 2017 engine.swf with the Glaciar camera and puffle chat renderer."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_engine import FFDEC_SHA256, REGISTER_PUFFLE_CHAT, sha256  # noqa: E402

ENGINE_2017_SHA256 = 'c30d75048a7d3e49becef76dd81a683163df36dbabbcc1683e984cbd0ca5d23b'
CLASS_PATH = 'scripts/__Packages/com/clubpenguin/engine/PuffleManager.as'
CLASS_NAME = '/__Packages/com/clubpenguin/engine/PuffleManager'
CONSTRUCTOR_TAIL = (
    '      this._puffles = [];\n'
    '      this._pathEngine = new com.clubpenguin.engine.pathfinding.PathEngine();\n'
    '   }'
)
CONSTRUCTOR_PATCH = (
    '      this._puffles = [];\n'
    '      this._pathEngine = new com.clubpenguin.engine.pathfinding.PathEngine();\n'
    + REGISTER_PUFFLE_CHAT
    + '      this.setupMobileCamera();\n'
    '   }'
)
INSERTION = '   function setupPathEngine()\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffdec', required=True, type=Path)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sha256(args.engine) != ENGINE_2017_SHA256 or sha256(args.ffdec) != FFDEC_SHA256:
        raise SystemExit('Unexpected source SWF or FFDec jar; refusing to patch')
    if args.output.exists():
        raise SystemExit('Output exists; refusing to overwrite')

    camera = Path(__file__).with_name('mobile_camera_glaciar.as').read_text(encoding='utf-8')
    renderer = Path(__file__).with_name('renderer.as').read_text(encoding='utf-8')
    java = ['java', '-Djava.awt.headless=true', '-jar', str(args.ffdec.resolve())]
    with tempfile.TemporaryDirectory(prefix='glaciar-engine-') as tmp:
        tmp = Path(tmp)
        subprocess.run(java + ['-export', 'script', str(tmp / 'export'), str(args.engine.resolve())],
                       check=True, stdout=subprocess.DEVNULL, timeout=180)
        source = (tmp / 'export' / CLASS_PATH).read_text(encoding='utf-8')
        if source.count(CONSTRUCTOR_TAIL) != 1 or source.count(INSERTION) != 1:
            raise SystemExit('Unexpected PuffleManager layout; refusing to patch')
        source = source.replace(CONSTRUCTOR_TAIL, CONSTRUCTOR_PATCH, 1)
        source = source.replace(INSERTION, renderer + camera + INSERTION, 1)
        modified = tmp / 'PuffleManager.as'
        modified.write_text(source, encoding='utf-8')
        output = tmp / 'engine.swf'
        subprocess.run(java + ['-replace', str(args.engine.resolve()), str(output), CLASS_NAME, str(modified)],
                       check=True, stdout=subprocess.DEVNULL, timeout=180)
        with args.output.open('xb') as dest, output.open('rb') as src:
            shutil.copyfileobj(src, dest)
    print('Built 2017 engine.swf with glaciarCamera and puffletalk.')


if __name__ == '__main__':
    main()

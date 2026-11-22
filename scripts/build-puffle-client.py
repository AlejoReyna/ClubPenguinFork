#!/usr/bin/env python3
"""Rebuild the pinned SWF and a deterministic companion archive, without credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.puffle_sim.client.build_engine import FFDEC_SHA256  # noqa: E402

FFDEC_URL = 'https://github.com/jindrapetrik/jpexs-decompiler/releases/download/version26.4.0/ffdec_26.4.0.zip'
FFDEC_ZIP_SHA256 = '56ce960aeafc7caf034e2052ba49f878a883f7c7ca690da6eeffca9587b5e4b9'


def ensure_ffdec(root: Path = ROOT) -> Path:
    """Return the verified FFDec 26.4.0 jar path, downloading it if absent."""
    directory = root / '.local/tools/ffdec-26.4.0'
    jar = directory / 'ffdec.jar'
    if not jar.is_file():
        archive = root / '.local/tools/ffdec-26.4.0.zip'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != FFDEC_ZIP_SHA256:
            urllib.request.urlretrieve(FFDEC_URL, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != FFDEC_ZIP_SHA256:
            raise SystemExit('FFDec distribution checksum mismatch')
        directory.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if not (directory / member.filename).resolve().is_relative_to(directory.resolve()):
                    raise SystemExit('Invalid FFDec archive')
            bundle.extractall(directory)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != FFDEC_SHA256:
        raise SystemExit('FFDec checksum mismatch')
    return jar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--client-dir', type=Path,
                        help='directory holding engine.swf, login.swf and create.swf')
    parser.add_argument('--stub', action='store_true', help='test image only; not publishable')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stub:
        (args.output / 'engine.swf').write_bytes(b'test-only placeholder')
        (args.output / 'login.swf').write_bytes(b'test-only placeholder')
        (args.output / 'create.swf').write_bytes(b'test-only placeholder')
    else:
        client = args.client_dir or (ROOT / 'vendor/vanilla-media/media/play/v2/client')
        for name in ('engine.swf', 'login.swf', 'create.swf'):
            target = args.output / name
            if target.exists():
                raise SystemExit(f'{name} already exists; refusing to overwrite')
        if args.client_dir is not None:
            (args.output / 'engine.swf').write_bytes((client / 'engine.swf').read_bytes())
        else:
            jar = ensure_ffdec(ROOT)
            subprocess.run([
                sys.executable,
                str(ROOT / 'experiments/puffle_sim/client/build_engine_glaciar.py'),
                '--ffdec', str(jar),
                '--engine', str(client / 'engine.swf'),
                '--output', str(args.output / 'engine.swf'),
            ], check=True, cwd=ROOT)
        for name in ('login.swf', 'create.swf'):
            target = args.output / name
            target.write_bytes((client / name).read_bytes())
    with zipfile.ZipFile(args.output / 'companion.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        files = list((ROOT / 'production/puffle').glob('*.py'))
        files += [p for p in (ROOT / 'experiments/puffle_sim').glob('*.py') if p.name != 'run_houdini_fixture.py']
        files += list((ROOT / 'experiments/puffle_sim/assets').glob('*.png'))
        for path in sorted(files):
            entry = zipfile.ZipInfo(str(path.relative_to(ROOT)), (2026, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    metadata = {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                for name in ('engine.swf', 'login.swf', 'create.swf', 'companion.zip')}
    (args.output / 'build.json').write_text(json.dumps(metadata, sort_keys=True) + '\n')
    print('Puffle client prepared; companion contains code/assets only.')


if __name__ == '__main__':
    main()


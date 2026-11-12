#!/usr/bin/env python3
"""Rebuild the pinned SWF and a deterministic companion archive, without credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--stub', action='store_true', help='test image only; not publishable')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stub:
        (args.output / 'engine.swf').write_bytes(b'test-only placeholder')
        (args.output / 'login.swf').write_bytes(b'test-only placeholder')
        (args.output / 'create.swf').write_bytes(b'test-only placeholder')
    else:
        client = ROOT / 'vendor/vanilla-media/media/play/v2/client'
        # Hashes in build.json must match the SWFs the image serves. The puffle
        # patches only apply to the older engine; the 2017 client is copied through.
        for name in ('engine.swf', 'login.swf', 'create.swf'):
            target = args.output / name
            if target.exists():
                raise SystemExit(f'{name} already exists; refusing to overwrite')
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

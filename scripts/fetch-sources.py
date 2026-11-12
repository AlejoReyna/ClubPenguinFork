#!/usr/bin/env python3
"""Download the pinned upstream sources and the verified Ruffle release.

--only limits the download (e.g. `--only houdini,dash,ruffle` skips the vanilla
media dump, which backend tests do not need).
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
import zipfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--only', help='comma-separated subset of: houdini,dash,vanilla-media,ruffle')
args = parser.parse_args()
wanted = set(args.only.split(',')) if args.only else None
vendor = root / 'vendor'
vendor.mkdir(exist_ok=True)
sources = json.loads((root / 'sources.json').read_text())
if wanted and not wanted <= set(sources) | {'ruffle'}:
    raise SystemExit('Unknown source in --only')
for name, source in sources.items():
    if wanted is not None and name not in wanted:
        continue
    path = vendor / name
    if not path.exists():
        subprocess.run(['git', 'clone', '--depth', '1', source['url'], str(path)], check=True)
        actual = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != source['revision']:
            subprocess.run(['git', '-C', str(path), 'fetch', '--depth', '1', 'origin', source['revision']], check=True)
            subprocess.run(['git', '-C', str(path), 'checkout', '--detach', source['revision']], check=True)
    actual = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != source['revision']:
        raise SystemExit(f'{name}: existing source revision differs; inspect it before changing it.')
    dirty = subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain', '--untracked-files=no'], text=True)
    if dirty.strip():
        raise SystemExit(f'{name}: tracked files differ from the pinned source.')
    print(f'{name}: {actual}')

if wanted is not None and 'ruffle' not in wanted:
    raise SystemExit(0)
archive = root / '.local/ruffle-0.7.1.zip'
archive.parent.mkdir(mode=0o700, exist_ok=True)
if not archive.exists():
    urllib.request.urlretrieve('https://github.com/ruffle-rs/ruffle/releases/download/v0.7.1/ruffle-0.7.1-web-selfhosted.zip', archive)
expected = 'ef12edbee135d581a9cd523837d746da30a07a64dbf1cf472d2e472c656f4b5e'
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit('Ruffle archive checksum differs from the pinned release.')
destination = vendor / 'ruffle'
destination.mkdir(exist_ok=True)
with zipfile.ZipFile(archive) as bundle:
    for member in bundle.infolist():
        if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
            raise SystemExit('Invalid archive path')
    bundle.extractall(destination)
print('Ruffle 0.7.1: checksum verified')

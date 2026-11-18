#!/usr/bin/env python3
"""Download the pinned upstream sources and the verified Ruffle release.

--only limits the download (e.g. `--only houdini,dash,ruffle` skips the vanilla
media dump, which backend tests do not need).

Stalled downloads are retried. A new Git checkout or archive is only saved
after its pin has been verified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BACKOFF_SECONDS = (10, 30)
# Low-speed thresholds make a stalled clone fail in a minute instead of hanging for the
# whole job timeout, which also keeps the retry budget for transfers that can recover.
GIT_OPTIONS = ['-c', 'http.lowSpeedLimit=1000', '-c', 'http.lowSpeedTime=60']
RUFFLE_URL = 'https://github.com/ruffle-rs/ruffle/releases/download/v0.7.1/ruffle-0.7.1-web-selfhosted.zip'
RUFFLE_SHA256 = 'ef12edbee135d581a9cd523837d746da30a07a64dbf1cf472d2e472c656f4b5e'


def git(*arguments, capture_output=False):
    return subprocess.run(['git', *GIT_OPTIONS, *arguments], check=True,
                          text=True, capture_output=capture_output)


def with_retries(action, what):
    """Run a transfer, retrying transient failures a bounded number of times."""
    for delay in (*BACKOFF_SECONDS, None):
        try:
            return action()
        except (subprocess.CalledProcessError, OSError) as error:
            if delay is None:
                raise
            print(f'{what} failed ({error}); retrying in {delay}s', file=sys.stderr)
            time.sleep(delay)


def revision(path):
    return git('-C', str(path), 'rev-parse', 'HEAD', capture_output=True).stdout.strip()


def verify_checkout(name, path, pinned):
    if revision(path) != pinned:
        raise SystemExit(f'{name}: existing source revision differs; inspect it before changing it.')
    dirty = git('-C', str(path), 'status', '--porcelain', '--untracked-files=no',
                capture_output=True).stdout
    if dirty.strip():
        raise SystemExit(f'{name}: tracked files differ from the pinned source.')


def ensure_source(name, source, vendor):
    path = (vendor / name).resolve()
    pinned = source['revision']

    def clone():
        with tempfile.TemporaryDirectory(prefix=f'.{name}-', dir=path.parent) as directory:
            checkout = Path(directory) / name
            git('clone', '--depth', '1', source['url'], str(checkout))
            if revision(checkout) != pinned:
                git('-C', str(checkout), 'fetch', '--depth', '1', 'origin', pinned)
                git('-C', str(checkout), 'checkout', '--detach', pinned)
            verify_checkout(name, checkout, pinned)
            checkout.rename(path)

    if not path.exists():
        with_retries(clone, f'checkout of {name}')
    else:
        verify_checkout(name, path, pinned)
    print(f'{name}: {pinned}')


def fetch_ruffle(root, vendor):
    archive = root / '.local/ruffle-0.7.1.zip'
    archive.parent.mkdir(mode=0o700, exist_ok=True)

    def download():
        with tempfile.TemporaryDirectory(prefix='.ruffle-', dir=archive.parent) as directory:
            partial = Path(directory) / archive.name
            with urllib.request.urlopen(RUFFLE_URL, timeout=60) as response, partial.open('wb') as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(partial.read_bytes()).hexdigest() != RUFFLE_SHA256:
                raise OSError('Ruffle archive checksum differs from the pinned release.')
            partial.replace(archive)

    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != RUFFLE_SHA256:
        with_retries(download, 'download of Ruffle 0.7.1')
    destination = vendor / 'ruffle'
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
                raise SystemExit('Invalid archive path')
        bundle.extractall(destination)
    print('Ruffle 0.7.1: checksum verified')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', help='comma-separated subset of: houdini,dash,vanilla-media,ruffle')
    args = parser.parse_args(argv)
    wanted = set(args.only.split(',')) if args.only else None
    vendor = ROOT / 'vendor'
    vendor.mkdir(exist_ok=True)
    sources = json.loads((ROOT / 'sources.json').read_text())
    if wanted and not wanted <= set(sources) | {'ruffle'}:
        raise SystemExit('Unknown source in --only')
    for name, source in sources.items():
        if wanted is not None and name not in wanted:
            continue
        ensure_source(name, source, vendor)
    if wanted is not None and 'ruffle' not in wanted:
        return 0
    fetch_ruffle(ROOT, vendor)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

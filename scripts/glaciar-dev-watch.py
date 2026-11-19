#!/usr/bin/env python3
"""Hot-reload helper for the glaciar-dev Docker stack.

Watches the working tree and restarts or reloads only what changed:
  - containers/bots/bots/*.py  -> restart bots container
  - containers/web/*.{html,conf,js,json} -> nginx reload (default.conf) or no-op (static)
  - containers/runtime/{socket_proxy,dialog_feed}.py -> restart webproxy / webdialog

This is not true in-process hot reload (Python and nginx load code at start). A
restart is usually 2–5 s for bots and instant for static HTML.

Usage:
  python3 scripts/glaciar-dev-watch.py
  python3 scripts/glaciar-dev-watch.py --prefix glaciar-dev --debounce 0.8
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WATCH_DIRS = [
    (ROOT / 'containers' / 'bots' / 'bots', 'bots'),
    (ROOT / 'containers' / 'web', 'web'),
    (ROOT / 'containers' / 'runtime', 'runtime'),
]


def docker(args, check=False):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check)


def mtime_tree(directory):
    if not directory.is_dir():
        return 0.0
    latest = 0.0
    for path in directory.rglob('*'):
        if path.is_file():
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                pass
    return latest


def restart(name):
    if docker(['inspect', name], check=False).returncode != 0:
        return
    print(f'[watch] docker restart {name}')
    docker(['restart', name], check=True)


def nginx_reload(web_name):
    if docker(['inspect', web_name], check=False).returncode != 0:
        return
    print(f'[watch] nginx reload in {web_name}')
    docker(['exec', web_name, 'nginx', '-s', 'reload'], check=False)


def apply(prefix, kind, path):
    p = prefix
    if kind == 'bots':
        restart(f'{p}-bots')
    elif kind == 'web':
        if path.name == 'default.conf':
            nginx_reload(f'{p}-web')
        else:
            print(f'[watch] {path.name} served from bind mount; refresh the browser')
    elif kind == 'runtime':
        if path.name == 'socket_proxy.py':
            restart(f'{p}-webproxy')
        elif path.name == 'dialog_feed.py':
            restart(f'{p}-webdialog')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default=os.environ.get('GLACIAR_DEV_PREFIX', 'glaciar-dev'))
    parser.add_argument('--debounce', type=float, default=0.6)
    parser.add_argument('--poll', type=float, default=0.5, help='seconds between mtime scans')
    args = parser.parse_args()

    baseline = {label: mtime_tree(directory) for directory, label in WATCH_DIRS}
    pending = {}

    print('[watch] monitoring', ', '.join(str(d) for d, _ in WATCH_DIRS))
    print('[watch] Ctrl+C to stop')

    try:
        while True:
            time.sleep(args.poll)
            for directory, label in WATCH_DIRS:
                current = mtime_tree(directory)
                if current <= baseline[label]:
                    continue
                baseline[label] = current
                pending[label] = time.monotonic() + args.debounce
            now = time.monotonic()
            for label, deadline in list(pending.items()):
                if now < deadline:
                    continue
                del pending[label]
                directory = next(d for d, l in WATCH_DIRS if l == label)
                newest = max(directory.rglob('*'), key=lambda p: p.stat().st_mtime if p.is_file() else 0,
                             default=directory)
                apply(args.prefix, label, newest)
    except KeyboardInterrupt:
        print('\n[watch] stopped')


if __name__ == '__main__':
    main()
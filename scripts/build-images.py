#!/usr/bin/env python3
"""Build all lab images for one architecture into the local Docker engine."""
import argparse
import platform
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', required=True)
parser.add_argument('--platform', default='linux/' + ('arm64' if platform.machine() in ['arm64', 'aarch64'] else 'amd64'))
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', args.tag):
    raise SystemExit('Invalid image tag')
contexts = {
    'runtime': {'houdini': 'vendor/houdini', 'dash': 'vendor/dash'},
    'database': {'houdini': 'vendor/houdini'},
    'web': {'media': 'vendor/legacy-media', 'ruffle': 'vendor/ruffle'},
}
for component, named in contexts.items():
    command = ['docker', 'buildx', 'build', '--platform', args.platform, '--load']
    for name, path in named.items():
        command += ['--build-context', name + '=' + path]
    command += ['-t', f'clubpenguin/{component}:{args.tag}', f'containers/{component}']
    subprocess.run(command, check=True, cwd=root)

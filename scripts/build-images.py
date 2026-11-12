#!/usr/bin/env python3
"""Build lab images for one architecture into the local Docker engine.

Examples:
  build-images.py --tag mi-release                         # all components, native arch
  build-images.py --tag t --platform linux/arm64           # release build (Apple silicon: native)
  build-images.py --tag t --components runtime,database --web-media stub   # PR CI: no 1.7 GiB of media
  build-images.py --component-tag web=web-abc123def456 --components web   # per-component tags

`--web-media stub` builds the web image around a few placeholder files instead of
`vendor/vanilla-media`, so tests can exercise nginx and the WebSocket proxy route
without downloading the media. A stub image is for tests only and must never be
published; `release.py` refuses to publish anything that was not built with full media.
"""
import argparse
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

root = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tag', help='tag for every component without its own --component-tag')
    parser.add_argument('--component-tag', action='append', default=[], metavar='NAME=TAG')
    parser.add_argument('--components', help='comma-separated subset (default: all enabled)')
    parser.add_argument('--platform', default='linux/' + ('arm64' if platform.machine() in ['arm64', 'aarch64'] else 'amd64'))
    parser.add_argument('--web-media', choices=['full', 'stub'], default='full')
    parser.add_argument('--builder', help='buildx builder to use (needed for --cache-dir)')
    parser.add_argument('--cache-dir', help='local BuildKit cache directory (never put secrets in build contexts)')
    args = parser.parse_args()
    tags = {}
    for item in args.component_tag:
        name, _, value = item.partition('=')
        tags[name] = value
    selected = args.components.split(',') if args.components else release.enabled_components(root)
    for name in selected:
        if name not in release.COMPONENTS:
            raise SystemExit(f'Unknown component: {name}')
        tag = tags.get(name) or args.tag
        if not tag or not TAG_RE.fullmatch(tag):
            raise SystemExit(f'Invalid or missing image tag for {name}')
        tags[name] = tag
    return args, selected, tags


def stub_media(directory):
    """Placeholder content with the layout the web Dockerfile expects."""
    for relative in ('media/play/v2/stub.txt', 'play/stub.txt'):
        target = Path(directory) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('placeholder for tests\n')
    return directory


def main():
    args, selected, tags = parse_args()
    commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    stub_dir = None
    try:
        for component in selected:
            spec = release.COMPONENTS[component]
            contexts = dict(spec['contexts'])
            if component == 'web':
                client_dir = tempfile.mkdtemp(prefix='cp-puffle-client-')
                subprocess.run([sys.executable, str(root / 'scripts/build-puffle-client.py'),
                                '--output', client_dir, *(['--stub'] if args.web_media == 'stub' else [])],
                               check=True, cwd=root)
                contexts['puffle-client'] = client_dir
            if component == 'web' and args.web_media == 'stub':
                stub_dir = stub_dir or stub_media(tempfile.mkdtemp(prefix='cp-stub-media-'))
                contexts['media'] = stub_dir
            command = ['docker', 'buildx', 'build', '--platform', args.platform, '--load',
                       '--provenance=false', '--sbom=false']
            if args.builder:
                command += ['--builder', args.builder]
            if args.cache_dir:
                cache = Path(args.cache_dir) / component
                command += ['--cache-from', f'type=local,src={cache}',
                            '--cache-to', f'type=local,dest={cache}-new,mode=max']
            for name, path in contexts.items():
                command += ['--build-context', f'{name}={path}']
            if commit:
                command += ['--label', f'org.opencontainers.image.revision={commit}']
            if component == 'web' and args.web_media == 'stub':
                command += ['--label', 'clubpenguin.test-only=stub-media']
            command += ['-t', f'clubpenguin/{component}:{tags[component]}', f'containers/{component}']
            subprocess.run(command, check=True, cwd=root)
            if component == 'web':
                shutil.rmtree(client_dir)
            if args.cache_dir:
                # BuildKit appends to the cache; swap in the pruned copy so it cannot grow without bound.
                cache = Path(args.cache_dir) / component
                new = Path(str(cache) + '-new')
                if new.exists():
                    shutil.rmtree(cache, ignore_errors=True)
                    new.rename(cache)
    finally:
        if stub_dir:
            shutil.rmtree(stub_dir, ignore_errors=True)


if __name__ == '__main__':
    main()

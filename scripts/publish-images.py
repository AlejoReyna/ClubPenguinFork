#!/usr/bin/env python3
"""Publish already-built, tested images using the current Azure CLI identity.

Manual/legacy entry point: it publishes `clubpenguin/<component>:<tag>` for the given
components. Pipelines use `release.py publish`, which does the same for the
per-component tags of a release. Either way the push credential lives in a throwaway
Docker config, is never printed, and is deleted afterwards; only linux/arm64 images
built with full media are accepted.
"""
import argparse
import json
from pathlib import Path
import os
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', required=True)
parser.add_argument('--registry', help='ACR name (default: CP_ACR_NAME)')
parser.add_argument('--components', default='runtime,database,web')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', args.tag) or args.tag == 'latest':
    raise SystemExit('Invalid image tag (latest is not a release identifier)')
if args.registry:
    os.environ['CP_ACR_NAME'] = args.registry
names = args.components.split(',')
if any(name not in release.COMPONENTS for name in names):
    raise SystemExit('Unknown component')
document = {'components': {n: {'tag': args.tag, 'needs_build': True, 'fingerprint': '', 'source': 'manual'} for n in names}}
with tempfile.TemporaryDirectory(prefix='cp-publish-') as temp:
    path = Path(temp) / 'release.json'
    path.write_text(json.dumps(document))
    release.publish(argparse.Namespace(release=str(path), out=str(path)))
    done = json.loads(path.read_text())
for name, entry in done['components'].items():
    print(f'{name}: {args.tag} {entry["registry_digest"]}')

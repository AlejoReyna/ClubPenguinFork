#!/usr/bin/env python3
"""Publish tested images using the current Azure CLI identity without printing tokens."""
import argparse
import json
import re
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', required=True)
parser.add_argument('--registry', default='acrdevopslabn3fc0')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', args.tag):
    raise SystemExit('Invalid image tag')
result = subprocess.run(['az', 'acr', 'login', '--name', args.registry, '--expose-token', '-o', 'json'],
                        check=True, capture_output=True, text=True)
credentials = json.loads(result.stdout)
subprocess.run(['docker', 'login', credentials['loginServer'], '--username',
                '00000000-0000-0000-0000-000000000000', '--password-stdin'],
               input=credentials['accessToken'], text=True, check=True, capture_output=True)
for component in ['runtime', 'database', 'web']:
    target = f'{credentials["loginServer"]}/clubpenguin/{component}:{args.tag}'
    subprocess.run(['docker', 'tag', f'clubpenguin/{component}:{args.tag}', target], check=True)
    subprocess.run(['docker', 'push', target], check=True)

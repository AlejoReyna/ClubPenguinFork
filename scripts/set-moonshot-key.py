#!/usr/bin/env python3
"""Store a Moonshot key with hidden input, outside Terraform. Never calls Moonshot.

Requires --execute and a dedicated, already-open SSH tunnel to the Pi. Creating
the Secret does not change provider mode or enable billable model requests.
"""
import argparse
import base64
import getpass
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--expected-node', required=True)
    parser.add_argument('--kubeconfig', default='~/.kube/clubpenguin-pi.yaml')
    args = parser.parse_args()
    if not args.execute:
        print('Preview: external Secret moonshot-api; no key requested, no changes. Use --execute to store it.')
        return
    path = Path(args.kubeconfig).expanduser()
    if path.stat().st_mode & 0o077:
        raise SystemExit('Dedicated kubeconfig must have mode 0600.')
    prefix = ['kubectl', '--kubeconfig', str(path), '--context=default', '--request-timeout=15s']

    def kube(argv, payload=None):
        result = subprocess.run(prefix + argv, input=payload, capture_output=True,
                                text=True, timeout=45)
        if result.returncode:
            raise SystemExit('Kubernetes operation failed; private output omitted.')
        return result.stdout

    cfg = json.loads(kube(['config', 'view', '-o', 'json']))
    if len(cfg['clusters']) != 1 or cfg['clusters'][0]['cluster']['server'] != 'https://127.0.0.1:16443':
        raise SystemExit('Unexpected Kubernetes destination. Use the dedicated Pi tunnel.')
    nodes = json.loads(kube(['get', 'nodes', '-o', 'json']))['items']
    if (len(nodes) != 1 or nodes[0]['metadata']['name'] != args.expected_node
            or nodes[0]['status']['nodeInfo']['architecture'] != 'arm64'
            or not any(c['type'] == 'Ready' and c['status'] == 'True' for c in nodes[0]['status']['conditions'])):
        raise SystemExit('Expected Pi is not Ready; no key requested or written.')
    value = getpass.getpass('Moonshot API key (entrada oculta): ').strip()
    if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise SystemExit('Empty or invalid key; no Secret changed.')
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
              'metadata': {'name': 'moonshot-api', 'namespace': 'clubpenguin'},
              'data': {'MOONSHOT_API_KEY': base64.b64encode(value.encode()).decode()}}
    kube(['apply', '--server-side', '--field-manager=clubpenguin-bots-operator', '-f', '-'], json.dumps(secret))
    print('External Secret moonshot-api stored. Key omitted; provider mode unchanged; zero Moonshot requests.')


if __name__ == '__main__':
    main()

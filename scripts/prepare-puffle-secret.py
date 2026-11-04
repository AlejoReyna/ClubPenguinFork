#!/usr/bin/env python3
"""Create the external broker secret once, after verifying the dedicated Pi target."""
import base64
import json
import secrets
import subprocess

import lablib
import tunnel


def main():
    cfg = lablib.load_config('plan')
    ok, message = tunnel.verify_target(cfg['CP_KUBECONFIG'], cfg['CP_NODE_NAME'])
    if not ok:
        raise SystemExit(message)
    command = ['kubectl', '--kubeconfig', cfg['CP_KUBECONFIG'], '--context', 'default', '-n', 'clubpenguin']
    found = subprocess.run(command + ['get', 'secret', 'puffle-service', '-o', 'name'], capture_output=True)
    if found.returncode == 0:
        print('External puffle service Secret already exists; preserved.')
        return
    value = secrets.token_urlsafe(48)
    document = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'puffle-service', 'namespace': 'clubpenguin'},
                'type': 'Opaque', 'data': {'secret': base64.b64encode(value.encode()).decode()}}
    result = subprocess.run(command + ['create', '-f', '-'], input=json.dumps(document), text=True, capture_output=True)
    if result.returncode:
        raise SystemExit('Could not create service Secret; no credential was printed.')
    print('External puffle service Secret created; value not printed or stored in Git.')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Check the live bot configuration and database prerequisites for an image deploy."""
import json
import os

import deploy
import lablib


def check(deployment, expected_mode):
    if expected_mode not in ('api', 'simulated'):
        raise ValueError('Set the expected bot provider mode explicitly')
    spec = deployment['spec']
    containers = spec['template']['spec']['containers']
    bot = next((c for c in containers if c['name'] == 'bots'), None)
    modes = [v.get('value') for v in (bot or {}).get('env', []) if v.get('name') == 'BOTS_PROVIDER_MODE']
    if (spec.get('replicas', 1) != 1 or spec.get('strategy', {}).get('type') != 'Recreate'
            or modes != [expected_mode]):
        raise ValueError('Live bots must have one replica, Recreate, and the expected provider mode')


def main():
    mode = os.environ.get('TF_VAR_bots_provider_mode', '')
    if os.environ.get('TF_VAR_bots_enabled') != 'true' or os.environ.get('TF_VAR_bots_replicas') != '1':
        lablib.fail('Automatic deployments require bots explicitly enabled with one replica')
    cfg = lablib.load_config('plan')
    raw = deploy.kubectl(cfg, '-n', deploy.NAMESPACE, 'get', 'deployment', 'bots', '-o', 'json').stdout
    try:
        check(json.loads(raw), mode)
    except (ValueError, KeyError, TypeError) as exc:
        lablib.fail(str(exc))
    print('Live bot deployment and explicit provider mode match.')
    schema = deploy.kubectl(
        cfg, '-n', deploy.NAMESPACE, 'exec', 'postgres-0', '--',
        'psql', '-X', '-U', 'houdini', '-d', 'houdini', '-At', '-v', 'ON_ERROR_STOP=1',
        '-c', "SELECT to_regclass('public.penguin_welcome_puffle') IS NOT NULL",
    ).stdout.strip()
    if schema != 't':
        lablib.fail('The live welcome puffle table is missing. Take a verified backup and apply scripts/lab-welcome-puffle.sql before deploying.')
    print('Live welcome puffle table is present.')


if __name__ == '__main__':
    main()

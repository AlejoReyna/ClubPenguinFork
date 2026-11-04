#!/usr/bin/env python3
"""Preview or explicitly provision the three bot accounts and an external Secret.

Only --execute writes: credentials are saved privately BEFORE registering accounts.
Existing accounts without that credential file are never reset, adopted or activated.
No Moonshot key is read or requested and no LLM request is made.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_accounts
import lablib

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--kubeconfig', default='~/.kube/clubpenguin-pi.yaml')
    parser.add_argument('--expected-node', required=True)
    parser.add_argument('--credentials-file', default=str(ROOT / '.local/bots/credentials.json'))
    parser.add_argument('--work-roles', action='store_true', help='use a previously prepared work-credentials.json')
    args = parser.parse_args()
    kubeconfig = str(Path(args.kubeconfig).expanduser())
    config = json.loads(lablib.run(['kubectl', '--kubeconfig', kubeconfig, 'config', 'view', '-o', 'json'], quiet=True).stdout)
    if config['current-context'] != 'default' or len(config['clusters']) != 1 or config['clusters'][0]['cluster']['server'] != 'https://127.0.0.1:16443':
        lablib.fail('Unexpected Kubernetes destination; use the dedicated Pi tunnel/config.')
    if Path(kubeconfig).stat().st_mode & 0o077:
        lablib.fail('The dedicated kubeconfig must be private.')

    def kube(*argv, stdin=None):
        result = lablib.run(['kubectl', '--kubeconfig', kubeconfig, '--context', 'default',
                          '--request-timeout=15s'] + list(argv), input_text=stdin,
                          quiet=True, timeout=90, check=False)
        if result.returncode:
            safe = [part for part in argv if len(part) < 40]
            lablib.fail('Kubernetes operation failed (%s); output omitted to protect credentials.'
                        % ' '.join(safe[:6]))
        return result.stdout

    nodes = json.loads(kube('get', 'nodes', '-o', 'json'))['items']
    if len(nodes) != 1 or nodes[0]['metadata']['name'] != args.expected_node or nodes[0]['status']['nodeInfo']['architecture'] != 'arm64':
        lablib.fail('Unexpected node identity; no changes made.')
    if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in nodes[0]['status']['conditions']):
        lablib.fail('The Pi is not Ready.')
    if args.work_roles and args.credentials_file == str(ROOT / '.local/bots/credentials.json'):
        args.credentials_file = str(ROOT / '.local/bots/work-credentials.json')
    candidate = Path(args.credentials_file).expanduser()
    if candidate.is_symlink():
        lablib.fail('Credential file must not be a symlink.')
    path = candidate.resolve()
    if not path.is_relative_to((ROOT / '.local').resolve()):
        lablib.fail('Bot credentials must remain inside ignored .local/.')
    if path.exists() and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        lablib.fail('Credential file must be private (0600), without symlinks.')
    names = [p[0].lower() for p in (bot_accounts.WORK_ROLES if args.work_roles else bot_accounts.PERSONAS)]
    # The names are fixed alphanumeric constants, never arbitrary SQL input.
    quoted = ','.join("'" + name + "'" for name in names)
    query = ("SELECT COALESCE(json_agg(json_build_object('username',username,'nickname',nickname,"
             "'active',active,'approval_es',approval_es,'moderator',moderator,"
             "'stealth_moderator',stealth_moderator)), '[]'::json) "
             'FROM penguin WHERE username IN (' + quoted + ');')

    def inventory():
        raw = kube('-n', 'clubpenguin', 'exec', 'postgres-0', '--', 'psql', '-U', 'houdini',
                   '-d', 'houdini', '-At', '-v', 'ON_ERROR_STOP=1', '-c', query)
        return {row['username']: row for row in json.loads(raw)}

    existing = inventory()
    if args.work_roles and not bot_accounts.legacy_work_nicknames_unchanged(existing):
        lablib.fail('A work account was renamed after legacy provisioning; no accounts or Secret changed.')
    for name, account in existing.items():
        if account['moderator'] or account['stealth_moderator'] or not account['active']:
            lablib.fail('A target bot account is privileged or inactive; no flags will be changed.')
    if existing and not path.exists():
        lablib.fail('Target accounts already exist but their private credentials are missing; no reset or adoption performed.')
    secret = kube('-n', 'clubpenguin', 'get', 'secret', 'bot-credentials', '--ignore-not-found', '-o', 'name').strip()
    if secret and not path.exists():
        lablib.fail('An external bot Secret already exists without this credential file; inspect ownership before provisioning.')
    if args.work_roles and not path.exists():
        lablib.fail('Prepare the private work-role configuration with verified areas first.')
    document = json.loads(path.read_text()) if path.exists() else bot_accounts.config_document()
    bots = {b['username'].lower(): b for b in document['bots']}
    if set(bots) != set(names):
        lablib.fail('Private bot identities do not match the three expected accounts.')
    if args.work_roles:
        import sys
        sys.path.insert(0, str(ROOT / 'containers/bots'))
        from bots.config import build_config
        candidate_config = build_config(document, {})
        expected = {name.lower(): (name, room) for name, _, room, _ in bot_accounts.WORK_ROLES}
        if len(candidate_config.bots) != 3 or any(
            (b.nickname, b.room) != expected[b.username.lower()] or 'walk' not in bots[b.username.lower()]
            for b in candidate_config.bots
        ):
            lablib.fail('Work roles require their dedicated names, rooms and explicit walk areas.')
    missing = [name for name in names if name not in existing]
    if not args.execute:
        print(f'Preview: {len(missing)} new accounts, {len(existing)} existing; external Secret bot-credentials.')
        print('No account, Secret or file changed. --execute is required to provision.')
        return
    if args.work_roles:
        deployment = json.loads(kube('-n', 'clubpenguin', 'get', 'deployment', 'bots', '-o', 'json'))
        if not bot_accounts.deployment_is_stopped(deployment):
            lablib.fail('Stop the bots Deployment and wait for all replicas to terminate before migration.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    lablib.write_private(path, json.dumps(document, indent=2))
    if missing:
        result = kube('-n', 'clubpenguin', 'exec', '-i', 'deploy/dash', '--', 'python', '-c',
                      bot_accounts.registration_program(), stdin=json.dumps({'bots': [bots[name] for name in missing]}))
        if json.loads(result)['created'] != len(missing):
            lablib.fail('Registration result did not match the requested accounts.')
    if args.work_roles:
        # Dash title-cases the nickname. Correct it before any roster or Secret write.
        corrected = kube('-n', 'clubpenguin', 'exec', 'postgres-0', '--', 'psql', '-U', 'houdini',
                         '-d', 'houdini', '-At', '-v', 'ON_ERROR_STOP=1', '-c',
                         bot_accounts.nickname_correction_sql())
        got = sorted(line.strip() for line in corrected.splitlines()
                     if line.strip() and not line.strip().startswith('UPDATE '))
        if got != sorted(names):
            lablib.fail('Visible nicknames could not be set to the approved role names; Secret was not changed.')
    after = inventory()
    if set(after) != set(names) or any(not a['active'] or a['moderator'] or a['stealth_moderator'] for a in after.values()):
        lablib.fail('Bot account checks did not pass; credentials remain private for recovery.')
    if args.work_roles:
        expected_nicks = {name.lower(): name for name, *_rest in bot_accounts.WORK_ROLES}
        if any(after[name].get('nickname') != expected_nicks[name] or not after[name].get('approval_es')
               for name in names):
            lablib.fail('Visible name or Spanish approval flag is wrong; Secret was not changed.')
    authenticated = kube('-n', 'clubpenguin', 'exec', '-i', 'deploy/dash', '--', 'python', '-c',
                         bot_accounts.authentication_program(), stdin=json.dumps(document))
    if json.loads(authenticated)['authenticated'] != len(names):
        lablib.fail('Bot credential verification did not pass; Secret was not changed.')
    roster_report = {'approved': None, 'reason': None}
    if args.work_roles:
        roster_report = json.loads(kube('-n', 'clubpenguin', 'exec', '-i', 'deploy/dash', '--', 'python', '-c',
                                        bot_accounts.roster_approval_program(), stdin=json.dumps(document)))
        if roster_report.get('approved') != len(names) or roster_report.get('reason'):
            lablib.fail('Spanish roster approval (bit 8) or the visible name did not match; Secret was not changed.')
    # Server-side apply avoids a last-applied annotation containing a duplicate of Secret data.
    generated = kube('-n', 'clubpenguin', 'create', 'secret', 'generic', 'bot-credentials',
                     '--from-file=config.json=' + str(path), '--dry-run=client', '-o', 'json')
    kube('-n', 'clubpenguin', 'apply', '--server-side', '--field-manager=clubpenguin-bots-operator', '-f', '-', stdin=generated)
    summary = {
        'created': len(missing), 'reused': len(existing), 'accounts': names,
        'all_active': True, 'all_non_moderator': True, 'secret': 'bot-credentials',
        'moonshot_calls': 0,
    }
    if args.work_roles:
        summary['nicknames_exact'] = True
        summary['spanish_roster_bit'] = bot_accounts.SPANISH_APPROVAL_BIT
        summary['roster_approved'] = roster_report.get('approved')
    lablib.write_private(path.with_name('provisioning.json'), json.dumps(summary, indent=2))
    print(f'Bot accounts ready: {len(missing)} created, {len(existing)} reused; normal privileges verified.')
    print('External Secret bot-credentials ready. Passwords were not printed. No LLM calls or Deployment changes.')


if __name__ == '__main__':
    main()

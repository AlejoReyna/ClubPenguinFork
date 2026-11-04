#!/usr/bin/env python3
"""Idempotently prepare GitHub environments and narrowly scoped Azure OIDC.

Requires the operator's existing gh/az logins. Never prints access tokens or
registration credentials. Runner installation is deliberately a separate step.
"""
import json
import subprocess

import lablib

REPO = 'AlejoReyna/ClubPenguinFork'


def run(command, data=None):
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('Configuration failed: ' + ' '.join(command[:3]) + ' (credential details withheld)')
    return json.loads(result.stdout) if result.stdout.strip() else None


def oidc_subject_prefix(customization):
    """Subject prefix GitHub actually presents (immutable owner@id/repo@id when enabled)."""
    customization = customization or {}
    if customization.get('use_immutable_subject') and customization.get('sub_claim_prefix'):
        return customization['sub_claim_prefix']
    return f'repo:{REPO}'


def main():
    cfg = lablib.load_config('plan')
    run(['gh', 'api', '-X', 'PUT', f'repos/{REPO}/actions/permissions/fork-pr-contributor-approval',
         '-f', 'approval_policy=all_external_contributors'])
    account = run(['az', 'account', 'show', '-o', 'json'])
    name = 'gh-clubpenguin-lab-deploy'
    apps = run(['az', 'ad', 'app', 'list', '--filter', f"displayName eq '{name}'", '-o', 'json'])
    if len(apps) > 1:
        raise SystemExit('Ambiguous deploy identity; refusing to select one')
    app = apps[0] if apps else run(['az', 'ad', 'app', 'create', '--display-name', name, '-o', 'json'])
    client = app['appId']
    principals = run(['az', 'ad', 'sp', 'list', '--filter', f"appId eq '{client}'", '-o', 'json'])
    principal = principals[0] if principals else run(['az', 'ad', 'sp', 'create', '--id', client, '-o', 'json'])
    federations = run(['az', 'ad', 'app', 'federated-credential', 'list', '--id', client, '-o', 'json'])
    prefix = oidc_subject_prefix(run(['gh', 'api', f'repos/{REPO}/actions/oidc/customization/sub']))
    for environment in ('clubpenguin-lab', 'clubpenguin-backup'):
        subject = f'{prefix}:environment:{environment}'
        if not any(f['subject'] == subject for f in federations):
            suffix = '' if prefix == f'repo:{REPO}' else '-immutable'
            run(['az', 'ad', 'app', 'federated-credential', 'create', '--id', client, '--parameters',
                 json.dumps({'name': 'github-' + environment + suffix, 'issuer': 'https://token.actions.githubusercontent.com',
                             'subject': subject, 'audiences': ['api://AzureADTokenExchange']}), '-o', 'json'])
        endpoint = f'repos/{REPO}/environments/{environment}'
        run(['gh', 'api', '-X', 'PUT', endpoint, '--input', '-'],
            {'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}})
        policies = run(['gh', 'api', endpoint + '/deployment-branch-policies'])
        if not any(x['name'] == 'main' for x in policies['branch_policies']):
            run(['gh', 'api', '-X', 'POST', endpoint + '/deployment-branch-policies', '-f', 'name=main', '-f', 'type=branch'])
    acr = run(['az', 'acr', 'show', '--name', cfg['CP_ACR_NAME'], '-o', 'json'])
    storage = run(['az', 'storage', 'account', 'show', '--name', cfg['CP_STATE_STORAGE_ACCOUNT'],
                   '--resource-group', cfg['CP_STATE_RESOURCE_GROUP'], '-o', 'json'])
    for role, scope in [('AcrPush', acr['id'])] + [
            ('Storage Blob Data Contributor', storage['id'] + '/blobServices/default/containers/' + container)
            for container in (cfg['CP_STATE_CONTAINER'], cfg['CP_BACKUP_CONTAINER'])]:
        existing = run(['az', 'role', 'assignment', 'list', '--assignee', principal['id'], '--scope', scope, '-o', 'json'])
        if not any(x['roleDefinitionName'] == role and x['scope'].lower() == scope.lower() for x in existing):
            run(['az', 'role', 'assignment', 'create', '--assignee-object-id', principal['id'],
                 '--assignee-principal-type', 'ServicePrincipal', '--role', role, '--scope', scope, '-o', 'json'])
    values = {'CP_AUTO_DEPLOY_ENABLED': 'false', 'CP_BACKUP_ENABLED': 'false', 'AZURE_CLIENT_ID': client,
              'AZURE_TENANT_ID': account['tenantId'], 'AZURE_SUBSCRIPTION_ID': account['id'],
              'CP_NODE_NAME': cfg['CP_NODE_NAME']}
    # Preserve the live provider mode rather than changing it while enabling CI.
    live = run(['kubectl', '--kubeconfig', cfg['CP_KUBECONFIG'], '--context', 'default', '-n', 'clubpenguin',
                'get', 'deployment', 'bots', '-o', 'json'])
    bot = next(c for c in live['spec']['template']['spec']['containers'] if c['name'] == 'bots')
    values['CP_BOTS_PROVIDER_MODE'] = next(e['value'] for e in bot['env'] if e['name'] == 'BOTS_PROVIDER_MODE')
    proxy = run(['kubectl', '--kubeconfig', cfg['CP_KUBECONFIG'], '--context', 'default', '-n', 'clubpenguin',
                 'get', 'deployment', 'web', '-o', 'json'])
    origins = next(e['value'] for c in proxy['spec']['template']['spec']['containers'] for e in c.get('env', [])
                   if e['name'] == 'ALLOWED_ORIGINS')
    values['CP_PROXY_EXTRA_HOSTS'] = ','.join(h for h in origins.split(',') if h and h != cfg['CP_PUBLIC_HOSTNAME'])
    for key, value in values.items():
        run(['gh', 'variable', 'set', key, '--repo', REPO, '--body', value])
    run(['gh', 'api', '-X', 'PUT', f'repos/{REPO}/branches/main/protection', '--input', '-'], {
        'required_status_checks': {'strict': True, 'contexts': ['Validate configuration', 'Test isolated containers and bots']},
        'enforce_admins': False, 'required_pull_request_reviews': {'required_approving_review_count': 0}, 'restrictions': None})
    print('GitHub environments, main protection, Azure OIDC and scoped permissions configured; auto deploy and scheduled backup remain off.')


if __name__ == '__main__':
    main()

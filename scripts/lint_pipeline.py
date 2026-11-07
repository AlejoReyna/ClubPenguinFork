#!/usr/bin/env python3
"""Project-specific checks for azure-pipelines.yml (needs PyYAML).

This is NOT Azure DevOps' own validation: it cannot expand templates or know which
service connections exist. It parses the YAML and enforces the safety rules this lab
depends on, so a careless edit fails in CI instead of reaching the Pi:

  * deployment is off by default; PR runs never reach credentials or the Mac agent;
  * credentialed stages are guarded for main, non-PR runs; apply lives in a deployment job;
  * no pipeline text can bypass the reviewed plan (-auto-approve, direct terraform, destroy);
  * no step prints the environment or Secrets; no `latest` tags; no persisted git credentials;
  * plan/apply/verify jobs are pinned to one agent; every job has a timeout;
  * the cloudflared enablement stays explicit in the deploy scripts.

The real validation of the YAML is Azure DevOps itself (see docs/operations.md).
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit('PyYAML is required (pip install pyyaml)')

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TEXT = [
    (r'-auto-approve', 'terraform -auto-approve bypasses the reviewed plan'),
    (r'terraform\s+(apply|plan|destroy|show|state|import|init)', 'run Terraform only through scripts/deploy.py (saved, gated plan)'),
    (r'\bset\s+-[a-z]*x', '`set -x` would echo commands and secrets into the log'),
    (r'\bprintenv\b|\|\s*env\b|^\s*env\s*$', 'never print the environment'),
    (r'kubectl[^\n]*get\s+secrets?\b', 'do not read Kubernetes Secrets in the pipeline'),
    (r':latest\b', '`latest` is not a release identifier'),
    (r'System\.AccessToken', 'the job access token is not needed'),
]
# Names (keys and values) that must not appear anywhere in the pipeline.
FORBIDDEN_NAMES = [(r'(?i)moonshot|kimi', 'pipelines must not wire paid-provider credentials')]
# Settings that must never be enabled, checked structurally (they are keys, not text).
FORBIDDEN_SETTINGS = {
    'addSpnToEnvironment': 'do not expose the federated token to scripts',
    'persistCredentials': 'do not leave git credentials in the workspace',
}


def walk(node):
    """Yield every node (dicts, lists and scalars), including both branches of ${{ if }}."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def text_of(node):
    """All keys and string values, so key names (azureSubscription, vmImage) are searchable."""
    parts = []
    for item in walk(node):
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.extend(str(key) for key in item)
    return '\n'.join(parts)


def values_of(node):
    """String values only (scripts and settings), for scanning commands."""
    return '\n'.join(n for n in walk(node) if isinstance(n, str))


def stages_of(document):
    found = []
    for item in document.get('stages', []):
        if isinstance(item, dict) and 'stage' in item:
            found.append(item)
        elif isinstance(item, dict):  # ${{ if ... }}: [stage, ...]
            for value in item.values():
                found += [s for s in (value if isinstance(value, list) else []) if isinstance(s, dict) and 'stage' in s]
    return found


def jobs_of(stage):
    found = []
    for item in stage.get('jobs', []):
        if isinstance(item, dict) and ('job' in item or 'deployment' in item):
            found.append(item)
    return found


def lint(document, deploy_script_text=''):
    problems = []
    parameters = {p['name']: p for p in document.get('parameters', []) if isinstance(p, dict) and 'name' in p}
    if parameters.get('deploy', {}).get('default') is not False:
        problems.append('parameter "deploy" must exist and default to false')
    if document.get('lockBehavior') != 'sequential':
        problems.append('lockBehavior: sequential is required (one deployment at a time)')
    for key in ('trigger', 'pr'):
        include = (document.get(key) or {}).get('branches', {}).get('include')
        if include != ['main']:
            problems.append(f'{key} must include only the main branch')

    everything = values_of(document)
    for pattern, reason in FORBIDDEN_TEXT:
        if re.search(pattern, everything, re.MULTILINE):
            problems.append(f'forbidden pattern /{pattern}/: {reason}')

    names = text_of(document)
    for pattern, reason in FORBIDDEN_NAMES:
        if re.search(pattern, names):
            problems.append(f'forbidden name /{pattern}/: {reason}')
    for node in walk(document):
        if isinstance(node, dict):
            for key, reason in FORBIDDEN_SETTINGS.items():
                if str(node.get(key, '')).lower() == 'true':
                    problems.append(f'{key}: true is forbidden: {reason}')

    stages = {s['stage']: s for s in stages_of(document)}
    for name, stage in stages.items():
        condition = str(stage.get('condition', ''))
        stage_text = text_of(stage)
        credentialed = ('azureSubscription' in stage_text or 'environment' in stage_text
                        or any(isinstance(n, dict) and 'name' in n and 'demands' in n for n in walk(stage)))
        uses_vm_image = 'vmImage' in stage_text
        if credentialed:
            if 'vmImage' in stage_text:
                problems.append(f'stage {name}: hosted agents must never receive credentials or the lab network')
            guarded = 'PullRequest' in condition or 'dependencies.Release.result' in condition
            if not guarded:
                problems.append(f'stage {name}: credentialed stages must exclude pull requests in their condition')
        elif not uses_vm_image and name not in ('Cleanup',):
            problems.append(f'stage {name}: expected a Microsoft-hosted pool (vmImage)')
        for job in jobs_of(stage):
            label = f'{name}/{job.get("job") or job.get("deployment")}'
            if 'timeoutInMinutes' not in job:
                problems.append(f'job {label}: timeoutInMinutes is required')
            job_text = text_of(job)
            if 'deployment' in job and not job.get('environment'):
                problems.append(f'job {label}: deployment jobs must target an environment (approvals live there)')
            if 'deploy.py apply' in job_text and 'deployment' not in job:
                problems.append(f'job {label}: apply must run in a deployment job behind the environment')
            if re.search(r'deploy\.py (plan|apply|verify)', job_text) and 'Agent.Name' not in job_text:
                problems.append(f'job {label}: plan/apply/verify must be pinned to one agent (demands: Agent.Name)')
            if 'deploy.py apply' in job_text and '--expect-sha256' not in job_text:
                problems.append(f'job {label}: apply must pass the hash of the reviewed plan')
    release = stages.get('Release')
    if release:
        condition = str(release.get('condition', ''))
        for needle in ('PullRequest', 'refs/heads/main'):
            if needle not in condition:
                problems.append(f'stage Release: condition must contain {needle}')
        if 'Apply' in stages and 'PullRequest' not in str(stages['Apply'].get('condition', '')):
            problems.append('stage Apply: condition must exclude pull requests')
    else:
        problems.append('stage Release is missing')
    if deploy_script_text:
        if '-var=cloudflared_enabled=true' not in deploy_script_text:
            problems.append('scripts/deploy.py must pass -var=cloudflared_enabled=true explicitly')
        if re.search(r'cloudflared_enabled=false', deploy_script_text):
            problems.append('scripts/deploy.py must never disable cloudflared')
    return problems


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'azure-pipelines.yml'
    document = yaml.safe_load(path.read_text())
    deploy = (ROOT / 'scripts/deploy.py').read_text()
    problems = lint(document, deploy)
    for problem in problems:
        print('LINT:', problem)
    if problems:
        raise SystemExit(1)
    print(f'{path.name}: policy checks passed ({len(stages_of(document))} stages)')


if __name__ == '__main__':
    main()

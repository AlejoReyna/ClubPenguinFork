#!/usr/bin/env python3
"""Decide which expensive CI work a pull request needs, conservatively.

Validation (Terraform, Ansible, Python, Helm, pipeline lint) always runs. The container
build and tests are skipped only when *every* changed file is known to be inert for them
(documentation, monitoring, Terraform, Ansible, GitHub metadata). Anything unknown, and
anything shared (scripts, sources.json, the pipeline itself, tests), triggers the work, so
a dependency can never be silently skipped. Pushes to main always run everything.

Why not `paths:` filters on the trigger: a skipped required check never reports, which
blocks merging. Deciding inside the pipeline keeps the check green when nothing needs to run.
"""
import argparse
import fnmatch
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402

# Files that cannot change the images or their tests. Validation covers them separately.
INERT = ['docs/*', 'README.md', '*.md', '.gitignore', '.github/*', 'monitoring/*', 'infra/*', 'ansible/*']
# Never inert even if a pattern above would match (checked first).
SHARED = ['scripts/*', 'sources.json', 'azure-pipelines.yml', 'tests/*', 'containers/*', 'vendor/*', 'production/*', 'experiments/*']


def is_inert(path):
    if any(fnmatch.fnmatch(path, pattern) for pattern in SHARED):
        return False
    return any(fnmatch.fnmatch(path, pattern) for pattern in INERT)


def classify(files, everything=False):
    relevant = [f for f in files if not is_inert(f)]
    run_containers = everything or bool(relevant)
    reason = ('main or manual run: everything runs' if everything else
              ('%d changed file(s) can affect images or tests' % len(relevant)) if relevant else
              'only documentation, infrastructure or monitoring files changed; validation still runs')
    return {'runContainers': run_containers, 'reason': reason, 'changed': len(files)}


def changed_files(base, head):
    # In a PR build HEAD is a merge commit; diff against the target branch tip it merged into.
    result = lablib.run(['git', '-C', str(lablib.ROOT), 'diff', '--name-only', f'{base}...{head}'], check=False, quiet=True)
    if result.returncode != 0:
        result = lablib.run(['git', '-C', str(lablib.ROOT), 'diff', '--name-only', base, head], check=False, quiet=True)
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', help='target branch ref (e.g. origin/main); omit to run everything')
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--all', action='store_true', help='force everything (push to main, manual runs)')
    parser.add_argument('--vso', action='store_true', help='emit an Azure DevOps output variable')
    args = parser.parse_args()
    if args.all or not args.base:
        result = classify([], everything=True)
    else:
        files = changed_files(args.base, args.head)
        if files is None:  # cannot compute the diff: never skip
            result = classify([], everything=True)
            result['reason'] = 'could not compute the diff: everything runs'
        else:
            result = classify(files)
    print(json.dumps(result, sort_keys=True))
    if args.vso:
        lablib.set_pipeline_variable('runContainers', str(result['runContainers']).lower())


if __name__ == '__main__':
    main()

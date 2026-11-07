#!/usr/bin/env python3
"""Summarize and gate a Terraform plan without ever printing attribute values.

Input is `terraform show -json <plan>` (read from a file or stdin). Output is a
sanitized summary: resource addresses, actions and the *paths* of attributes that
change. The only values shown are image tags, which are not secret. Secrets that
a plan may contain (passwords, Secret data) are never copied.

The gate answers four questions before anything is applied:

  1. Is the scope the lab's own (known resource types, lab namespaces only)?
  2. Does it destroy or replace something that holds data or the public tunnel?
  3. Would it create the Cloudflare connector/policy that already exist live but
     are not in the state (they must be imported, never recreated or deleted)?
  4. Does it touch data-bearing resources, so a verified backup must come first?

Exit status: 0 allowed, 2 blocked.
"""
import argparse
import json
import re
import sys
from pathlib import Path

LAB_NAMESPACES = {'clubpenguin', 'monitoring-lab'}
KNOWN_TYPES = {
    'kubernetes_namespace_v1', 'kubernetes_secret_v1', 'kubernetes_persistent_volume_claim_v1',
    'kubernetes_stateful_set_v1', 'kubernetes_service_v1', 'kubernetes_deployment_v1',
    'kubernetes_network_policy_v1', 'kubernetes_config_map_v1', 'helm_release', 'random_password',
}
# Delete/replace of these needs an explicit --allow-destructive naming the address.
PROTECTED = [
    r'^kubernetes_namespace_v1\.', r'^kubernetes_persistent_volume_claim_v1\.', r'^kubernetes_stateful_set_v1\.',
    r'^random_password\.', r'^kubernetes_secret_v1\.(postgres|grafana)\b', r'^helm_release\.',
    r'^kubernetes_deployment_v1\.cloudflared\b', r'^kubernetes_network_policy_v1\.web_from_cloudflared\b',
]
# Changes to these can alter or endanger stored data: take a verified backup first.
DATA_RESOURCES = [
    r'^kubernetes_stateful_set_v1\.postgres\b', r'^kubernetes_persistent_volume_claim_v1\.postgres\b',
    r'^kubernetes_secret_v1\.postgres\b', r'^random_password\.postgres\b',
    r'^kubernetes_deployment_v1\.runtime\["(login|glaciar|dash)"\]',
]
CLOUDFLARED = {
    'kubernetes_deployment_v1.cloudflared[0]': ('deployment', 'cloudflared', 'clubpenguin/cloudflared'),
    'kubernetes_network_policy_v1.web_from_cloudflared[0]': ('networkpolicy', 'web-from-cloudflared',
                                                              'clubpenguin/web-from-cloudflared'),
}
KIND = {('create',): 'create', ('update',): 'update', ('delete',): 'delete', ('read',): 'read', ('no-op',): 'no-op',
        ('delete', 'create'): 'replace', ('create', 'delete'): 'replace'}


def matches(patterns, address):
    return any(re.search(pattern, address) for pattern in patterns)


def diff_paths(before, after, unknown=None, prefix='', limit=40):
    """Attribute paths that differ. Values are intentionally not returned."""
    paths = []

    def walk(b, a, u, path):
        if len(paths) >= limit:
            return
        if u is True:
            paths.append(path)
        elif isinstance(b, dict) and isinstance(a, dict):
            for key in sorted(set(b) | set(a)):
                walk(b.get(key), a.get(key), (u or {}).get(key) if isinstance(u, dict) else None,
                     f'{path}.{key}' if path else key)
        elif isinstance(b, list) and isinstance(a, list) and len(b) == len(a):
            for index, (x, y) in enumerate(zip(b, a)):
                walk(x, y, (u or [None] * len(a))[index] if isinstance(u, list) else None, f'{path}[{index}]')
        elif b != a:
            paths.append(path)

    walk(before, after, unknown, prefix)
    return paths


def image_changes(before, after, paths):
    """Return {path: (old_tag, new_tag)} for image attributes only (tags are not secret)."""
    def lookup(document, path):
        node = document
        for part in re.findall(r'[^.\[\]]+', path):
            if isinstance(node, list):
                node = node[int(part)]
            else:
                node = node[part]
        return node

    result = {}
    for path in paths:
        if path.endswith('.image'):
            try:
                old, new = lookup(before, path), lookup(after, path)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            tag = lambda value: str(value).rsplit(':', 1)[-1] if isinstance(value, str) and ':' in value else '?'  # noqa: E731
            result[path] = (tag(old), tag(new))
    return result


def summarize(plan):
    changes = []
    for resource in plan.get('resource_changes', []):
        change = resource['change']
        kind = KIND.get(tuple(change['actions']), '+'.join(change['actions']))
        if kind in ('no-op', 'read'):
            continue
        before, after = change.get('before') or {}, change.get('after') or {}
        paths = diff_paths(before, after, change.get('after_unknown')) if kind == 'update' else []
        namespace = None
        for document in (after, before):
            meta = (document.get('metadata') or [{}]) if isinstance(document.get('metadata'), list) else [{}]
            if meta and isinstance(meta[0], dict) and meta[0].get('namespace'):
                namespace = meta[0]['namespace']
                break
        changes.append({
            'address': resource['address'], 'type': resource['type'], 'kind': kind, 'paths': paths,
            'images': {p: list(v) for p, v in image_changes(before, after, paths).items()} if kind == 'update' else {},
            'namespace': namespace,
            'restarts': kind == 'update' and any(p.startswith('spec[0].template') for p in paths),
        })
    return changes


def classify(changes, live=None, allow_destructive=(), expect=None):
    """Apply the lab's policy. ``live`` maps 'kind/name' presence checks from the cluster."""
    allow = set(allow_destructive)
    blocked, notes, backup_reasons, adoption = [], [], [], []
    for item in changes:
        address, kind = item['address'], item['kind']
        if item['type'] not in KNOWN_TYPES:
            blocked.append(f'{address}: resource type outside the lab scope')
        if item['namespace'] and item['namespace'] not in LAB_NAMESPACES:
            blocked.append(f'{address}: namespace outside the lab ({item["namespace"]})')
        if kind in ('delete', 'replace') and matches(PROTECTED, address) and address not in allow:
            blocked.append(f'{address}: {kind} of a protected resource (data, credentials or the public tunnel); '
                           'pass --allow-destructive with this exact address only after a verified backup and review')
        if address in CLOUDFLARED and kind == 'create':
            resource, name, ident = CLOUDFLARED[address]
            if live is not None and live.get(f'{resource}/{name}'):
                adoption.append(address)
                blocked.append(f'{address}: exists in the cluster but not in the Terraform state; import it '
                               f'(terraform import \'{address}\' {ident}) instead of recreating it')
        substantive = kind in ('create', 'delete', 'replace') or any(not p.startswith('metadata') for p in item['paths'])
        if matches(DATA_RESOURCES, address) and substantive:
            backup_reasons.append(f'{address} ({kind})')
        if item['restarts']:
            notes.append(f'{address} will restart its pods')
    if expect == 'images-only':
        for item in changes:
            only_images = item['kind'] == 'update' and item['paths'] and all(p.endswith('.image') for p in item['paths'])
            release_info = item['address'].startswith('kubernetes_config_map_v1.release')
            if not (only_images or release_info):
                blocked.append(f'{item["address"]}: a rollback to recorded image tags must change images only')
    return {'blocked': blocked, 'notes': notes, 'backup_required': bool(backup_reasons),
            'backup_reasons': backup_reasons, 'adoption_required': adoption}


def render(changes, verdict):
    lines = []
    symbols = {'create': '+', 'update': '~', 'delete': '-', 'replace': '-/+'}
    for item in changes:
        lines.append(f'{symbols.get(item["kind"], "?"):>3} {item["address"]}  [{item["kind"]}]')
        for old, new in item['images'].values():
            lines.append(f'      image {old} -> {new}')
        others = [p for p in item['paths'] if not p.endswith('.image')]
        if others:
            lines.append('      changed: ' + ', '.join(others[:6]) + (' …' if len(others) > 6 else ''))
    if not changes:
        lines.append('No changes.')
    lines.append('')
    lines.append(f'Backup before apply: {"REQUIRED" if verdict["backup_required"] else "not required"}')
    for reason in verdict['backup_reasons']:
        lines.append(f'  - {reason}')
    for note in verdict['notes']:
        lines.append(f'Note: {note}')
    for problem in verdict['blocked']:
        lines.append(f'BLOCKED: {problem}')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--plan', required=True, help='terraform show -json output ("-" for stdin)')
    parser.add_argument('--live', help='JSON like {"deployment/cloudflared": true}')
    parser.add_argument('--allow-destructive', default='', help='comma-separated exact addresses')
    parser.add_argument('--expect', choices=['images-only'])
    parser.add_argument('--out', help='write the sanitized summary JSON here')
    args = parser.parse_args()
    plan = json.load(sys.stdin) if args.plan == '-' else json.loads(Path(args.plan).read_text())
    live = json.loads(Path(args.live).read_text()) if args.live else None
    changes = summarize(plan)
    verdict = classify(changes, live, [a for a in args.allow_destructive.split(',') if a], args.expect)
    summary = {'changes': changes, 'verdict': verdict, 'has_changes': bool(changes)}
    print(render(changes, verdict))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    raise SystemExit(2 if verdict['blocked'] else 0)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Compute, verify, publish and record lab releases.

A release is a set of per-component image tags. Tags are derived from the content
of each component's inputs (its tracked files, the pinned upstream revisions it
uses and an optional salt), so:

* a change that only touches the frontend produces a new `web` tag and leaves
  `database` (PostgreSQL) on its existing tag, hence no restart;
* an image that already exists in the registry for that fingerprint is reused,
  never rebuilt (the web image carries ~1.7 GiB of media);
* every tag maps back to a commit through the release manifest, and `latest` is
  never used.

Subcommands: resolve, publish, manifest, tfvars, list, fetch. See --help.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lablib  # noqa: E402
from lablib import ROOT, TAG_RE, fail, git, run  # noqa: E402

FINGERPRINT_VERSION = 'clubpenguin-release-v1'

# Registry repository is always clubpenguin/<name>. `contexts` are the named build
# contexts consumed by the Dockerfile; `sources` are the pinned upstream inputs.
COMPONENTS = {
    'runtime': {'prefix': 'rt', 'sources': ['houdini', 'dash'],
                'contexts': {'houdini': 'vendor/houdini', 'dash': 'vendor/dash',
                             'puffle': 'experiments/puffle_sim', 'production': 'production/puffle'}},
    'database': {'prefix': 'db', 'sources': ['houdini'], 'contexts': {'houdini': 'vendor/houdini'}},
    'web': {'prefix': 'web', 'sources': ['legacy-media', 'ruffle'],
            'contexts': {'media': 'vendor/legacy-media', 'ruffle': 'vendor/ruffle'}},
    # Appears only when containers/bots/Dockerfile is tracked in Git.
    'bots': {'prefix': 'bots', 'sources': [], 'contexts': {}, 'optional': True},
}


def enabled_components(root=ROOT):
    names = []
    for name, spec in COMPONENTS.items():
        if spec.get('optional') and not tracked_files(name, root).get('containers/%s/Dockerfile' % name):
            continue
        names.append(name)
    return names


def tracked_files(component, root=ROOT):
    """Return {relative path: absolute path} for the tracked files of a component."""
    extra = {'runtime': ['experiments/puffle_sim', 'production/puffle'],
             'web': ['experiments/puffle_sim', 'production/puffle', 'scripts/build-puffle-client.py']}
    out = run(['git', '-C', str(root), 'ls-files', '-z', '--', f'containers/{component}',
               *extra.get(component, [])],
              quiet=True).stdout
    return {p: Path(root) / p for p in out.split('\0') if p}


def source_revisions(root=ROOT):
    sources = json.loads((Path(root) / 'sources.json').read_text())
    revisions = {name: spec['revision'] for name, spec in sources.items()}
    # Ruffle is pinned by checksum inside fetch-sources.py rather than sources.json.
    fetch = (Path(root) / 'scripts/fetch-sources.py').read_text()
    match = re.search(r"expected\s*=\s*'([0-9a-f]{64})'", fetch)
    if not match:
        fail('Could not read the pinned Ruffle checksum from scripts/fetch-sources.py')
    revisions['ruffle'] = match.group(1)
    return revisions


def fingerprint(component, root=ROOT, revisions=None, salt=''):
    spec = COMPONENTS[component]
    revisions = revisions or source_revisions(root)
    digest = hashlib.sha256()
    digest.update(f'{FINGERPRINT_VERSION}\0{component}\0{salt}\n'.encode())
    files = tracked_files(component, root)
    if not files:
        fail(f'No tracked files for component {component}')
    for relative in sorted(files):
        content = files[relative].read_bytes()
        digest.update(f'{relative}\0{hashlib.sha256(content).hexdigest()}\n'.encode())
    for name in spec['sources']:
        digest.update(f'source:{name}:{revisions[name]}\n'.encode())
    return digest.hexdigest()


def dirty_paths(root=ROOT):
    status = run(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no',
                  '--', 'containers', 'experiments', 'production', 'sources.json',
                  'scripts/fetch-sources.py', 'scripts/build-puffle-client.py'], quiet=True).stdout
    return [line for line in status.splitlines() if line.strip()]


def tag_for(component, root=ROOT, salt='', revisions=None):
    return '%s-%s' % (COMPONENTS[component]['prefix'], fingerprint(component, root, revisions, salt)[:12])


def parse_pins(values):
    pins = {}
    for item in values or []:
        name, _, tag = item.partition('=')
        if name not in COMPONENTS or not TAG_RE.fullmatch(tag) or tag == 'latest':
            fail(f'Invalid --pin "{item}": expected component=tag (never latest)')
        pins[name] = tag
    return pins


# --- registry access (az must already be signed in by the pipeline task) ---------

def registry_tag(acr, component, tag):
    """Return the manifest digest of clubpenguin/<component>:<tag>, or None if absent."""
    result = run(['az', 'acr', 'repository', 'show', '--name', acr, '--image',
                  f'clubpenguin/{component}:{tag}', '--query', 'digest', '-o', 'tsv'],
                 check=False, quiet=True)
    if result.returncode == 0:
        return result.stdout.strip()
    message = (result.stderr + result.stdout).lower()
    if 'not found' in message or 'could not be found' in message or 'resourcenotfound' in message:
        return None
    fail('Could not query the registry: ' + lablib.redact(result.stderr.strip()[-400:]))


def resolve(args):
    root = ROOT
    cfg = lablib.load_config('registry') if not args.offline else {}
    revisions = source_revisions(root)
    dirty = dirty_paths(root)
    if dirty and not args.allow_dirty:
        fail('Tracked release inputs have uncommitted changes; commit them or pass --allow-dirty '
             '(never for a published release):\n' + '\n'.join(dirty))
    pins = parse_pins([x for x in cfg.get('CP_RELEASE_PINS', '').split(',') if x])
    pins.update(parse_pins(args.pin))
    release = {'schema': 1, 'commit': git('rev-parse', 'HEAD'), 'dirty': bool(dirty), 'components': {}}
    for name in enabled_components(root):
        entry = {'fingerprint': fingerprint(name, root, revisions, args.salt)}
        if name in pins:
            entry.update(tag=pins[name], source='pinned')
        else:
            entry.update(tag='%s-%s' % (COMPONENTS[name]['prefix'], entry['fingerprint'][:12]), source='computed')
        if args.offline:
            entry['registry_digest'] = None
        else:
            entry['registry_digest'] = registry_tag(cfg['CP_ACR_NAME'], name, entry['tag'])
        entry['needs_build'] = entry['registry_digest'] is None
        if entry['needs_build'] and entry['source'] == 'pinned' and not args.offline:
            fail(f'Pinned tag clubpenguin/{name}:{entry["tag"]} does not exist in the registry')
        release['components'][name] = entry
    write_output(release, args.out)
    for name, entry in release['components'].items():
        print('%-9s %-22s %-8s %s' % (name, entry['tag'], entry['source'],
                                       'build' if entry['needs_build'] else 'reuse ' + str(entry['registry_digest'])[:19]))


def write_output(data, out):
    if out:
        lablib.write_json(out, data)
    else:
        print(json.dumps(data, indent=2, sort_keys=True))


# --- build and test ----------------------------------------------------------------

def build(args):
    """Build what the release needs for linux/arm64 and test it in isolation.

    runtime and database are always built locally when anything is missing from the
    registry: they are small, BuildKit caches them, and the isolated tests need both.
    Only components whose tag is absent from the registry are later published; the web
    image (~1.7 GiB of media) is built only when its own tag is absent.
    """
    release = json.loads(Path(args.release).read_text())
    comps = release['components']
    pending = [n for n, e in comps.items() if e['needs_build']]
    if not pending:
        print('Every component already exists in the registry for these inputs: nothing to build or test.')
        return
    to_build = sorted(pending)
    # Reuse the exact registry artifacts, especially a pinned PostgreSQL image.
    # Rebuilding a pinned tag locally could test different base layers than the
    # image actually deployed, even when its Dockerfile has not changed.
    cfg = lablib.load_config('registry')
    reused = [name for name in comps if name not in pending]
    if reused:
        auth = json.loads(run(['az', 'acr', 'login', '--name', cfg['CP_ACR_NAME'], '--expose-token', '-o', 'json'],
                              quiet=True).stdout)
        with tempfile.TemporaryDirectory(prefix='cp-release-pull-') as directory:
            isolated_docker_config(directory, auth['loginServer'], auth['accessToken'])
            for name in reused:
                origin = f'{auth["loginServer"]}/clubpenguin/{name}@{comps[name]["registry_digest"]}'
                run(['docker', '--config', directory, 'pull', '--platform', 'linux/arm64', origin], timeout=1800, quiet=True)
                run(['docker', '--config', directory, 'tag', origin, f'clubpenguin/{name}:{comps[name]["tag"]}'], quiet=True)
    sources = sorted({s for n in to_build for s in COMPONENTS[n]['sources']})
    py = sys.executable
    run([py, str(ROOT / 'scripts/fetch-sources.py'), '--only', ','.join(sources)], timeout=3600)
    command = [py, str(ROOT / 'scripts/build-images.py'), '--platform', args.platform, '--components', ','.join(to_build)]
    for name in to_build:
        command += ['--component-tag', f'{name}={comps[name]["tag"]}']
    run(command, timeout=7200)
    require_arm64([f'clubpenguin/{n}:{comps[n]["tag"]}' for n in pending])
    test = [py, str(ROOT / 'scripts/test-containers.py'), '--tag', comps['runtime']['tag'],
            '--component-tag', f'database={comps["database"]["tag"]}', '--puffle']
    if 'web' in comps:
        test += ['--web-tag', comps['web']['tag']]
    if 'bots' in comps:
        test += ['--bots-tag', comps['bots']['tag']]
        run(['bash', str(ROOT / 'scripts/run-bot-tests.sh'),
             f'clubpenguin/runtime:{comps["runtime"]["tag"]}'], timeout=1800)
    result = run(test, timeout=1800)
    print(result.stdout.strip())
    run(['bash', str(ROOT / 'scripts/run-puffle-tests.sh'),
         f'clubpenguin/runtime:{comps["runtime"]["tag"]}'], timeout=1800)


# --- local image checks and publication ------------------------------------------

def local_platform(image):
    result = run(['docker', 'image', 'inspect', '--format', '{{.Os}}/{{.Architecture}}', image], quiet=True)
    return result.stdout.strip()


def require_arm64(images):
    for image in images:
        platform = local_platform(image)
        if platform != 'linux/arm64':
            fail(f'{image} is {platform}; deployment images must be linux/arm64')
        marker = run(['docker', 'image', 'inspect', '--format',
                      '{{index .Config.Labels "clubpenguin.test-only"}}', image], quiet=True).stdout.strip()
        if marker:
            fail(f'{image} was built for tests only ({marker}); build it with full media before publishing')
        print(f'ok  {image} is linux/arm64')


def docker_origin_config():
    return Path(os.environ.get('DOCKER_CONFIG') or (Path.home() / '.docker'))


def isolated_docker_config(directory, server, token):
    """Create a throwaway Docker config holding the push credential.

    The operator's own ~/.docker (and any registry login in it) is neither read for
    credentials nor modified: only its contexts and CLI plugins are linked in so the
    same Docker engine keeps working.
    """
    origin = docker_origin_config()
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = ''
    original_config = origin / 'config.json'
    if original_config.is_file():
        try:
            current = json.loads(original_config.read_text()).get('currentContext', '')
        except (OSError, ValueError):
            current = ''
    plugin_dirs = [str(origin / 'cli-plugins'), '/opt/homebrew/lib/docker/cli-plugins',
                   '/usr/local/lib/docker/cli-plugins', '/usr/libexec/docker/cli-plugins',
                   '/usr/lib/docker/cli-plugins']
    auth = base64.b64encode(f'00000000-0000-0000-0000-000000000000:{token}'.encode()).decode()
    config = {'auths': {server: {'auth': auth}} if server else {}, 'cliPluginsExtraDirs': plugin_dirs}
    if current:
        config['currentContext'] = current
    lablib.write_private(directory / 'config.json', json.dumps(config))
    for name in ('contexts', 'buildx'):
        if (origin / name).exists() and not (directory / name).exists():
            (directory / name).symlink_to(origin / name)
    return directory


def acr_credentials(acr):
    result = run(['az', 'acr', 'login', '--name', acr, '--expose-token', '--output', 'json'], quiet=True)
    data = json.loads(result.stdout)
    return data['loginServer'], data['accessToken']


def publish(args):
    cfg = lablib.load_config('registry')
    release = json.loads(Path(args.release).read_text())
    todo = [(n, e) for n, e in release['components'].items() if e['needs_build']]
    if not todo:
        print('Nothing to publish: every component already exists in the registry.')
        write_output(release, args.out or args.release)
        return
    for name, entry in todo:
        if not TAG_RE.fullmatch(entry['tag']) or entry['tag'] == 'latest':
            fail(f'Refusing to publish {name} with tag {entry["tag"]}')
    local = [f'clubpenguin/{n}:{e["tag"]}' for n, e in todo]
    require_arm64(local)
    server, token = acr_credentials(cfg['CP_ACR_NAME'])
    workdir = Path(tempfile.mkdtemp(prefix='cp-docker-'))
    workdir.chmod(0o700)
    try:
        config_dir = isolated_docker_config(workdir, server, token)
        env = {'DOCKER_CONFIG': str(config_dir)}
        for name, entry in todo:
            source = f'clubpenguin/{name}:{entry["tag"]}'
            target = f'{server}/{source}'
            run(['docker', 'tag', source, target], env=env)
            try:
                run(['docker', 'push', target], env=env, timeout=3600)
            finally:
                run(['docker', 'rmi', target], env=env, check=False, quiet=True)  # removes only the extra tag
            digest = registry_tag(cfg['CP_ACR_NAME'], name, entry['tag'])
            if not digest:
                fail(f'{target} is not visible in the registry after pushing')
            entry.update(registry_digest=digest, needs_build=False, published=True, platform='linux/arm64')
            print(f'published {source} {digest}')
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    write_output(release, args.out or args.release)


# --- manifest, terraform variables, blob history ---------------------------------

def build_manifest(release, run_info):
    components = {}
    for name, entry in release['components'].items():
        components[name] = {
            'tag': entry['tag'], 'fingerprint': entry['fingerprint'], 'source': entry['source'],
            'digest': entry.get('registry_digest'), 'platform': entry.get('platform', 'linux/arm64'),
            'published_by_this_run': bool(entry.get('published')),
        }
    return {'schema': 1, 'commit': release['commit'], 'created_utc': time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()),
            'run': run_info, 'components': components}


def manifest(args):
    release = json.loads(Path(args.release).read_text())
    for name, entry in release['components'].items():
        if not entry.get('registry_digest'):
            fail(f'{name} has no registry digest; publish the release first')
    run_info = {'build_id': os.environ.get('BUILD_BUILDID', 'local'),
                'build_number': os.environ.get('BUILD_BUILDNUMBER', 'local'),
                'branch': os.environ.get('BUILD_SOURCEBRANCH', git('rev-parse', '--abbrev-ref', 'HEAD'))}
    write_output(build_manifest(release, run_info), args.out)


def manifest_to_tfvars(data, public_hostname):
    """Terraform variables for a manifest (from a fresh release or a rollback target)."""
    comps = data['components']
    values = {'runtime_image_tag': comps['runtime']['tag'], 'database_image_tag': comps['database']['tag'],
              'web_image_tag': comps['web']['tag']}
    if 'bots' in comps:
        values['bots_image_tag'] = comps['bots']['tag']
    for tag in values.values():
        if not TAG_RE.fullmatch(tag) or tag == 'latest':
            fail('Manifest contains an invalid image tag')
    values['release_info'] = {
        'commit': data['commit'], 'runtime_tag': values['runtime_image_tag'],
        'database_tag': values['database_image_tag'], 'web_tag': values['web_image_tag'],
        'manifest_created_utc': data['created_utc'], 'pipeline_run': str(data.get('run', {}).get('build_id', '')),
    }
    return values


def tfvars(args):
    data = json.loads(Path(args.manifest).read_text())
    write_output(manifest_to_tfvars(data, None), args.out)


def blob_args(cfg):
    return ['--account-name', cfg['CP_STATE_STORAGE_ACCOUNT'], '--container-name', cfg['CP_BACKUP_CONTAINER'],
            '--auth-mode', 'login']


def upload_manifest(path, cfg):
    """Store a manifest named by when it was *deployed*, so "previous" means previously live."""
    data = json.loads(Path(path).read_text())
    name = 'releases/%s-%s.json' % (data.get('recorded_utc') or data['created_utc'], data['commit'][:12])
    run(['az', 'storage', 'blob', 'upload'] + blob_args(cfg) + ['--name', name, '--file', str(path), '--overwrite',
        'false', '--only-show-errors', '-o', 'none'], quiet=True)
    return name


def list_manifests(cfg):
    result = run(['az', 'storage', 'blob', 'list'] + blob_args(cfg) + ['--prefix', 'releases/', '--query', '[].name',
                 '-o', 'json'], quiet=True)
    return sorted(json.loads(result.stdout))


def fetch_manifest(cfg, name, destination):
    if not re.fullmatch(r'releases/\d{8}T\d{6}Z-[0-9a-f]{12}\.json', name):
        fail('Invalid release manifest name')
    run(['az', 'storage', 'blob', 'download'] + blob_args(cfg) + ['--name', name, '--file', str(destination),
        '--only-show-errors', '-o', 'none'], quiet=True)
    return json.loads(Path(destination).read_text())


def list_cmd(args):
    cfg = lablib.load_config('backup')
    for name in list_manifests(cfg):
        print(name)


def fetch_cmd(args):
    cfg = lablib.load_config('backup')
    names = list_manifests(cfg)
    if args.name == 'previous':
        if len(names) < 2:
            fail('There is no previous release manifest in the registry of releases')
        name = names[-2]
    elif args.name == 'latest':
        name = names[-1]
    else:
        name = args.name
    data = fetch_manifest(cfg, name, args.out)
    print(f'Fetched {name}: commit {data["commit"][:12]}, tags ' +
          ', '.join(f'{k}={v["tag"]}' for k, v in data['components'].items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('resolve', help='compute component tags and query the registry')
    p.add_argument('--out')
    p.add_argument('--salt', default='', help='change to force new tags (e.g. refreshed base images)')
    p.add_argument('--pin', action='append', metavar='COMPONENT=TAG',
                   help='keep an existing published tag (migration); repeatable')
    p.add_argument('--offline', action='store_true', help='skip registry queries (tests)')
    p.add_argument('--allow-dirty', action='store_true')
    p.set_defaults(func=resolve)
    p = sub.add_parser('publish', help='push locally built ARM64 images and record digests')
    p.add_argument('--release', required=True)
    p.add_argument('--out')
    p.set_defaults(func=publish)
    p = sub.add_parser('build', help='build the missing components for ARM64 and run the isolated tests')
    p.add_argument('--release', required=True)
    p.add_argument('--platform', default='linux/arm64')
    p.set_defaults(func=build)
    p = sub.add_parser('manifest', help='write the release manifest after publication')
    p.add_argument('--release', required=True)
    p.add_argument('--out')
    p.set_defaults(func=manifest)
    p = sub.add_parser('tfvars', help='derive Terraform variables from a manifest')
    p.add_argument('--manifest', required=True)
    p.add_argument('--out')
    p.set_defaults(func=tfvars)
    sub.add_parser('list', help='list recorded release manifests').set_defaults(func=list_cmd)
    p = sub.add_parser('fetch', help='download a recorded manifest (name, "latest" or "previous")')
    p.add_argument('name')
    p.add_argument('--out', required=True)
    p.set_defaults(func=fetch_cmd)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

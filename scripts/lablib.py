"""Shared helpers for the lab's CI/CD scripts (standard library only, Python 3.9+).

Three concerns live here so the other scripts stay small:

* private configuration: deployment values come from environment variables or an
  agent-local file, never from committed code (the repository is anonymized);
* a per-run workspace with mode 0700, used for plans, Terraform data and evidence;
* subprocess and logging helpers that avoid echoing credentials.
"""
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Values that the anonymization put in the repository. A real deployment must not
# silently run against them.
SAMPLE_MARKERS = ('example.com', 'example.org', '00000000-0000-0000-0000-000000000000')
SAMPLE_VALUES = {'192.168.0.10', '192.168.0.0/24'}

HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
IP_OR_HOST_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')
USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')
AZURE_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,62}$')
RG_RE = re.compile(r'^[\w.()-]{1,90}$')
TAG_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')
BLOB_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$')
KEY_RE = re.compile(r'^[A-Za-z0-9_./-]{1,200}$')
CIDR_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}/(\d|[12]\d|3[0-2])$')
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')
RUN_DIR_RE = re.compile(r'^run-[A-Za-z0-9]+-[A-Za-z0-9]+$')

# name -> (validator, default, description). default None means "no default".
CONFIG_SPEC = {
    'CP_PUBLIC_HOSTNAME': (HOSTNAME_RE, None, 'Public hostname served through the tunnel'),
    'CP_PI_HOST': (IP_OR_HOST_RE, None, 'Address of the Raspberry Pi on the LAN'),
    'CP_PI_USER': (USER_RE, None, 'SSH user on the Raspberry Pi'),
    'CP_LAN_CIDR': (CIDR_RE, None, 'LAN range allowed to reach the game ports (network policy game-ingress)'),
    'CP_ACR_NAME': (AZURE_NAME_RE, None, 'Name of the existing Azure Container Registry'),
    'CP_STATE_RESOURCE_GROUP': (RG_RE, None, 'Resource group of the Terraform state account'),
    'CP_STATE_STORAGE_ACCOUNT': (BLOB_RE, None, 'Storage account for Terraform state and backups'),
    'CP_STATE_CONTAINER': (BLOB_RE, 'tfstate-clubpenguin', 'Blob container for Terraform state'),
    'CP_CLUSTER_STATE_KEY': (KEY_RE, 'cluster.tfstate', 'State blob of infra/cluster'),
    'CP_CLOUD_STATE_KEY': (KEY_RE, 'cloud.tfstate', 'State blob of infra/cloud'),
    'CP_BACKUP_CONTAINER': (BLOB_RE, 'clubpenguin-backups', 'Blob container for backups and releases'),
    'CP_NODE_NAME': (IP_OR_HOST_RE, 'penguin-pi', 'Expected Kubernetes node name'),
    'CP_PUFFLE_ENABLED': (re.compile(r'^(true|false)$'), 'false', 'Explicit puffle production capability'),
    'CP_PUFFLE_BROKER_REPLICAS': (re.compile(r'^[01]$'), '1', 'Retain the broker PVC while stopping a legacy rollback'),
    'CP_PUFFLE_ALLOWED_PLAYERS': (re.compile(r'^(\d+(,\d+)*)?$'), '', 'Optional pilot player IDs; empty permits voluntary pairing'),
    'CP_RELEASE_PINS': (re.compile(r'^(?:(?:runtime|database|web|bots)=[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(?:,(?:runtime|database|web|bots)=[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})*)?$'), '', 'Explicit image tags retained across automatic releases'),
    'CP_KUBECONFIG': (None, '~/.kube/clubpenguin-pi.yaml', 'Dedicated kubeconfig for the Pi'),
    'CP_SSH_CONFIG': (None, '', 'Optional ssh_config used instead of ~/.ssh/config'),
    'CP_AZURE_SUBSCRIPTION_ID': (UUID_RE, '', 'If set, the Azure login must be this subscription'),
}

# Which keys each phase needs. A missing key fails fast, before anything is built.
PHASE_KEYS = {
    'agent': ['CP_PI_HOST', 'CP_PI_USER'],
    'registry': ['CP_ACR_NAME'],
    'state': ['CP_STATE_RESOURCE_GROUP', 'CP_STATE_STORAGE_ACCOUNT'],
    'plan': ['CP_PUBLIC_HOSTNAME', 'CP_ACR_NAME', 'CP_STATE_RESOURCE_GROUP', 'CP_STATE_STORAGE_ACCOUNT',
             'CP_PI_HOST', 'CP_PI_USER', 'CP_LAN_CIDR'],
    'backup': ['CP_STATE_STORAGE_ACCOUNT'],
}


def fail(message):
    if not os.environ.get('CP_TEST_QUIET'):  # unit tests exercise failures on purpose
        print('ERROR: ' + message, file=sys.stderr)
    raise SystemExit(1)


def _read_env_file(path):
    """Parse KEY=VALUE lines. No shell evaluation, no command substitution."""
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        fail(f'{path} must be owned by the current user with mode 0600')
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            fail(f'{path}:{number}: expected KEY=VALUE')
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not re.fullmatch(r'CP_[A-Z0-9_]+', key):
            fail(f'{path}:{number}: only CP_* keys are accepted')
        values[key] = value
    return values


def default_config_file():
    explicit = os.environ.get('CP_CONFIG_FILE')
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / '.config/clubpenguin-lab/deploy.env'


def load_config(phase=None, keys=(), environ=None, config_file=None):
    """Return validated private configuration.

    Precedence: environment variables, then the agent-local file, then defaults.
    ``phase`` selects the keys that must be present; ``keys`` adds more.
    Sample values left by the anonymization are rejected.
    """
    environ = os.environ if environ is None else environ
    merged = {}
    path = config_file if config_file is not None else default_config_file()
    if path and Path(path).is_file():
        merged.update(_read_env_file(Path(path)))
    for name in CONFIG_SPEC:
        if environ.get(name):
            merged[name] = environ[name]
    needed = set(PHASE_KEYS.get(phase, [])) | set(keys)
    result = {}
    problems = []
    allow_sample = environ.get('CP_ALLOW_SAMPLE_VALUES') == '1'
    for name, (validator, default, description) in CONFIG_SPEC.items():
        value = merged.get(name, default)
        if value in (None, ''):
            if name in needed:
                problems.append(f'{name} is required ({description})')
            result[name] = value or ''
            continue
        if validator is not None and not validator.fullmatch(value):
            problems.append(f'{name} has an invalid format')
        if not allow_sample and (value in SAMPLE_VALUES or any(m in value for m in SAMPLE_MARKERS)):
            problems.append(f'{name} still holds an anonymized sample value; set the real one privately')
        result[name] = value
    if problems:
        fail('Private configuration is incomplete:\n  - ' + '\n  - '.join(problems) +
             '\nProvide it through pipeline variables or ~/.config/clubpenguin-lab/deploy.env '
             '(see docs/ci-cd/configuracion.md).')
    result['CP_KUBECONFIG'] = str(Path(result['CP_KUBECONFIG']).expanduser())
    if result['CP_SSH_CONFIG']:
        result['CP_SSH_CONFIG'] = str(Path(result['CP_SSH_CONFIG']).expanduser())
    return result


def backend_config(cfg, root_name):
    """Backend settings for infra/<root_name> as a dict (written to a 0600 file)."""
    key = cfg['CP_CLUSTER_STATE_KEY'] if root_name == 'cluster' else cfg['CP_CLOUD_STATE_KEY']
    return {
        'resource_group_name': cfg['CP_STATE_RESOURCE_GROUP'],
        'storage_account_name': cfg['CP_STATE_STORAGE_ACCOUNT'],
        'container_name': cfg['CP_STATE_CONTAINER'],
        'key': key,
        'use_azuread_auth': True,
    }


def render_hcl(values):
    lines = []
    for key, value in values.items():
        if isinstance(value, bool):
            lines.append(f'{key} = {str(value).lower()}')
        else:
            lines.append(f'{key} = {json.dumps(str(value))}')
    return '\n'.join(lines) + '\n'


def write_private(path, text):
    """Create a file readable only by the current user; refuse to follow symlinks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail(f'{path} is a symlink')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    path.chmod(0o600)


def write_json(path, data, private=False):
    text = json.dumps(data, indent=2, sort_keys=True) + '\n'
    if private:
        write_private(path, text)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + '.tmp')
        tmp.write_text(text)
        tmp.replace(path)


class RunDir:
    """Private workspace for one pipeline run: plan, Terraform data and evidence.

    The name is derived from the Azure DevOps build id and attempt so the plan job
    and the apply job (separate jobs, same agent) find the same directory, while two
    runs never share one.
    """

    def __init__(self, base=None, run_id=None, create=True):
        base = Path(base or os.environ.get('CP_RUNS_DIR') or (Path.home() / '.clubpenguin-lab/runs'))
        self.base = base.expanduser()
        self.name = run_id or self.default_name()
        if not RUN_DIR_RE.fullmatch(self.name):
            fail('Invalid run identifier')
        self.path = self.base / self.name
        if create:
            self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.base.chmod(0o700)
            self.path.mkdir(exist_ok=True, mode=0o700)
            self.path.chmod(0o700)
            for sub in ('plan', 'tfdata', 'evidence', 'config'):
                (self.path / sub).mkdir(exist_ok=True, mode=0o700)

    @staticmethod
    def default_name():
        build = os.environ.get('BUILD_BUILDID')
        if build:
            # One directory per pipeline run, whatever the job attempt: re-running a failed
            # job of the same run finds (or safely replaces) the plan it belongs to.
            return f'run-{build}-r'
        return 'run-local-' + time.strftime('%Y%m%d%H%M%S') + secrets.token_hex(2)

    def sub(self, name):
        return self.path / name

    def remove(self):
        """Delete only this run's directory, and only if it is where we created it."""
        target = self.path
        if target.is_symlink() or not target.exists():
            return False
        if target.parent != self.base or not RUN_DIR_RE.fullmatch(target.name):
            fail('Refusing to remove a directory outside the run workspace')
        shutil.rmtree(target)
        return True

    def prune_stale(self, days=3):
        """Remove abandoned run directories (cancelled pipelines) older than ``days``."""
        removed = []
        if not self.base.is_dir():
            return removed
        cutoff = time.time() - days * 86400
        for entry in self.base.iterdir():
            if entry.is_symlink() or not entry.is_dir() or not RUN_DIR_RE.fullmatch(entry.name):
                continue
            if entry.name == self.name or entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry)
            removed.append(entry.name)
        return removed


SECRET_PATTERNS = [
    re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_.-]+'),            # JWTs (cloudflared, OIDC, AAD)
    re.compile(r'(?i)(password|passwd|token|secret|apikey|api_key)(["\']?\s*[:=]\s*["\']?)[^\s"\',;]{4,}'),
    re.compile(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}'),
    re.compile(r'sk-[A-Za-z0-9]{16,}'),
]


def redact(text):
    """Best-effort masking for text that is about to be shown or stored as evidence."""
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + m.group(2) + '***', text)
        else:
            text = pattern.sub('***', text)
    return text


def run(command, check=True, timeout=None, env=None, input_text=None, cwd=None, quiet=False):
    """Run a command, capturing output. Never echoes the environment."""
    if not quiet:
        print('+ ' + ' '.join(str(part) for part in command[:6]) + (' …' if len(command) > 6 else ''), flush=True)
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        result = subprocess.run([str(part) for part in command], capture_output=True, text=True, timeout=timeout,
                                env=merged, input=input_text, cwd=cwd)
    except subprocess.TimeoutExpired:
        fail(f'Timed out after {timeout}s: {command[0]} {command[1] if len(command) > 1 else ""}')
    if check and result.returncode != 0:
        tail = redact((result.stderr or result.stdout or '')[-1500:])
        fail(f'{command[0]} failed with exit code {result.returncode}\n{tail}')
    return result


def set_pipeline_variable(name, value, output=True):
    """Publish a non-secret value to later pipeline steps/jobs (Azure DevOps logging command)."""
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_.:/=+@ -]{0,300}', value):
        fail(f'Refusing to publish pipeline variable {name}: unexpected characters')
    flag = ';isOutput=true' if output else ''
    print(f'##vso[task.setvariable variable={name}{flag}]{value}', flush=True)


def log_issue(kind, message):
    """Show a warning or error in the Azure DevOps UI (and plainly elsewhere)."""
    print(f'##vso[task.logissue type={kind}]{redact(message)}', flush=True)


def git(*args, check=True):
    return run(['git', '-C', str(ROOT)] + list(args), check=check, quiet=True).stdout.strip()

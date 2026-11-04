#!/usr/bin/env python3
"""Install the official GitHub ARM64 runner as this operator's LaunchAgent."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import urllib.request

REPO = 'AlejoReyna/ClubPenguinFork'
ROOT = Path(__file__).resolve().parents[1]


def run(command, env=None):
    location = str(Path(command[0]).parent) if command[0].endswith(('/config.sh', '/svc.sh')) else None
    result = subprocess.run(command, capture_output=True, text=True, env=env, cwd=location)
    if result.returncode:
        raise SystemExit('Runner setup failed: ' + ' '.join(command[:2]) + ' (credentials withheld)')
    return result.stdout


def main():
    directory = Path.home() / '.clubpenguin-lab/github-runner'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (directory / '.runner').is_file():
        data = json.loads(run(['gh', 'api', f'repos/{REPO}/actions/runners/downloads']))
        package = next(x for x in data if x['os'] == 'osx' and x['architecture'] == 'arm64')
        archive = directory / 'runner.tar.gz'
        urllib.request.urlretrieve(package['download_url'], archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != package['sha256_checksum']:
            raise SystemExit('Runner archive checksum mismatch')
        with tarfile.open(archive) as bundle:
            # The archive is checksum-verified from GitHub's authenticated API.
            for member in bundle.getmembers():
                if not (directory / member.name).resolve().is_relative_to(directory.resolve()):
                    raise SystemExit('Invalid runner archive')
            bundle.extractall(directory, filter='data')
        archive.unlink()
        token = json.loads(run(['gh', 'api', '-X', 'POST', f'repos/{REPO}/actions/runners/registration-token']))['token']
        env = dict(os.environ)
        env['PATH'] = str(ROOT / '.local/tools/ansible/bin') + ':' + str(ROOT / '.local/tools') + ':' + env['PATH']
        # config.sh records PATH for launchd; credentials never enter command logs.
        run([str(directory / 'config.sh'), '--url', 'https://github.com/' + REPO, '--token', token,
             '--name', 'clubpenguin-mac', '--labels', 'clubpenguin-lab', '--work', '_work', '--unattended'], env=env)
    if not (directory / '.service').exists():
        run([str(directory / 'svc.sh'), 'install'])
    run([str(directory / 'svc.sh'), 'start'])
    print('Official macOS ARM64 runner installed and started; deployment remains explicitly gated.')


if __name__ == '__main__':
    main()

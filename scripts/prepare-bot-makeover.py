#!/usr/bin/env python3
"""Create a private candidate configuration for the existing work accounts.

This is a local preparation step: it does not contact Kubernetes or a provider.
The destination must not already exist; the original file remains untouched.
"""
import argparse
import copy
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/bots'))
from bots.config import build_config

PROFILES = {
    'baristaia': ('Juanito', ['Barista'], 110),
    'djia': ('DJ Cobra', ['DJ', 'Cobra'], 120),
    'pizzeroia': ('Viterbos', ['Pizzero'], 330),
}


def candidate(document):
    updated = copy.deepcopy(document)
    bots = updated.get('bots')
    if not isinstance(bots, list) or len(bots) != 3:
        raise ValueError('Expected exactly three work accounts')
    names = [str(bot.get('username', '')).lower() for bot in bots]
    if len(set(names)) != 3 or set(names) != set(PROFILES):
        raise ValueError('Work account identities do not match')
    for bot, username in zip(bots, names):
        nickname, aliases, room = PROFILES[username]
        if bot.get('room') != room or 'walk' not in bot:
            raise ValueError('Work account room or movement area does not match')
        bot['nickname'] = nickname
        bot['aliases'] = aliases[:]
    build_config(updated, {})
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default=str(ROOT / '.local/bots/work-credentials.json'))
    parser.add_argument('--output', default=str(ROOT / '.local/bots/work-makeover.json'))
    args = parser.parse_args()
    source, target = Path(args.input).expanduser(), Path(args.output).expanduser()
    local = (ROOT / '.local').resolve()
    if (source.is_symlink() or target.is_symlink() or
            not source.resolve().is_relative_to(local) or not target.resolve().is_relative_to(local)):
        parser.error('Both files must be inside .local and not symlinks')
    info = source.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        parser.error('Input must be a private file owned by the current user')
    result = candidate(json.loads(source.read_text()))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result, output, indent=2, ensure_ascii=False)
        output.write('\n')
    print('Private candidate created; original preserved. No remote changes or provider calls.')


if __name__ == '__main__':
    main()

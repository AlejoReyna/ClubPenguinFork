#!/usr/bin/env python3
"""Prepare a private candidate configuration without contacting the lab."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_accounts

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--areas-file', help='JSON overriding the measured rectangles in bot_accounts.MEASURED_WORK_AREAS')
    parser.add_argument('--output', default=str(ROOT / '.local/bots/work-credentials.json'))
    parser.add_argument('--cast', help='non-secret cast JSON file or directory; prepare N new accounts')
    parser.add_argument('--base-credentials', help='private existing configuration to preserve when adding a cast')
    args = parser.parse_args()
    output = Path(args.output).expanduser()
    if output.is_symlink() or not output.resolve().is_relative_to((ROOT / '.local').resolve()):
        parser.error('Output must be a non-symlink path inside .local')
    if args.cast and args.areas_file:
        parser.error('--cast uses only the measured room catalog')
    document = bot_accounts.cast_config_document(args.cast) if args.cast else bot_accounts.work_config_document(
        json.loads(Path(args.areas_file).read_text()) if args.areas_file else None)
    if args.base_credentials:
        if not args.cast:
            parser.error('--base-credentials requires --cast')
        base = Path(args.base_credentials).expanduser()
        if base.is_symlink() or base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
            parser.error('Base credentials must be private, owned by this user and not a symlink')
        old = json.loads(base.read_text())
        names = {bot['username'].lower() for bot in old['bots']}
        if names & {bot['username'] for bot in document['bots']}:
            parser.error('New cast overlaps existing credentials; no identities replaced')
        old['bots'].extend(document['bots'])
        document = old
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as target:
        json.dump(document, target, indent=2, ensure_ascii=False)
        target.write('\n')
    print('Private candidate created; no account or Secret was changed.')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Prepare a private candidate configuration without contacting the lab."""
import argparse
import json
import os
from pathlib import Path

import bot_accounts

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--areas-file', help='JSON overriding the measured rectangles in bot_accounts.MEASURED_WORK_AREAS')
    parser.add_argument('--output', default=str(ROOT / '.local/bots/work-credentials.json'))
    args = parser.parse_args()
    output = Path(args.output).expanduser()
    if output.is_symlink() or not output.resolve().is_relative_to((ROOT / '.local').resolve()):
        parser.error('Output must be a non-symlink path inside .local')
    document = bot_accounts.work_config_document(
        json.loads(Path(args.areas_file).read_text()) if args.areas_file else None)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as target:
        json.dump(document, target, indent=2, ensure_ascii=False)
        target.write('\n')
    print('Private candidate created; no account or Secret was changed.')


if __name__ == '__main__':
    main()

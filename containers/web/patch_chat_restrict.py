#!/usr/bin/env python3
"""Allow ``/`` in the in-game chat field (dev ``/party`` and similar commands).

The CP client sets ``TextField.restrict`` from the ``chat_restrict`` string in each
locale's ``game_strings.json`` / ``game_strings.jsonp``. The stock dump omits ``/``.
This script patches those files in place under ``media/play/<lang>/web_service/``.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

LANGS = ('de', 'en', 'es', 'fr', 'pt')
SLASH_TOKEN = ' /'


def patch_lang_table(table: list) -> bool:
    changed = False
    for row in table:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or row[0] != 'chat_restrict':
            continue
        value = row[1]
        if not isinstance(value, str):
            raise ValueError('chat_restrict is not a string')
        if '/' in value:
            return False
        row[1] = value.rstrip() + SLASH_TOKEN
        changed = True
        break
    if not changed:
        raise ValueError('chat_restrict entry missing')
    return True


def patch_json(path: Path) -> bool:
    data = json.loads(path.read_text(encoding='utf-8'))
    table = data.get('lang')
    if not isinstance(table, list):
        raise ValueError(f'{path}: missing lang table')
    if not patch_lang_table(table):
        return False
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    return True


def patch_jsonp(path: Path) -> bool:
    raw = path.read_text(encoding='utf-8')
    match = re.fullmatch(r'cp_game_strings\((.*)\)\s*;?\s*', raw, flags=re.DOTALL)
    if not match:
        raise ValueError(f'{path}: unexpected jsonp wrapper')
    data = json.loads(match.group(1))
    table = data.get('lang')
    if not isinstance(table, list):
        raise ValueError(f'{path}: missing lang table')
    if not patch_lang_table(table):
        return False
    body = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    path.write_text(f'cp_game_strings({body});\n', encoding='utf-8')
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('media_root', type=Path, help='directory that contains play/<lang>/…')
    args = parser.parse_args()
    root = args.media_root.resolve()
    touched = 0
    roots = [root / 'play' / lang / 'web_service' / 'game_configs' for lang in LANGS]
    roots.append(root / 'play' / 'web_service' / 'game_configs')
    for base in roots:
        for name in ('game_strings.json', 'game_strings.jsonp'):
            path = base / name
            if not path.is_file():
                continue
            if name.endswith('.json'):
                if patch_json(path):
                    touched += 1
            else:
                if patch_jsonp(path):
                    touched += 1
    print(f'Patched chat_restrict (+ slash) in {touched} file(s) under {root}')


if __name__ == '__main__':
    main()
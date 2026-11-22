#!/usr/bin/env python3
"""Allow ``/`` in the in-game chat field (dev ``/party`` and similar commands).

The CP client sets ``TextField.restrict`` from the ``chat_restrict`` string in each
locale's ``game_strings.json`` / ``game_strings.jsonp`` (and inside the zipped
``game_configs.bin`` bundle loaded by the 2017 AS3 wrapper). The stock dump omits ``/``.
This script patches those files in place under ``media/play/<lang>/web_service/``
and ``play/<lang>/web_service/``.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import zipfile
from pathlib import Path

LANGS = ('de', 'en', 'es', 'fr', 'pt', 'ru')
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


def patch_json_text(raw: str, source: Path) -> str | None:
    data = json.loads(raw)
    table = data.get('lang')
    if not isinstance(table, list):
        raise ValueError(f'{source}: missing lang table')
    if not patch_lang_table(table):
        return None
    return json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n'


def patch_json(path: Path) -> bool:
    patched = patch_json_text(path.read_text(encoding='utf-8'), path)
    if patched is None:
        return False
    path.write_text(patched, encoding='utf-8')
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


def patch_zip_bundle(path: Path) -> bool:
    with zipfile.ZipFile(path, 'r') as src:
        names = src.namelist()
        if 'game_strings.json' not in names:
            return False
        raw = src.read('game_strings.json').decode('utf-8')
        patched = patch_json_text(raw, path)
        if patched is None:
            return False
        entries = [(info, src.read(info.filename)) for info in src.infolist()]
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.game_configs-', suffix='.bin', delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_DEFLATED) as dst:
            for info, payload in entries:
                if info.filename == 'game_strings.json':
                    payload = patched.encode('utf-8')
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = info.compress_type
                entry.external_attr = info.external_attr
                dst.writestr(entry, payload)
        tmp_path.chmod(path.stat().st_mode & 0o777)
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return True


def candidate_play_roots(root: Path) -> list[Path]:
    play_roots = []
    for candidate in (root / 'play', root / 'media' / 'play'):
        if candidate.is_dir() and candidate not in play_roots:
            play_roots.append(candidate)
    return play_roots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('media_root', type=Path, help='directory that contains play/<lang>/… or media/play/<lang>/…')
    args = parser.parse_args()
    root = args.media_root.resolve()
    touched = 0
    for play_root in candidate_play_roots(root):
        service_dirs = [play_root / lang / 'web_service' for lang in LANGS]
        service_dirs.append(play_root / 'web_service')
        for service_dir in service_dirs:
            configs_dir = service_dir / 'game_configs'
            for name in ('game_strings.json', 'game_strings.jsonp'):
                path = configs_dir / name
                if not path.is_file():
                    continue
                if name.endswith('.json'):
                    if patch_json(path):
                        touched += 1
                else:
                    if patch_jsonp(path):
                        touched += 1
            bundle = service_dir / 'game_configs.bin'
            if bundle.is_file() and patch_zip_bundle(bundle):
                touched += 1
    print(f'Patched chat_restrict (+ slash) in {touched} file(s) under {root}')


if __name__ == '__main__':
    main()

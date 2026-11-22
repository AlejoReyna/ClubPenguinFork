import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'containers/web/patch_chat_restrict.py'
VENDOR = ROOT / 'vendor/vanilla-media/media/play/es/web_service/game_configs'
SAMPLE_STRINGS = json.dumps({
    'lang': [
        ['chat_restrict', 'a-z A-Z !?¿¡'],
        ['other_key', 'hello'],
    ]
}, ensure_ascii=False)


class PatchChatRestrictTest(unittest.TestCase):
    def test_adds_slash_to_json_jsonp_and_game_configs_bin_under_play_and_media_play(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix in (root / 'play' / 'es' / 'web_service', root / 'media' / 'play' / 'es' / 'web_service'):
                base = prefix / 'game_configs'
                base.mkdir(parents=True)
                if (VENDOR / 'game_strings.json').is_file():
                    for name in ('game_strings.json', 'game_strings.jsonp'):
                        shutil.copy2(VENDOR / name, base / name)
                    json_bytes = (VENDOR / 'game_strings.json').read_bytes()
                else:
                    (base / 'game_strings.json').write_text(SAMPLE_STRINGS, encoding='utf-8')
                    (base / 'game_strings.jsonp').write_text(f'cp_game_strings({SAMPLE_STRINGS});\n', encoding='utf-8')
                    json_bytes = SAMPLE_STRINGS.encode('utf-8')
                with zipfile.ZipFile(prefix / 'game_configs.bin', 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
                    bundle.writestr('game_strings.json', json_bytes)
                    bundle.writestr('rooms.json', b'{}')

            subprocess.run([sys.executable, str(PATCH), str(root)], check=True, cwd=ROOT)

            for prefix in (root / 'play' / 'es' / 'web_service', root / 'media' / 'play' / 'es' / 'web_service'):
                base = prefix / 'game_configs'
                data = json.loads((base / 'game_strings.json').read_text(encoding='utf-8'))
                chat_restrict = dict(data['lang']).get('chat_restrict', '')
                self.assertIn('/', chat_restrict)
                jsonp = (base / 'game_strings.jsonp').read_text(encoding='utf-8')
                self.assertTrue(jsonp.startswith('cp_game_strings('))
                self.assertIn(' /"', jsonp)
                with zipfile.ZipFile(prefix / 'game_configs.bin', 'r') as bundle:
                    zipped = json.loads(bundle.read('game_strings.json').decode('utf-8'))
                    self.assertIn('/', dict(zipped['lang']).get('chat_restrict', ''))
                    self.assertEqual(bundle.read('rooms.json'), b'{}')


if __name__ == '__main__':
    unittest.main()

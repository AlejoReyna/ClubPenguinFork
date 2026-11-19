import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'containers/web/patch_chat_restrict.py'
VENDOR = ROOT / 'vendor/vanilla-media/media/play/es/web_service/game_configs'


class PatchChatRestrictTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (VENDOR / 'game_strings.json').is_file():
            raise unittest.SkipTest('vendor media missing')

    def test_adds_slash_to_json_and_jsonp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'play' / 'es' / 'web_service' / 'game_configs'
            base.mkdir(parents=True)
            for name in ('game_strings.json', 'game_strings.jsonp'):
                shutil.copy2(VENDOR / name, base / name)
            subprocess.run([sys.executable, str(PATCH), str(root)], check=True, cwd=ROOT)
            data = json.loads((base / 'game_strings.json').read_text(encoding='utf-8'))
            for key, value in data['lang']:
                if key == 'chat_restrict':
                    self.assertIn('/', value)
                    break
            else:
                self.fail('chat_restrict missing')
            jsonp = (base / 'game_strings.jsonp').read_text(encoding='utf-8')
            self.assertTrue(jsonp.startswith('cp_game_strings('))


if __name__ == '__main__':
    unittest.main()
import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('bot_makeover', ROOT / 'scripts/prepare-bot-makeover.py')
makeover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(makeover)

import bot_accounts
from bots.config import build_config
from bots.text import mentions


class BotMakeoverTest(unittest.TestCase):
    def test_existing_credentials_and_settings_are_preserved(self):
        original = bot_accounts.work_config_document()
        snapshot = copy.deepcopy(original)
        result = makeover.candidate(original)
        self.assertEqual(original, snapshot)
        for old, new in zip(original['bots'], result['bots']):
            self.assertEqual({k: v for k, v in old.items() if k not in ('nickname', 'aliases')},
                             {k: v for k, v in new.items() if k not in ('nickname', 'aliases')})
        config = build_config(result, {})
        self.assertEqual([bot.nickname for bot in config.bots], ['Juanito', 'Cadence', 'Viterbos'])
        for alias, expected in [('Juanito', 'Juanito'), ('Barista', 'Juanito'),
                                ('Cadence', 'Cadence'), ('DJ', 'Cadence'),
                                ('Cobra', 'Cadence'), ('DJ Cobra', 'Cadence'),
                                ('Viterbos', 'Viterbos'),
                                ('Pizzero', 'Viterbos')]:
            self.assertEqual([bot.nickname for bot in config.bots
                              if mentions(alias + ' hola', bot.mention_names)], [expected])

    def test_unknown_or_wrong_room_rejected(self):
        original = bot_accounts.work_config_document()
        original['bots'][1]['username'] = 'stranger'
        with self.assertRaises(ValueError):
            makeover.candidate(original)
        original = bot_accounts.work_config_document()
        original['bots'][1]['room'] = 110
        with self.assertRaises(ValueError):
            makeover.candidate(original)

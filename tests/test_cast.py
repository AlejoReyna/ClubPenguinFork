"""Persona validation, opt-in credentials join and the unchanged legacy path."""
import json
from pathlib import Path
import tempfile
import unittest

from bots.cast import CastError, load_cards, parse_card
from bots.config import ConfigError, build_config
from support import config_document_world

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / 'containers/bots/cast'


class CastTests(unittest.TestCase):
    def test_planned_roster_is_23_new_characters(self):
        cards = load_cards(CARDS)
        full = [card for card in cards.values() if not card.is_role_note()]
        notes = [card for card in cards.values() if card.is_role_note()]
        self.assertEqual(len(full), 23)
        self.assertEqual(sorted(card.username for card in notes), ['baristaia', 'djia', 'pizzeroia'])
        self.assertTrue(all(card.role == 'helper' for card in notes))
        self.assertEqual(cards['peperina'].role, 'helper')
        self.assertEqual(cards['pacopregon'].role, 'guide')
        counts = {}
        for card in full:
            self.assertEqual(card.missing_for_new_character(), [])
            self.assertTrue(card.nickname.isascii())
            self.assertLessEqual(len(card.nickname), 30)
            self.assertEqual(card.appearance_items, ())
            counts[card.home_room] = counts.get(card.home_room, 0) + 1
        self.assertEqual(counts, {120: 3, 121: 2, 802: 2, 300: 1, 330: 1,
                                  310: 1, 340: 4, 320: 3, 230: 5, 800: 1})

    def test_cast_is_an_explicit_opt_in_and_unmatched_credentials_stay_legacy(self):
        document = config_document_world(6112)
        first = build_config(document, {})
        second = build_config(document, {'BOTS_CAST_DIR': str(CARDS)})
        for before, after in zip(first.bots, second.bots):
            self.assertEqual(vars(before), vars(after))
            self.assertIsNone(after.card)
        self.assertEqual(len(second.cast_unmatched), 26)

    def test_card_joins_only_its_credentials(self):
        document = {'bots': [{'username': 'lolaritmo', 'password': 'synthetic-only'}]}
        config = build_config(document, {'BOTS_CAST_DIR': str(CARDS)})
        bot = config.bots[0]
        self.assertEqual((bot.nickname, bot.room), ('Lola Ritmo', 120))
        self.assertEqual(bot.password, 'synthetic-only')
        self.assertIsNotNone(bot.card)
        self.assertIn('competitiva', bot.personality)
        self.assertTrue(config.catalog.require(120).allows(*bot.spawn))

    def test_no_card_preserves_stationary_anchor_walk_and_personality(self):
        doc = {'bots': [{'username': 'djia', 'password': 'synthetic-only',
                         'nickname': 'DJ Cobra', 'aliases': ['Cobra'], 'room': 120,
                         'personality': 'DJ original', 'stationary': True,
                         'anchor': {'x': 322, 'y': 168}, 'repeat_action': 11,
                         'music': False}]}
        before = build_config(doc, {}).bots[0]
        after = build_config(doc, {'BOTS_CAST_DIR': str(CARDS)}).bots[0]
        before_fields, after_fields = vars(before), vars(after)
        self.assertIsNone(before_fields.pop('role'))
        self.assertEqual(after_fields.pop('role'), 'helper')
        self.assertIsNone(after.card)
        self.assertEqual(before_fields, after_fields)

    def test_explicit_empty_aliases_override_and_missing_music_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cast.json'
            path.write_text(json.dumps({'username': 'guiaia', 'home_room': 110, 'aliases': []}))
            doc = {'bots': [{'username': 'guiaia', 'password': 'synthetic-only',
                             'room': 100, 'aliases': ['Old'], 'personality': 'Original', 'music': False}]}
            bot = build_config(doc, {'BOTS_CAST_DIR': temp}).bots[0]
            self.assertEqual(bot.personality, 'Original')
            self.assertFalse(bot.music)
            self.assertEqual(bot.aliases, [])

    def test_disabled_card_opens_no_session(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'off.json').write_text(json.dumps({'username': 'guiaia', 'enabled': False}))
            config = build_config(config_document_world(6112), {'BOTS_CAST_DIR': temp})
            self.assertEqual([b.username for b in config.bots], ['pescadoria'])

    def test_bad_cards_fail_with_an_operator_error(self):
        for update in ({'password': 'never-in-a-card'}, {'nickname': 'Niño'},
                       {'nickname': 'x' * 31}, {'username': 'a'}, {'home_room': -1},
                       {'idle_actions': ['shell']}, {'schedule': 'not-a-list'},
                       {'fallbacks': ['visita example.com']}, {'relationships': {}},
                       {'role': 'mascot'}, {'role': ''},
                       {'schedule': [{'from': '25:00', 'to': '02:00', 'room': 120, 'activity': 'dance'}]}):
            with self.subTest(update=update), self.assertRaises(CastError):
                parse_card(dict({'username': 'testbot'}, **update))

    def test_name_collisions_and_unknown_home_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cast.json'
            path.write_text(json.dumps([{'username': 'bota', 'nickname': 'Lola'},
                                        {'username': 'botb', 'aliases': ['Lola']}]))
            with self.assertRaises(CastError):
                load_cards(path)
            path.write_text(json.dumps({'username': 'guiaia', 'home_room': 99999}))
            with self.assertRaises(ConfigError):
                build_config(config_document_world(6112), {'BOTS_CAST_DIR': temp})

    def test_schedule_wraps_at_midnight(self):
        block = parse_card({'username': 'testbot', 'schedule': [
            {'from': '18:00', 'to': '02:00', 'room': 120, 'activity': 'dance'}]}).schedule[0]
        self.assertTrue(block.covers(19 * 60))
        self.assertTrue(block.covers(60))
        self.assertFalse(block.covers(12 * 60))

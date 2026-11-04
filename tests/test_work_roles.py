import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bot_accounts
from bots.config import ConfigError, build_config
from bots.text import mentions


class WorkRolesTest(unittest.TestCase):
    def setUp(self):
        # Synthetic protocol coordinates, not claims about the room artwork.
        self.areas = {name: {'min_x': 100, 'max_x': 120, 'min_y': 150, 'max_y': 170}
                      for name, *_ in bot_accounts.WORK_ROLES}

    def test_profiles_are_distinct_and_route_mentions(self):
        config = build_config(bot_accounts.work_config_document(self.areas), {})
        self.assertEqual([b.nickname for b in config.bots], ['BaristaIA', 'DJIA', 'PizzeroIA'])
        self.assertEqual([b.room for b in config.bots], [110, 120, 330])
        for bot, alias in zip(config.bots, ['Barista', 'DJ', 'Pizzero']):
            self.assertEqual([b.nickname for b in config.bots if mentions(alias + ' hola', b.mention_names)],
                             [bot.nickname])
        self.assertIn('No afirmes haber cambiado la musica', config.bots[1].personality)

    def test_missing_or_invalid_areas_rejected(self):
        with self.assertRaises(ValueError):
            bot_accounts.work_config_document({})
        self.areas['DJIA']['max_x'] = -1
        with self.assertRaises(ConfigError):
            bot_accounts.work_config_document(self.areas)

    def test_migration_requires_a_fully_stopped_deployment(self):
        self.assertTrue(bot_accounts.deployment_is_stopped({'spec': {'replicas': 0}, 'status': {'replicas': 0}}))
        self.assertTrue(bot_accounts.deployment_is_stopped({'spec': {'replicas': 0}, 'status': {}}))
        self.assertTrue(bot_accounts.deployment_is_stopped({'spec': {'replicas': 0}, 'status': {'replicas': None}}))
        self.assertFalse(bot_accounts.deployment_is_stopped({'spec': {'replicas': 1}, 'status': {'replicas': 0}}))
        self.assertFalse(bot_accounts.deployment_is_stopped({'spec': {'replicas': 0}, 'status': {'replicas': 1}}))
        self.assertFalse(bot_accounts.deployment_is_stopped({}))

    def test_nickname_correction_touches_only_the_three_roles(self):
        sql = bot_accounts.nickname_correction_sql()
        for name in ('BaristaIA', 'DJIA', 'PizzeroIA'):
            self.assertIn("WHEN '%s' THEN '%s'" % (name.lower(), name), sql)
        self.assertIn('NOT moderator', sql)
        self.assertNotIn('GuiaIA', sql)
        self.assertNotIn('password', sql.lower())

    def test_legacy_provisioner_refuses_later_nickname_changes(self):
        self.assertTrue(bot_accounts.legacy_work_nicknames_unchanged({}))
        self.assertTrue(bot_accounts.legacy_work_nicknames_unchanged({
            'baristaia': {'nickname': 'Baristaia'}, 'djia': {'nickname': 'DJIA'},
            'pizzeroia': {'nickname': 'PizzeroIA'}}))
        self.assertFalse(bot_accounts.legacy_work_nicknames_unchanged({
            'baristaia': {'nickname': 'Juanito'}}))

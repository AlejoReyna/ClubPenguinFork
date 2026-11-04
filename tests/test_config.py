"""Configuration parsing: defaults, env overrides and every rejection."""
import json
import unittest

from bots.config import ConfigError, build_config, load_config
from bots.config import parse_area, parse_bot

from support import config_document_world


class BuildConfigTests(unittest.TestCase):

    def test_valid_document_builds_everything(self):
        config = build_config(config_document_world(6112))
        self.assertEqual(config.server.login_port, 6112)
        self.assertEqual(config.server.world_port, 6112)
        self.assertEqual(len(config.bots), 2)
        first = config.bots[0]
        self.assertEqual(first.username, 'guiaia')
        self.assertEqual(first.nickname, 'GuiaIA')
        self.assertEqual(first.area[0], 10)
        self.assertEqual(config.provider.mode, 'simulated')
        self.assertEqual(config.provider.model, 'kimi-k2.6')
        self.assertFalse(config.behavior.respond_to_bots)

    def test_env_overrides_win_over_document(self):
        document = config_document_world(
            6112, **{'server.login_host': 'ignored-login',
                     'provider.mode': 'simulated'})
        env = {'BOTS_LOGIN_HOST': 'real-login', 'BOTS_LOGIN_PORT': '7112',
               'BOTS_WORLD_HOST': 'real-world', 'BOTS_WORLD_PORT': '9877',
               'BOTS_METRICS_PORT': '9199'}
        config = build_config(document, env)
        self.assertEqual(config.server.login_host, 'real-login')
        self.assertEqual(config.server.login_port, 7112)
        self.assertEqual(config.server.world_port, 9877)
        self.assertEqual(config.metrics_port, 9199)

    def test_api_mode_requires_a_key(self):
        document = config_document_world(6112, **{'provider.mode': 'api'})
        with self.assertRaises(ConfigError):
            build_config(document, {})
        # Simulated mode needs no key (only used by tests).
        simulated = build_config(config_document_world(6112), {})
        self.assertIsNone(simulated.provider.api_key)

    def test_api_mode_with_env_key_builds(self):
        document = config_document_world(6112,
                                         **{'provider.mode': 'api',
                                            'provider.base_url': 'http://127.0.0.1:9/v1'})
        env = {'MOONSHOT_API_KEY': 'sk-test-not-real'}
        config = build_config(document, env)
        self.assertEqual(config.provider.api_key, 'sk-test-not-real')
        self.assertEqual(config.provider.base_url, 'http://127.0.0.1:9/v1')

    def test_missing_bots_list_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_config({'bots': []})
        with self.assertRaises(ConfigError):
            build_config({})

    def test_duplicate_nicknames_are_rejected(self):
        document = config_document_world(6112)
        document['bots'][1]['nickname'] = 'GuiaIA'
        with self.assertRaises(ConfigError):
            build_config(document)

    def test_bad_username_is_rejected(self):
        document = config_document_world(6112)
        document['bots'][0]['username'] = 'ab'
        with self.assertRaises(ConfigError):
            build_config(document)
        document['bots'][0]['username'] = 'gu ia'
        with self.assertRaises(ConfigError):
            build_config(document)

    def test_placeholder_password_is_rejected(self):
        document = config_document_world(6112)
        document['bots'][0]['password'] = '<INGRESA_UNA>'
        with self.assertRaises(ConfigError):
            build_config(document)

    def test_max_message_length_bounds(self):
        document = config_document_world(6112, **{'behavior.max_message_length': 400})
        with self.assertRaises(ConfigError):
            build_config(document)

    def test_walk_delay_consistency(self):
        document = config_document_world(6112, **{'behavior.walk_min_delay': 30,
                                                  'behavior.walk_max_delay': 10})
        with self.assertRaises(ConfigError):
            build_config(document)

    def test_thinking_value_is_bounded(self):
        document = config_document_world(6112, **{'provider.thinking': 'moonshot'})
        with self.assertRaises(ConfigError):
            build_config(document)

    def test_bot_defaults_fill_optional_fields(self):
        bot = parse_bot(0, {'username': 'exploradoria', 'password': 'clave123'})
        self.assertEqual(bot.nickname, 'exploradoria')
        self.assertEqual(bot.room, 100)
        self.assertEqual(bot.aliases, [])
        self.assertEqual(bot.area, (100, 1000, 150, 700))
        self.assertEqual(bot.mention_names, ['exploradoria', 'exploradoria'])

    def test_mention_names_include_aliases(self):
        bot = parse_bot(0, {'username': 'guiaia', 'password': 'clave123',
                            'nickname': 'GuiaIA', 'aliases': ['Guia']})
        self.assertEqual(bot.mention_names, ['GuiaIA', 'guiaia', 'Guia'])

    def test_area_bounds(self):
        with self.assertRaises(ConfigError):
            parse_area({'min_x': 200, 'max_x': 100, 'min_y': 0, 'max_y': 10}, 'x')
        with self.assertRaises(ConfigError):
            parse_area({'min_x': 0, 'max_x': 9000, 'min_y': 0, 'max_y': 10}, 'x')
        with self.assertRaises(ConfigError):
            parse_area({}, 'x')  # malformed keys
        with self.assertRaises(ConfigError):
            parse_area('nope', 'x')
        self.assertEqual(parse_area(None, 'x'), (100, 1000, 150, 700))


class LoadConfigTests(unittest.TestCase):

    def test_inline_document_via_environment(self):
        document = config_document_world(6112)
        config = load_config({'BOTS_CONFIG': json.dumps(document)})
        self.assertEqual(len(config.bots), 2)

    def test_invalid_inline_json_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({'BOTS_CONFIG': '{"bots":'})

    def test_no_configuration_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({'BOTS_CONFIG_PATH': '/nonexistent/bots.json'})
        with self.assertRaises(ConfigError):
            load_config({})


if __name__ == '__main__':
    unittest.main()

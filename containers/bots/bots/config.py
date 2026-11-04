"""Configuration for the bots service.

Sources, in increasing precedence:
1. built-in defaults;
2. a JSON document (``BOTS_CONFIG`` env var or ``BOTS_CONFIG_PATH`` file);
3. environment overrides for infra values (hosts, ports, metrics, provider
   endpoint, API key).

The JSON document matches the format of ``containers/bots/configs/`` and the
one-time output of scripts/create-bot-accounts.py. Bot credentials (user and
password) live only there, never in code, logs or Terraform. No default file
contains a real secret.
"""
import json
import logging
import math
import os
from pathlib import Path

logger = logging.getLogger('bots.config')

METRICS_PORT_DEFAULT = 9102
CONFIG_SEARCH_PATHS = ('/etc/bots/config.json', 'configs/bots.json')


class ConfigError(Exception):
    """Raised for unusable configuration; the message is operator-facing."""


class ServerConfig:
    def __init__(self, login_host, login_port, world_host, world_port,
                 connect_timeout, auth_timeout):
        self.login_host = login_host
        self.login_port = int(login_port)
        self.world_host = world_host
        self.world_port = int(world_port)
        self.connect_timeout = float(connect_timeout)
        self.auth_timeout = float(auth_timeout)


class ProviderConfig:
    def __init__(self, mode, base_url, api_key, model, max_completion_tokens,
                 timeout, retries, retry_wait, thinking, max_concurrent,
                 fallback_replies, max_requests_per_day=100, budget_path='/var/lib/bots/budget.json'):
        self.mode = mode
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.max_completion_tokens = int(max_completion_tokens)
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.retry_wait = float(retry_wait)
        self.thinking = thinking
        self.max_concurrent = int(max_concurrent)
        self.fallback_replies = list(fallback_replies)
        self.max_requests_per_day = int(max_requests_per_day)
        self.budget_path = budget_path


class PolicyConfig:
    def __init__(self, requests_per_minute, max_concurrent_requests,
                 player_cooldown, bot_min_gap, queue_size,
                 memory_turns, memory_ttl):
        self.requests_per_minute = int(requests_per_minute)
        self.max_concurrent_requests = int(max_concurrent_requests)
        self.player_cooldown = float(player_cooldown)
        self.bot_min_gap = float(bot_min_gap)
        self.queue_size = int(queue_size)
        self.memory_turns = int(memory_turns)
        self.memory_ttl = float(memory_ttl)


class BehaviorConfig:
    def __init__(self, walk_min_delay, walk_max_delay, first_move_delay_min,
                 first_move_delay_max, max_message_length, respond_to_bots,
                 bot_chat_interval, bot_exchange_limit):
        self.walk_min_delay = float(walk_min_delay)
        self.walk_max_delay = float(walk_max_delay)
        self.first_move_delay_min = float(first_move_delay_min)
        self.first_move_delay_max = float(first_move_delay_max)
        self.max_message_length = int(max_message_length)
        # Cross-bot conversations exist but stay opt-in and strictly bounded.
        self.respond_to_bots = bool(respond_to_bots)
        self.bot_chat_interval = float(bot_chat_interval)
        self.bot_exchange_limit = int(bot_exchange_limit)


class BotConfig:
    """One penguin identity and its personalization."""

    def __init__(self, index, username, password, nickname, room, aliases,
                 personality, area):
        self.index = index
        self.username = str(username)
        self.password = str(password)
        self.nickname = str(nickname)
        self.room = int(room)
        self.aliases = [str(alias) for alias in aliases]
        self.personality = str(personality)
        self.area = area  # (min_x, max_x, min_y, max_y)

    @property
    def mention_names(self):
        """Tokens that make a human chat count as "the bot was addressed"."""
        return [self.nickname, self.username] + self.aliases


class Config:
    def __init__(self, server, provider, policy, behavior, bots, metrics_port):
        self.server = server
        self.provider = provider
        self.policy = policy
        self.behavior = behavior
        self.bots = bots
        self.metrics_port = int(metrics_port)


def _require(mapping, key, where):
    if key not in mapping:
        raise ConfigError(f'{where}: missing required key "{key}"')
    return mapping[key]


def _port(env, env_key, raw_section, key, default):
    """Port from the environment or the document, with an operator message."""
    value = env.get(env_key, raw_section.get(key, default))
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f'server: "{key}" must be a port number, got {value!r}')


def _number(mapping, key, default, minimum, where):
    value = mapping.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f'{where}: "{key}" must be a number, got {value!r}')
    if not math.isfinite(value) or value < minimum:
        raise ConfigError(f'{where}: "{key}" must be >= {minimum}, got {value}')
    return value


def parse_area(raw, where):
    if raw is None:
        return (100, 1000, 150, 700)
    try:
        min_x, max_x, min_y, max_y = (int(raw['min_x']), int(raw['max_x']),
                                      int(raw['min_y']), int(raw['max_y']))
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f'{where}: walk area needs integers '
                          'min_x, max_x, min_y, max_y')
    if max_x <= min_x or max_y <= min_y:
        raise ConfigError(f'{where}: walk area upper bounds must exceed lower')
    # Room scenes are 1520x960; keep legacy walks comfortably inside.
    if not (0 <= min_x < max_x <= 1520 and 0 <= min_y < max_y <= 960):
        raise ConfigError(f'{where}: walk area outside the AS2 scene (1520x960)')
    return (min_x, max_x, min_y, max_y)


def parse_bot(index, raw):
    if not isinstance(raw, dict):
        raise ConfigError(f'bots[{index}] must be an object')
    where = f'bots[{index}]'
    username = _require(raw, 'username', where)
    if not (4 <= len(str(username)) <= 12 and str(username).isalnum()):
        raise ConfigError(f'{where}: username must look like a registered '
                          f'account (4-12 alnum chars), got {username!r}')
    password = raw.get('password')
    if not password or '<' in str(password):
        raise ConfigError(f'{where}: password missing (placeholder present?)')
    nickname = raw.get('nickname') or str(username)
    room = int(raw.get('room', 100))
    if room <= 0:
        raise ConfigError(f'{where}: room must be a positive room id')
    aliases = raw.get('aliases') or []
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise ConfigError(f'{where}: aliases must be a list of strings')
    bot = BotConfig(
        index=index, username=username, password=password, nickname=nickname,
        room=room, aliases=aliases,
        personality=raw.get('personality') or '', area=parse_area(raw.get('walk'), where))
    return bot


DEFAULT_FALLBACK_REPLIES = (
    'Mi mente se enfoca en otro lado por ahora; exito con tu travesia.',
    'Por ahora sigo caminando; luego retomamos la conversacion.',
    'Ahora no puedo responder bien, pero la isla tiene mucho por descubrir.',
)


def load_config(env=None):
    env = env if env is not None else os.environ
    document = _load_document(env)
    return build_config(document, env)


def _load_document(env):
    inline = env.get('BOTS_CONFIG')
    if inline:
        try:
            document = json.loads(inline)
        except json.JSONDecodeError as error:
            raise ConfigError(f'BOTS_CONFIG is not valid JSON: {error}')
        if not isinstance(document, dict):
            raise ConfigError('BOTS_CONFIG must be a JSON object')
        logger.info('Loaded bot configuration from BOTS_CONFIG (in-memory)')
        return document
    path = env.get('BOTS_CONFIG_PATH')
    if path:
        return _read_json_file(Path(path), 'BOTS_CONFIG_PATH')
    for candidate in CONFIG_SEARCH_PATHS:
        file_path = Path(candidate)
        if file_path.is_file():
            return _read_json_file(file_path, 'configuration default path')
    raise ConfigError(
        'No bot configuration: set BOTS_CONFIG_PATH (a 0400 JSON file, '
        'usually from the bot-credentials Secret) or BOTS_CONFIG')


def _read_json_file(path, origin):
    if not path.is_file():
        raise ConfigError(f'{origin}: file {path} does not exist')
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ConfigError(f'{origin}: {path} is not valid JSON: {error}')
    if not isinstance(document, dict):
        raise ConfigError(f'{origin}: {path} must contain a JSON object')
    logger.info('Loaded bot configuration from %s', path)
    return document


def build_config(document, env=None):
    env = env if env is not None else os.environ
    if 'bots' not in document or not isinstance(document['bots'], list) \
            or not document['bots']:
        raise ConfigError('configuration needs a non-empty "bots" list')
    bots = [parse_bot(index, raw) for index, raw in enumerate(document['bots'])]
    nicknames = [bot.nickname.lower() for bot in bots]
    if len(set(nicknames)) != len(nicknames):
        raise ConfigError('bot nicknames must be unique')

    server_raw = document.get('server') or {}
    server = ServerConfig(
        login_host=str(env.get('BOTS_LOGIN_HOST',
                               server_raw.get('login_host', 'login'))),
        login_port=_port(env, 'BOTS_LOGIN_PORT', server_raw, 'login_port', 6112),
        world_host=str(env.get('BOTS_WORLD_HOST',
                               server_raw.get('world_host', 'glaciar'))),
        world_port=_port(env, 'BOTS_WORLD_PORT', server_raw, 'world_port', 9876),
        connect_timeout=_number(server_raw, 'connect_timeout', 10.0, 1.0, 'server'),
        auth_timeout=_number(server_raw, 'auth_timeout', 20.0, 1.0, 'server'))

    provider_raw = document.get('provider') or {}
    mode = str(env.get('BOTS_PROVIDER_MODE', provider_raw.get('mode', 'simulated')))
    if mode not in ('api', 'simulated'):
        raise ConfigError('provider.mode must be "api" or "simulated"')
    api_key = env.get('MOONSHOT_API_KEY', provider_raw.get('api_key'))
    if api_key is None or str(api_key) == '':
        if mode == 'api':
            raise ConfigError(
                'Moonshot API key missing: provide MOONSHOT_API_KEY through the '
                'Secret (mode "api") or set provider.mode="simulated" for tests')
        api_key = None
    base_url = str(env.get('BOTS_PROVIDER_BASE_URL',
                           provider_raw.get('base_url', 'https://api.moonshot.ai/v1')))
    model = str(env.get('BOTS_PROVIDER_MODEL',
                        provider_raw.get('model', 'kimi-k2.6')))
    thinking = str(provider_raw.get('thinking', 'disabled'))
    if thinking not in ('enabled', 'disabled', 'auto'):
        raise ConfigError('provider.thinking must be enabled, disabled or auto')
    fallback_replies = provider_raw.get('fallback_replies')
    if fallback_replies is None:
        fallback_replies = DEFAULT_FALLBACK_REPLIES
    if (not isinstance(fallback_replies, (list, tuple)) or not fallback_replies
            or any(not isinstance(item, str) for item in fallback_replies)):
        raise ConfigError('provider.fallback_replies must be a list of strings')
    fallback_replies = [item for item in fallback_replies if item.strip()]
    if not fallback_replies:
        raise ConfigError('provider.fallback_replies must not be empty')
    provider = ProviderConfig(
        mode=mode, base_url=base_url, api_key=api_key, model=model,
        max_completion_tokens=_number(provider_raw, 'max_completion_tokens',
                                      120, 16, 'provider'),
        timeout=_number(provider_raw, 'timeout', 30.0, 1.0, 'provider'),
        retries=_number(provider_raw, 'retries', 2, 0, 'provider'),
        retry_wait=_number(provider_raw, 'retry_wait', 2.0, 0.1, 'provider'),
        thinking=thinking,
        max_concurrent=_number(provider_raw, 'max_concurrent', 2, 1, 'provider'),
        fallback_replies=fallback_replies,
        max_requests_per_day=_number(provider_raw, 'max_requests_per_day', 100, 1, 'provider'),
        budget_path=str(env.get('BOTS_BUDGET_PATH', provider_raw.get('budget_path', '/var/lib/bots/budget.json'))))

    policy_raw = document.get('policy') or {}
    policy = PolicyConfig(
        requests_per_minute=_number(policy_raw, 'requests_per_minute',
                                    6, 1, 'policy'),
        max_concurrent_requests=_number(policy_raw, 'max_concurrent_requests',
                                        2, 1, 'policy'),
        player_cooldown=_number(policy_raw, 'player_cooldown', 6.0, 0, 'policy'),
        bot_min_gap=_number(policy_raw, 'bot_min_gap', 2.0, 0, 'policy'),
        queue_size=_number(policy_raw, 'queue_size', 8, 1, 'policy'),
        memory_turns=_number(policy_raw, 'memory_turns', 8, 1, 'policy'),
        memory_ttl=_number(policy_raw, 'memory_ttl', 600.0, 1, 'policy'))

    behavior_raw = document.get('behavior') or {}
    max_len = int(_number(behavior_raw, 'max_message_length', 54, 8,
                          'behavior'))
    if max_len > 100:
        raise ConfigError('behavior.max_message_length must stay within the '
                          'AS2 chat box (<= 100)')
    behavior = BehaviorConfig(
        walk_min_delay=_number(behavior_raw, 'walk_min_delay', 12.0, 0.1, 'behavior'),
        walk_max_delay=_number(behavior_raw, 'walk_max_delay', 28.0, 0.2, 'behavior'),
        first_move_delay_min=_number(behavior_raw, 'first_move_delay_min',
                                     2.0, 0.0, 'behavior'),
        first_move_delay_max=_number(behavior_raw, 'first_move_delay_max',
                                     8.0, behavior_raw.get('first_move_delay_min', 2.0),
                                     'behavior'),
        max_message_length=max_len,
        respond_to_bots=bool(behavior_raw.get('respond_to_bots', False)),
        bot_chat_interval=_number(behavior_raw, 'bot_chat_interval', 90.0, 5, 'behavior'),
        bot_exchange_limit=_number(behavior_raw, 'bot_exchange_limit', 2, 0, 'behavior'))
    if behavior.walk_max_delay < behavior.walk_min_delay:
        raise ConfigError('behavior.walk_max_delay must be >= walk_min_delay')

    metrics_port = int(env.get('BOTS_METRICS_PORT', METRICS_PORT_DEFAULT))
    return Config(server=server, provider=provider, policy=policy,
                  behavior=behavior, bots=bots, metrics_port=metrics_port)

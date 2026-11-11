"""Configuration for the bots service.

Sources, in increasing precedence:
1. built-in defaults;
2. a JSON document (``BOTS_CONFIG`` env var or ``BOTS_CONFIG_PATH`` file);
3. environment overrides for infra values (hosts, ports, metrics, provider
   endpoint, API key, spend caps, time zone, cast directory).

The JSON document matches the one-time output of scripts/create-bot-accounts.py.
Bot credentials (user and password) live only there, never in code, logs or
Terraform. No default file contains a real secret.

Live penguins (docs/bots.md) are opt-in: only when ``BOTS_CAST_DIR``
points at a directory of persona cards do bots with a card (matched to the
Secret entry by ``username``) become cast-driven. Without it, or for a bot
without a card, the entry is parsed exactly as before.
"""
import datetime
import json
import logging
import math
import os
from pathlib import Path
import random
import zlib

from . import cast as cast_mod
from . import rooms as rooms_mod
from . import safety
from . import spend as spend_mod

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
                 fallback_replies, max_requests_per_day=100, budget_path='/var/lib/bots/budget.json',
                 daily_usd=1.0, monthly_usd=30.0, max_requests_per_minute=6, prices=None):
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
        # 0 turns the legacy requests-per-day cap off (the USD caps then govern).
        self.max_requests_per_day = int(max_requests_per_day)
        self.budget_path = budget_path
        self.daily_usd = float(daily_usd)
        self.monthly_usd = float(monthly_usd)
        self.max_requests_per_minute = int(max_requests_per_minute)
        self.prices = prices if prices is not None else spend_mod.Prices()


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
                 bot_chat_interval, bot_exchange_limit, life_min_delay=20.0,
                 life_max_delay=60.0, personal_space=55.0, rest_median=35.0,
                 burst_chance=0.45, afk_chance=0.03):
        self.walk_min_delay = float(walk_min_delay)
        self.walk_max_delay = float(walk_max_delay)
        self.first_move_delay_min = float(first_move_delay_min)
        self.first_move_delay_max = float(first_move_delay_max)
        self.max_message_length = int(max_message_length)
        # Cross-bot conversations exist but stay opt-in and strictly bounded.
        self.respond_to_bots = bool(respond_to_bots)
        self.bot_chat_interval = float(bot_chat_interval)
        self.bot_exchange_limit = int(bot_exchange_limit)
        # Cast-driven bots pick a new in-room activity every 20-60 s.
        self.life_min_delay = float(life_min_delay)
        self.life_max_delay = float(life_max_delay)
        # Cast bodies: minimum px between penguins (0 = original random walk).
        self.personal_space = float(personal_space)
        # Player-like pacing: median pause in s (0 = uniform life_min..life_max), chance a
        # walk turns into a burst of clicks, chance a pause becomes a few AFK minutes.
        self.rest_median = float(rest_median)
        self.burst_chance = float(burst_chance)
        self.afk_chance = float(afk_chance)


class ServiceConfig:
    """Process-level settings: readiness, staggered start, time zone, cast and filter files."""

    def __init__(self, ready_ratio, startup_stagger_min, startup_stagger_max, tz_name, tz,
                 cast_dir, blocklist_path, life_seed):
        self.ready_ratio = float(ready_ratio)
        self.startup_stagger_min = float(startup_stagger_min)
        self.startup_stagger_max = float(startup_stagger_max)
        self.tz_name = tz_name
        self.tz = tz
        self.cast_dir = cast_dir
        self.blocklist_path = blocklist_path
        self.life_seed = int(life_seed)


class DialogueConfig:
    """Phase 2 is independent of both cast bodies and paid-provider activation."""

    DEFAULTS = {
        'greeting_chance': 0.5, 'greeting_model_share': 0.3,
        'greeting_delay_min': 3.0, 'greeting_delay_max': 8.0, 'greeting_ttl': 600.0,
        'scene_min_delay': 240.0, 'scene_max_delay': 360.0,
        'line_delay_min': 1.0, 'line_delay_max': 2.5,
        'hello_chance': 0.6, 'question_chance': 0.5, 'other_chance': 0.25,
        'continuity_ttl': 120.0, 'player_daily_limit': 40, 'queue_size': 8,
        'room_gap': 2.0,
        # Attention: a bot already chatting with the player keeps answering; unnamed chat
        # is heard less across the room (px, 0 = everywhere) and by shyer characters
        # (weight of card chattiness, 0 = ignore it).
        'continuity_chance': 0.9, 'hearing_radius': 220.0, 'chattiness_weight': 1.0,
        # Typing speed in characters per second before a line appears (0 = instant).
        'typing_cps': 4.5,
        # Group chat: at most group_max penguins answer one human line. Chat aimed at the
        # group ("que hacen", "chicos") gives each nearby penguin group_chance to answer;
        # other unnamed chat lets one extra penguin chime in with chime_chance.
        'group_max': 3, 'group_chance': 0.7, 'chime_chance': 0.15,
    }

    def __init__(self, enabled=False, **settings):
        self.enabled = enabled
        for key, default in self.DEFAULTS.items():
            setattr(self, key, settings.get(key, default))


class LifeConfig:
    """Opt-in Phase 3. Durations are seconds; times of day use BOTS_TZ."""
    DEFAULTS = {'visit_seconds': 180.0, 'follow_seconds': 600.0,
                'departure_min': 1.0, 'departure_max': 2.0, 'tick_seconds': 2.0,
                'wander_chance': 0.15, 'minimum_home_share': 0.75,
                'conversation_seconds': 180.0, 'dojo_hour': 18, 'walk_weight': 1,
                'approximate_walk': 0}

    def __init__(self, enabled=False, events=True, autonomous=True, **values):
        self.enabled, self.events, self.autonomous = enabled, events, autonomous
        for key, default in self.DEFAULTS.items():
            setattr(self, key, values.get(key, default))


class RecallConfig:
    """Opt-in Phase 4. Short relationship notes keyed by numeric player id."""

    DEFAULTS = {'ttl_days': 30.0, 'min_turns': 3.0, 'max_note_chars': 120.0}

    def __init__(self, enabled=False, path='/var/lib/bots/recall.sqlite', **values):
        self.enabled = enabled
        self.path = str(path)
        for key, default in self.DEFAULTS.items():
            setattr(self, key, values.get(key, default))

    @property
    def ttl_seconds(self):
        return float(self.ttl_days) * 86400.0


def _parse_recall(raw, env, budget_path):
    if not isinstance(raw, dict) or set(raw) - ({'enabled', 'path'} | set(RecallConfig.DEFAULTS)):
        raise ConfigError('recall: unknown setting or not an object')
    enabled = raw.get('enabled', False)
    if 'BOTS_RECALL_ENABLED' in env:
        flag = str(env['BOTS_RECALL_ENABLED']).lower()
        if flag not in ('true', 'false'):
            raise ConfigError('BOTS_RECALL_ENABLED must be true or false')
        enabled = flag == 'true'
    if not isinstance(enabled, bool):
        raise ConfigError('recall.enabled must be true or false')
    path = str(env.get('BOTS_RECALL_PATH', raw.get('path', budget_path)))
    if not path:
        raise ConfigError('recall.path must be set when recall is enabled')
    values = {}
    for key, default in RecallConfig.DEFAULTS.items():
        minimum = 3 if key == 'min_turns' else 1
        value = _number(raw, key, default, minimum, 'recall')
        if key == 'min_turns':
            if value != int(value):
                raise ConfigError('recall.min_turns must be an integer')
            value = int(value)
        elif key == 'max_note_chars':
            value = int(value)
            if value > 200:
                raise ConfigError('recall.max_note_chars must be at most 200')
        values[key] = value
    return RecallConfig(enabled, path=path, **values)


def _parse_life(raw, env):
    if not isinstance(raw, dict) or set(raw) - ({'enabled', 'events', 'autonomous'} | set(LifeConfig.DEFAULTS)):
        raise ConfigError('life: unknown setting or not an object')
    enabled = raw.get('enabled', False)
    if 'BOTS_LIFE_ENABLED' in env:
        flag = str(env['BOTS_LIFE_ENABLED']).lower()
        if flag not in ('true', 'false'):
            raise ConfigError('BOTS_LIFE_ENABLED must be true or false')
        enabled = flag == 'true'
    events = raw.get('events', True)
    autonomous = raw.get('autonomous', True)
    if not all(isinstance(value, bool) for value in (enabled, events, autonomous)):
        raise ConfigError('life.enabled/events/autonomous must be booleans')
    values = {key: _number(raw, key, default, 0, 'life') for key, default in LifeConfig.DEFAULTS.items()}
    if (not .75 <= values['minimum_home_share'] <= 1 or not 0 <= values['wander_chance'] <= 1
            or not 0 < values['follow_seconds'] <= 600 or not 0 < values['visit_seconds'] <= 900
            or values['tick_seconds'] < .05 or values['departure_max'] < values['departure_min']
            or not 0 <= values['dojo_hour'] <= 23 or int(values['dojo_hour']) != values['dojo_hour']):
        raise ConfigError('life: invalid share, probability, duration or hour')
    values['dojo_hour'] = int(values['dojo_hour'])
    return LifeConfig(enabled, events, autonomous, **values)


def _parse_dialogue(raw, env):
    if not isinstance(raw, dict) or set(raw) - ({'enabled'} | set(DialogueConfig.DEFAULTS)):
        raise ConfigError('dialogue: unknown setting or not an object')
    enabled = raw.get('enabled', False)
    if 'BOTS_DIALOGUE_ENABLED' in env:
        flag = str(env['BOTS_DIALOGUE_ENABLED']).lower()
        if flag not in ('true', 'false'):
            raise ConfigError('BOTS_DIALOGUE_ENABLED must be true or false')
        enabled = flag == 'true'
    if not isinstance(enabled, bool):
        raise ConfigError('dialogue.enabled must be true or false')
    values = {}
    for key, default in DialogueConfig.DEFAULTS.items():
        minimum = 1 if key in ('player_daily_limit', 'queue_size', 'group_max') else 0
        value = _number(raw, key, default, minimum, 'dialogue')
        if key.endswith('chance') or key in ('greeting_model_share', 'chattiness_weight'):
            if value > 1:
                raise ConfigError(f'dialogue.{key}: probability must be at most 1')
        if key in ('player_daily_limit', 'queue_size', 'group_max'):
            if value != int(value):
                raise ConfigError(f'dialogue.{key}: must be an integer')
            value = int(value)
        values[key] = value
    for low, high in (('greeting_delay_min', 'greeting_delay_max'),
                      ('scene_min_delay', 'scene_max_delay'), ('line_delay_min', 'line_delay_max')):
        if values[high] < values[low] or (low == 'scene_min_delay' and values[low] < 0.05):
            raise ConfigError(f'dialogue: invalid {low}/{high} interval')
    return DialogueConfig(enabled, **values)


class BotConfig:
    """One penguin identity and its personalization."""

    def __init__(self, index, username, password, nickname, room, aliases,
                 personality, area, *, stationary=False, spawn=None,
                 repeat_action=None, repeat_emote=None, music=True, card=None,
                 fallbacks=(), greeter=False, role=None):
        self.index = index
        self.username = str(username)
        self.password = str(password)
        self.nickname = str(nickname)
        self.room = int(room)
        self.aliases = [str(alias) for alias in aliases]
        self.personality = str(personality)
        self.area = area  # (min_x, max_x, min_y, max_y)
        self.stationary = bool(stationary)
        self.spawn = spawn  # (x, y) room entry tile; defaults to area centre
        self.repeat_action = repeat_action  # u#sa loop (e.g. dance)
        self.repeat_emote = repeat_emote  # u#se loop
        # Players may ask this bot for their own music (on by default for every bot).
        self.music = bool(music)
        # cast.Card for cast-driven bots (their life is life.py, not the walk loops).
        self.card = card
        self.fallbacks = [str(line) for line in fallbacks]  # persona lines; empty = global ones
        # A host walks up to every human who enters its room, greets with the model and goes back to its post.
        self.greeter = bool(greeter)
        # cast role: None, "helper" or "guide". helper/guide never enter an igloo.
        self.role = role

    @property
    def mention_names(self):
        """Tokens that make a human chat count as "the bot was addressed"."""
        return [self.nickname, self.username] + self.aliases


class Config:
    def __init__(self, server, provider, policy, behavior, bots, metrics_port,
                 service=None, catalog=None, output_filter=None, cast_unmatched=(),
                 dialogue=None, life=None, recall=None):
        self.server = server
        self.provider = provider
        self.policy = policy
        self.behavior = behavior
        self.bots = bots
        self.metrics_port = int(metrics_port)
        self.service = service if service is not None else default_service()
        self.catalog = catalog                      # rooms.Catalog, only in cast mode
        self.output_filter = output_filter if output_filter is not None else safety.OutputFilter()
        self.cast_unmatched = tuple(cast_unmatched)  # cards with no credentials yet
        self.dialogue = dialogue or DialogueConfig()
        self.life = life or LifeConfig()
        self.recall = recall or RecallConfig()


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
        raise ConfigError(f'server: "{key}" must be a port number, got {value!r}') from None


def _number(mapping, key, default, minimum, where):
    value = mapping.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f'{where}: "{key}" must be a number, got {value!r}') from None
    if not math.isfinite(value) or value < minimum:
        raise ConfigError(f'{where}: "{key}" must be >= {minimum}, got {value}')
    return value


def parse_area(raw, where, *, allow_point=False):
    if raw is None:
        return (100, 1000, 150, 700)
    try:
        min_x, max_x, min_y, max_y = (int(raw['min_x']), int(raw['max_x']),
                                      int(raw['min_y']), int(raw['max_y']))
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f'{where}: walk area needs integers '
                          'min_x, max_x, min_y, max_y') from None
    if max_x < min_x or max_y < min_y:
        raise ConfigError(f'{where}: walk area upper bounds must not be below lower')
    if not allow_point and (max_x == min_x or max_y == min_y):
        raise ConfigError(f'{where}: walk area must span more than one tile '
                          '(use anchor + stationary for a fixed post)')
    # Room scenes are 1520x960; keep legacy walks comfortably inside.
    if allow_point and min_x == max_x and min_y == max_y:
        if not (0 <= min_x <= 1520 and 0 <= min_y <= 960):
            raise ConfigError(f'{where}: anchor outside the AS2 scene (1520x960)')
        return (min_x, max_x, min_y, max_y)
    if not (0 <= min_x < max_x <= 1520 and 0 <= min_y < max_y <= 960):
        raise ConfigError(f'{where}: walk area outside the AS2 scene (1520x960)')
    return (min_x, max_x, min_y, max_y)


def _credentials(index, raw):
    """Validated ``(username, password)`` of one Secret entry (cast or not)."""
    where = f'bots[{index}]'
    username = _require(raw, 'username', where)
    if not (4 <= len(str(username)) <= 12 and str(username).isalnum()):
        raise ConfigError(f'{where}: username must look like a registered '
                          f'account (4-12 alnum chars), got {username!r}')
    password = raw.get('password')
    if not password or '<' in str(password):
        raise ConfigError(f'{where}: password missing (placeholder present?)')
    return username, password


def _setting(env, env_key, raw, key, default, minimum, where):
    """A number from the environment (wins) or the document, with an operator message."""
    if str(env.get(env_key, '')).strip():
        return _number({key: env[env_key]}, key, default, minimum, f'environment {env_key}')
    return _number(raw, key, default, minimum, where)


def timezone_for(name):
    """tzinfo for an IANA name. UTC needs no tz database; others raise ConfigError."""
    if name in ('UTC', 'Etc/UTC', 'utc'):
        return datetime.timezone.utc
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:  # unknown key, malformed key or no tz database in the image
        raise ConfigError(f'service: time zone {name!r} is not available (BOTS_TZ must be an '
                          'IANA name such as "America/Mexico_City"; the default is UTC)') from None


def default_service():
    return ServiceConfig(0.8, 2.0, 4.0, 'UTC', datetime.timezone.utc, None, None, 0)


def _parse_service(raw, env):
    if not isinstance(raw, dict):
        raise ConfigError('service must be an object')
    ratio = _setting(env, 'BOTS_READY_RATIO', raw, 'ready_ratio', 0.8, 0.01, 'service')
    if ratio > 1.0:
        raise ConfigError('service: "ready_ratio" must be at most 1')
    stagger_min = _setting(env, 'BOTS_STARTUP_STAGGER_MIN', raw, 'startup_stagger_min', 2.0, 0.0, 'service')
    stagger_max = _setting(env, 'BOTS_STARTUP_STAGGER_MAX', raw, 'startup_stagger_max', 4.0, 0.0, 'service')
    if stagger_max < stagger_min:
        raise ConfigError('service: "startup_stagger_max" must be >= "startup_stagger_min"')
    tz_name = str(env.get('BOTS_TZ') or raw.get('tz') or 'UTC').strip()
    cast_dir = str(env.get('BOTS_CAST_DIR') or raw.get('cast_dir') or '').strip() or None
    blocklist = str(env.get('BOTS_BLOCKLIST_FILE') or raw.get('blocklist_file') or '').strip() or None
    seed = int(_number(raw, 'life_seed', 0, 0, 'service'))
    return ServiceConfig(ratio, stagger_min, stagger_max, tz_name, timezone_for(tz_name),
                         cast_dir, blocklist, seed)


def _cast_bot(index, raw, card, catalog):
    """A Secret entry merged with its persona card. The card wins field by field."""
    where = f'bots[{index}] ({card.username})'
    username, password = _credentials(index, raw)
    nickname = card.nickname or raw.get('nickname') or str(username)
    room = card.home_room if card.home_room is not None else raw.get('room')
    if isinstance(room, bool) or not isinstance(room, int) or room <= 0:
        raise ConfigError(f'{where}: no home_room in the card and no usable room in the Secret entry')
    home = catalog.get(room)
    if home is None:
        raise ConfigError(f'{where}: home room {room} is not in the room catalog')
    for pois_room, label in ((card.wander_favorites, 'wander.favorites'),
                             ([block.room for block in card.schedule], 'schedule')):
        missing = sorted(set(pois_room) - set(catalog.rooms))
        if missing:
            raise ConfigError(f'{where}: {label} names rooms {missing} that are not in the room catalog')
    if home.measured:
        absent = [name for name in card.home_pois if name not in home.pois]
        if absent:
            raise ConfigError(f'{where}: room {room} has no point of interest {absent} '
                              f'(it has {sorted(home.pois)})')
    # Deterministic per bot, so the same account always lands on the same slot.
    point, _poi = home.post(card.home_pois, random.Random(zlib.crc32(str(username).encode())))
    if point is None:
        raise ConfigError(f'{where}: room {room} has no spawn point to start from')
    if home.walk:
        area = (min(r.min_x for r in home.walk), max(r.max_x for r in home.walk),
                min(r.min_y for r in home.walk), max(r.max_y for r in home.walk))
    else:
        area = (point[0], point[0], point[1], point[1])
    aliases = list(card.aliases) if 'aliases' in card.provided else list(raw.get('aliases') or [])
    return BotConfig(
        index=index, username=username, password=password, nickname=nickname, room=room,
        aliases=aliases, personality=card.personality_text() or raw.get('personality') or '',
        area=area, stationary=not home.walk, spawn=point,
        music=card.music if 'music' in card.provided else raw.get('music', True),
        card=card, fallbacks=card.fallbacks if 'fallbacks' in card.provided else raw.get('fallbacks', ()),
        role=card.role)


def parse_bot(index, raw):
    if not isinstance(raw, dict):
        raise ConfigError(f'bots[{index}] must be an object')
    where = f'bots[{index}]'
    username, password = _credentials(index, raw)
    nickname = raw.get('nickname') or str(username)
    room = int(raw.get('room', 100))
    if room <= 0:
        raise ConfigError(f'{where}: room must be a positive room id')
    aliases = raw.get('aliases') or []
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise ConfigError(f'{where}: aliases must be a list of strings')
    stationary = bool(raw.get('stationary', False))
    anchor = raw.get('anchor')
    walk = raw.get('walk')
    if stationary:
        if not isinstance(anchor, dict):
            raise ConfigError(f'{where}: stationary bots need anchor {{x, y}}')
        try:
            ax, ay = int(anchor['x']), int(anchor['y'])
        except (KeyError, TypeError, ValueError):
            raise ConfigError(f'{where}: anchor needs integer x and y') from None
        area = parse_area({'min_x': ax, 'max_x': ax, 'min_y': ay, 'max_y': ay},
                          where, allow_point=True)
        spawn = (ax, ay)
    else:
        area = parse_area(walk, where)
        spawn = None
    repeat_action = raw.get('repeat_action')
    repeat_emote = raw.get('repeat_emote')
    if repeat_action is not None:
        repeat_action = int(repeat_action)
    if repeat_emote is not None:
        repeat_emote = int(repeat_emote)
    if repeat_action is not None and repeat_action < 0:
        raise ConfigError(f'{where}: repeat_action must be non-negative')
    if repeat_emote is not None and repeat_emote < 0:
        raise ConfigError(f'{where}: repeat_emote must be non-negative')
    bot = BotConfig(
        index=index, username=username, password=password, nickname=nickname,
        room=room, aliases=aliases,
        personality=raw.get('personality') or '', area=area,
        stationary=stationary, spawn=spawn,
        repeat_action=repeat_action, repeat_emote=repeat_emote,
        music=raw.get('music', True), greeter=bool(raw.get('greeter', False)))
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
            raise ConfigError(f'BOTS_CONFIG is not valid JSON: {error}') from None
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
        raise ConfigError(f'{origin}: {path} is not valid JSON: {error}') from None
    if not isinstance(document, dict):
        raise ConfigError(f'{origin}: {path} must contain a JSON object')
    logger.info('Loaded bot configuration from %s', path)
    return document


def _prices(raw, env):
    if not isinstance(raw, dict):
        raise ConfigError('provider.prices must be an object')
    values = {}
    for key, env_key, default in (('input_per_m', 'BOTS_PRICE_INPUT_PER_M', 0.95),
                                  ('cached_input_per_m', 'BOTS_PRICE_CACHED_INPUT_PER_M', 0.16),
                                  ('output_per_m', 'BOTS_PRICE_OUTPUT_PER_M', 4.0)):
        values[key] = _setting(env, env_key, raw, key, default, 0.0, 'provider.prices')
    return spend_mod.Prices(**values)


def _output_filter(service):
    if not service.blocklist_path:
        return safety.OutputFilter()
    try:
        return safety.OutputFilter.from_file(service.blocklist_path)
    except (OSError, UnicodeError) as error:
        raise ConfigError(f'service: blocklist {service.blocklist_path} cannot be read '
                          f'({getattr(error, "strerror", None) or type(error).__name__})') from None


def _check_cast_roster(bots, catalog):
    """Cast-wide rules: nobody answers to two penguins' names, no room is overcrowded."""
    owner = {}
    for bot in bots:
        for name in bot.mention_names:
            key = name.casefold()
            if key in owner and owner[key] != bot.username:
                raise ConfigError(f'the name {name!r} belongs to both {owner[key]!r} and '
                                  f'{bot.username!r}: two penguins would answer to it')
            owner[key] = bot.username
    homed = {}
    for bot in bots:
        homed[bot.room] = homed.get(bot.room, 0) + 1
    for room_id, count in sorted(homed.items()):
        room = catalog.get(room_id)
        if room is not None and count > room.bot_cap:
            raise ConfigError(f'room {room_id} has {count} bots homed but its bot_cap is {room.bot_cap}')


def build_config(document, env=None):
    env = env if env is not None else os.environ
    if 'bots' not in document or not isinstance(document['bots'], list) \
            or not document['bots']:
        raise ConfigError('configuration needs a non-empty "bots" list')
    service = _parse_service(document.get('service') or {}, env)
    dialogue = _parse_dialogue(document.get('dialogue', {}), env)
    life = _parse_life(document.get('life', {}), env)
    budget_default = str(env.get('BOTS_BUDGET_PATH', (document.get('provider') or {}).get(
        'budget_path', '/var/lib/bots/budget.json')))
    recall_path = str(Path(budget_default).with_name('recall.sqlite'))
    recall = _parse_recall(document.get('recall', {}), env, recall_path)
    if life.enabled and not dialogue.enabled:
        raise ConfigError('life.enabled requires dialogue.enabled for shared reservations')
    if recall.enabled and not dialogue.enabled:
        raise ConfigError('recall.enabled requires dialogue.enabled')
    if dialogue.enabled and not service.cast_dir:
        raise ConfigError('dialogue.enabled requires BOTS_CAST_DIR or service.cast_dir')
    output_filter = _output_filter(service)
    cards, catalog = {}, None
    if service.cast_dir:
        try:
            cards = cast_mod.load_cards(service.cast_dir, output_filter)
            catalog = rooms_mod.load_catalog(env.get('BOTS_ROOMS_FILE') or None)
        except (cast_mod.CastError, rooms_mod.RoomError) as error:
            raise ConfigError(f'cast: {error}') from None
    bots, matched = [], set()
    for index, raw in enumerate(document['bots']):
        card = cards.get(str(raw.get('username', '')).lower()) if isinstance(raw, dict) else None
        if card is None:
            bots.append(parse_bot(index, raw))  # no card: exactly the format of every older release
            continue
        matched.add(card.username)
        if card.is_role_note():
            # Stamp the role onto the Secret entry. Do not take over its post.
            bot = parse_bot(index, raw)
            bot.role = card.role
            bots.append(bot)
            continue
        if card.enabled:
            bots.append(_cast_bot(index, raw, card, catalog))
        else:
            logger.info('cast card %s is disabled; no session will be opened', card.username)
    nicknames = [bot.nickname.lower() for bot in bots]
    if len(set(nicknames)) != len(nicknames):
        raise ConfigError('bot nicknames must be unique')
    if cards:
        _check_cast_roster(bots, catalog)

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
    policy_raw = document.get('policy') or {}
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
        max_concurrent=_setting(env, 'BOTS_MAX_CONCURRENT', provider_raw, 'max_concurrent', 2, 1, 'provider'),
        fallback_replies=fallback_replies,
        max_requests_per_day=_setting(env, 'BOTS_MAX_REQUESTS_PER_DAY', provider_raw,
                                      'max_requests_per_day', 100, 0, 'provider'),
        budget_path=str(env.get('BOTS_BUDGET_PATH', provider_raw.get('budget_path', '/var/lib/bots/budget.json'))),
        daily_usd=_setting(env, 'BOTS_DAILY_SPEND_CAP_USD', provider_raw, 'daily_usd', 1.0, 0.0001, 'provider'),
        monthly_usd=_setting(env, 'BOTS_MONTHLY_SPEND_CAP_USD', provider_raw, 'monthly_usd', 30.0, 0.0001, 'provider'),
        max_requests_per_minute=_setting(env, 'BOTS_MAX_REQUESTS_PER_MINUTE', provider_raw,
                                         'max_requests_per_minute',
                                         _number(policy_raw, 'requests_per_minute', 6, 1, 'policy'), 1, 'provider'),
        prices=_prices(provider_raw.get('prices') or {}, env))
    if provider.monthly_usd < provider.daily_usd:
        raise ConfigError('provider: "monthly_usd" must be at least "daily_usd"')

    policy = PolicyConfig(
        requests_per_minute=_number(policy_raw, 'requests_per_minute',
                                    6, 1, 'policy'),
        max_concurrent_requests=_setting(env, 'BOTS_MAX_CONCURRENT', policy_raw,
                                         'max_concurrent_requests', 2, 1, 'policy'),
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
        bot_exchange_limit=_number(behavior_raw, 'bot_exchange_limit', 2, 0, 'behavior'),
        life_min_delay=_number(behavior_raw, 'life_min_delay', 20.0, 0.05, 'behavior'),
        life_max_delay=_number(behavior_raw, 'life_max_delay', 60.0, 0.05, 'behavior'),
        personal_space=_number(behavior_raw, 'personal_space', 55.0, 0, 'behavior'),
        rest_median=_number(behavior_raw, 'rest_median', 35.0, 0, 'behavior'),
        burst_chance=_number(behavior_raw, 'burst_chance', 0.45, 0, 'behavior'),
        afk_chance=_number(behavior_raw, 'afk_chance', 0.03, 0, 'behavior'))
    if behavior.walk_max_delay < behavior.walk_min_delay:
        raise ConfigError('behavior.walk_max_delay must be >= walk_min_delay')
    if behavior.life_max_delay < behavior.life_min_delay:
        raise ConfigError('behavior.life_max_delay must be >= life_min_delay')
    if behavior.personal_space > 200 or (0 < behavior.rest_median < 1) or behavior.rest_median > 600:
        raise ConfigError('behavior: personal_space must be 0-200 px and rest_median 0 or 1-600 s')
    if behavior.burst_chance > 1 or behavior.afk_chance > 1:
        raise ConfigError('behavior: burst_chance and afk_chance are probabilities (<= 1)')

    metrics_port = int(env.get('BOTS_METRICS_PORT', METRICS_PORT_DEFAULT))
    return Config(server=server, provider=provider, policy=policy,
                  behavior=behavior, bots=bots, metrics_port=metrics_port,
                  service=service, catalog=catalog, output_filter=output_filter,
                  cast_unmatched=sorted(set(cards) - matched), dialogue=dialogue,
                  life=life, recall=recall)

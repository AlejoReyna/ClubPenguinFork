"""Persona cards: the non-secret half of a character, joined to credentials by username.

One JSON file per penguin lives in ``containers/bots/cast/`` (Git) and reaches
the pod through the optional ``bots-cast`` ConfigMap. Credentials stay in the
``bot-credentials`` Secret; a card never holds a password. See
docs/pinguinos-vivos.md section 4 for the schema.

A card is a field-by-field override: what the card defines wins, what it omits
falls back to the Secret entry (the older ``room``/``walk``/``personality``
format), and a bot without a card is parsed exactly as before. Nicknames are
plain ASCII (client font) and at most 30 characters (``penguin.nickname`` is
VARCHAR(30)). Local chat lines are ASCII too, like every existing fallback.
"""
import json
import re
from pathlib import Path

from . import actions
from . import safety

NICKNAME_MAX = 30
LOCAL_LINE_MAX = 54  # the AS2 chat box, same as behavior.max_message_length

USERNAME_RE = re.compile(r'^[a-z0-9]{4,12}$')
NICKNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ]{0,%d}$' % (NICKNAME_MAX - 1))
_TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
_CARD_KEYS = frozenset({
    'username', 'nickname', 'aliases', 'home_room', 'home_pois', 'archetype', 'voice', 'likes',
    'dislikes', 'knows', 'relationships', 'schedule', 'wander', 'idle_actions', 'topics',
    'chattiness', 'greetings', 'fallbacks', 'music', 'appearance', 'enabled', 'personality',
})
_LIST_MAX = 12
_TEXT_MAX = 200


class CastError(ValueError):
    """Invalid persona card; the message names the file and the field."""


class Block:
    """One schedule block, in minutes after local midnight (end < start wraps past midnight)."""

    __slots__ = ('start', 'end', 'room', 'activity', 'chance')

    def __init__(self, start, end, room, activity, chance):
        self.start, self.end, self.room, self.activity, self.chance = start, end, room, activity, chance

    def covers(self, minute):
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end


class Card:
    def __init__(self, username, nickname=None, aliases=(), home_room=None, home_pois=(),
                 archetype='', voice='', likes=(), dislikes=(), knows=(), relationships=(),
                 schedule=(), wander_home_share=0.75, wander_favorites=(), idle_actions=(),
                 topics=(), chattiness=0.5, greetings=(), fallbacks=(), music=True,
                 appearance_color=None, appearance_items=(), enabled=True, personality=''):
        self.provided = frozenset()
        self.username = username
        self.nickname = nickname
        self.aliases = tuple(aliases)
        self.home_room = home_room
        self.home_pois = tuple(home_pois)
        self.archetype = archetype
        self.voice = voice
        self.likes, self.dislikes, self.knows = tuple(likes), tuple(dislikes), tuple(knows)
        self.relationships = tuple(relationships)
        self.schedule = tuple(schedule)
        self.wander_home_share = wander_home_share
        self.wander_favorites = tuple(wander_favorites)
        self.idle_actions = tuple(idle_actions)
        self.topics = tuple(topics)
        self.chattiness = chattiness
        self.greetings, self.fallbacks = tuple(greetings), tuple(fallbacks)
        self.music = music
        self.appearance_color = appearance_color
        self.appearance_items = tuple(appearance_items)
        self.enabled = enabled
        self.personality = personality

    def personality_text(self):
        """Prompt text for the existing system prompt ("Lo que sabes y haces: ...")."""
        if self.personality:
            return self.personality
        parts = []
        if self.archetype:
            parts.append(self.archetype[0].upper() + self.archetype[1:] + '.')
        if self.voice:
            parts.append(f'Tu voz: {self.voice}')
        for label, items in (('Te gusta', self.likes), ('No te gusta', self.dislikes), ('Sabes de', self.knows)):
            if items:
                parts.append(f'{label}: ' + ', '.join(items[:4]) + '.')
        if self.relationships:
            parts.append('Tu gente: ' + ', '.join(
                f"{item['with']} ({item['kind'].replace('_', ' ')})" for item in self.relationships[:4]) + '.')
        return ' '.join(parts)

    def missing_for_new_character(self):
        """Fields a brand-new character needs (it has no Secret entry to fall back on)."""
        required = {'nickname': self.nickname, 'home_room': self.home_room,
                    'archetype': self.archetype, 'voice': self.voice,
                    'greetings': self.greetings, 'fallbacks': self.fallbacks}
        return [name for name, value in required.items() if not value]


# ----------------------------------------------------------------- parsing
def _text(value, where, *, maximum=_TEXT_MAX):
    if not isinstance(value, str) or not value.strip():
        raise CastError(f'{where}: must be non-empty text')
    if len(value) > maximum:
        raise CastError(f'{where}: longer than {maximum} characters')
    if any(ord(c) < 32 for c in value):
        raise CastError(f'{where}: contains control characters')
    return value.strip()


def _texts(value, where, *, maximum=_TEXT_MAX):
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _LIST_MAX:
        raise CastError(f'{where}: must be a list of at most {_LIST_MAX} texts')
    return tuple(_text(item, f'{where}[{i}]', maximum=maximum) for i, item in enumerate(value))


def _number(value, where, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise CastError(f'{where}: must be a number between {low} and {high}, got {value!r}')
    return float(value)


def _room_id(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CastError(f'{where}: must be a positive room id, got {value!r}')
    return value


def _lines(value, where, output_filter):
    lines = _texts(value, where, maximum=LOCAL_LINE_MAX)
    for index, line in enumerate(lines):
        problem = safety.check_local_line(line, LOCAL_LINE_MAX, output_filter)
        if problem:
            raise CastError(f'{where}[{index}]: {problem}')
    return lines


def _block(raw, where):
    if not isinstance(raw, dict) or set(raw) - {'from', 'to', 'room', 'activity', 'chance'}:
        raise CastError(f'{where}: needs from, to, room, activity (and optional chance)')
    times = []
    for key in ('from', 'to'):
        match = _TIME_RE.match(str(raw.get(key, '')))
        if not match:
            raise CastError(f'{where}.{key}: must be HH:MM (24 h), got {raw.get(key)!r}')
        times.append(int(match.group(1)) * 60 + int(match.group(2)))
    if times[0] == times[1]:
        raise CastError(f'{where}: "from" and "to" are equal; say which hours you mean')
    activity = raw.get('activity')
    if activity not in actions.ACTIVITIES:
        raise CastError(f'{where}.activity: unknown {activity!r} (known: {sorted(actions.ACTIVITIES)})')
    chance = _number(raw.get('chance', 1.0), f'{where}.chance', 0.0, 1.0)
    return Block(times[0], times[1], _room_id(raw.get('room'), f'{where}.room'), activity, chance)


def parse_card(raw, where='card', output_filter=None):
    """Validate one card document and return a :class:`Card`."""
    if not isinstance(raw, dict):
        raise CastError(f'{where}: must be a JSON object')
    unknown = set(raw) - _CARD_KEYS
    if unknown:
        raise CastError(f'{where}: unknown keys {sorted(unknown)} (typo?)')
    for key in ('relationships', 'schedule'):
        if key in raw and (not isinstance(raw[key], list) or len(raw[key]) > _LIST_MAX):
            raise CastError(f'{where}.{key}: must be a list of at most {_LIST_MAX} entries')
    username = raw.get('username')
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise CastError(f'{where}: username must be 4-12 lowercase letters or digits, got {username!r}')
    where = f'{where} ({username})'

    nickname = raw.get('nickname')
    if nickname is not None and (not isinstance(nickname, str) or not NICKNAME_RE.match(nickname)):
        raise CastError(f'{where}: nickname must be ASCII letters, digits and spaces, '
                        f'1-{NICKNAME_MAX} characters, got {nickname!r}')
    aliases = _texts(raw.get('aliases'), f'{where}.aliases', maximum=NICKNAME_MAX)
    for alias in aliases:
        if not NICKNAME_RE.match(alias):
            raise CastError(f'{where}.aliases: {alias!r} must be ASCII letters, digits and spaces')
    if len({a.casefold() for a in aliases}) != len(aliases):
        raise CastError(f'{where}.aliases: repeated alias')

    home_pois = _texts(raw.get('home_pois'), f'{where}.home_pois', maximum=40)
    for poi in home_pois:
        if poi not in actions.KNOWN_POIS:
            raise CastError(f'{where}.home_pois: unknown point of interest {poi!r} '
                            f'(known: {sorted(actions.KNOWN_POIS)})')

    relationships = []
    for index, item in enumerate(raw.get('relationships') or []):
        item_where = f'{where}.relationships[{index}]'
        if not isinstance(item, dict) or set(item) - {'with', 'kind', 'note'} or not {'with', 'kind'} <= set(item):
            raise CastError(f'{item_where}: needs "with" and "kind" (and optional "note")')
        entry = {'with': _text(item['with'], f'{item_where}.with', maximum=NICKNAME_MAX),
                 'kind': _text(item['kind'], f'{item_where}.kind', maximum=40)}
        if not re.match(r'^[a-z_]+$', entry['kind']):
            raise CastError(f'{item_where}.kind: lowercase words joined by "_"')
        if item.get('note') is not None:
            entry['note'] = _text(item['note'], f'{item_where}.note')
        relationships.append(entry)

    schedule = [_block(item, f'{where}.schedule[{i}]') for i, item in enumerate(raw.get('schedule') or [])]

    wander = raw.get('wander') or {}
    if not isinstance(wander, dict) or set(wander) - {'home_share', 'favorites'}:
        raise CastError(f'{where}.wander: only home_share and favorites are allowed')
    favorites = wander.get('favorites') or []
    if not isinstance(favorites, list):
        raise CastError(f'{where}.wander.favorites: must be a list of room ids')

    idle_actions = _texts(raw.get('idle_actions'), f'{where}.idle_actions', maximum=20)
    for action in idle_actions:
        if action not in actions.BODY_ACTIONS:
            raise CastError(f'{where}.idle_actions: unknown action {action!r} '
                            f'(verified: {sorted(actions.BODY_ACTIONS)})')

    appearance = raw.get('appearance') or {}
    if not isinstance(appearance, dict) or set(appearance) - {'color', 'items'}:
        raise CastError(f'{where}.appearance: only color and items are allowed')
    color = appearance.get('color')
    if color is not None and (isinstance(color, bool) or not isinstance(color, int) or color < 0):
        raise CastError(f'{where}.appearance.color: null or a non-negative item id')
    items = appearance.get('items') or []
    if (not isinstance(items, list) or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in items)):
        raise CastError(f'{where}.appearance.items: must be a list of positive item ids')

    for flag in ('music', 'enabled'):
        if flag in raw and not isinstance(raw[flag], bool):
            raise CastError(f'{where}.{flag}: must be true or false')

    card = Card(
        username=username, nickname=nickname, aliases=aliases,
        home_room=_room_id(raw['home_room'], f'{where}.home_room') if 'home_room' in raw else None,
        home_pois=home_pois,
        archetype=_text(raw['archetype'], f'{where}.archetype') if 'archetype' in raw else '',
        voice=_text(raw['voice'], f'{where}.voice', maximum=400) if 'voice' in raw else '',
        likes=_texts(raw.get('likes'), f'{where}.likes'),
        dislikes=_texts(raw.get('dislikes'), f'{where}.dislikes'),
        knows=_texts(raw.get('knows'), f'{where}.knows'),
        relationships=relationships, schedule=schedule,
        wander_home_share=_number(wander.get('home_share', 0.75), f'{where}.wander.home_share', 0.0, 1.0),
        wander_favorites=[_room_id(room, f'{where}.wander.favorites') for room in favorites],
        idle_actions=idle_actions, topics=_texts(raw.get('topics'), f'{where}.topics', maximum=40),
        chattiness=_number(raw.get('chattiness', 0.5), f'{where}.chattiness', 0.0, 1.0),
        greetings=_lines(raw.get('greetings'), f'{where}.greetings', output_filter),
        fallbacks=_lines(raw.get('fallbacks'), f'{where}.fallbacks', output_filter),
        music=raw.get('music', True), appearance_color=color, appearance_items=items,
        enabled=raw.get('enabled', True),
        personality=_text(raw['personality'], f'{where}.personality', maximum=800) if 'personality' in raw else '')
    card.provided = frozenset(raw)
    return card


def _card_documents(path):
    """(label, document) pairs from a directory of files or one file."""
    path = Path(path)
    if path.is_dir():
        files = sorted(item for item in path.glob('*.json') if item.is_file())
    elif path.is_file():
        files = [path]
    else:
        raise CastError(f'cast path {path} does not exist')
    for file in files:
        try:
            document = json.loads(file.read_text(encoding='utf-8'))
        except OSError as error:
            raise CastError(f'{file.name}: cannot be read ({error.strerror or error})') from None
        except json.JSONDecodeError as error:
            raise CastError(f'{file.name}: not valid JSON ({error})') from None
        if isinstance(document, dict) and isinstance(document.get('cast'), list):
            document = document['cast']
        for index, item in enumerate(document if isinstance(document, list) else [document]):
            yield (file.name if not isinstance(document, list) else f'{file.name}[{index}]'), item


def load_cards(path, output_filter=None):
    """All cards under ``path`` (a directory or a file) as ``{username: Card}``."""
    cards = {}
    nicknames = {}
    for label, raw in _card_documents(path):
        card = parse_card(raw, label, output_filter)
        if card.username in cards:
            raise CastError(f'{label}: username {card.username!r} appears twice')
        cards[card.username] = card
    seen = {}
    for card in cards.values():
        for name in [card.nickname, card.username] + list(card.aliases):
            if not name:
                continue
            key = name.casefold()
            if key in seen and seen[key] != card.username:
                raise CastError(f'{card.username}: name {name!r} is already used by {seen[key]!r} '
                                '(two penguins would answer to it)')
            seen[key] = card.username
        if card.nickname:
            nicknames[card.nickname.casefold()] = card.username
    return cards

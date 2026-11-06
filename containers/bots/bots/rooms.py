"""Room catalog: where a penguin may stand, per room.

Houdini does not validate ``u#sp``: it relays any coordinates, so a point that
is not measured lets a bot walk on a wall. This catalog is therefore the only
source of coordinates for cast bots, and it refuses to hold a guess:

- a room is ``measured: true`` only when its walkable rectangles come from the
  room's own ``block_mc`` collision shape (FFDec method of docs/bots.md,
  "Zonas medidas"), and it says in ``source`` how;
- an unmeasured room may carry no rectangles and no points of interest. Its
  only coordinate is the designer's own ``start_x/start_y`` read from the SWF
  (``spawn`` + ``spawn_source``), where an assigned bot holds one fixed post.

Coordinates are raw AS2 stage coordinates, the ones sent in ``j#jr``/``u#sp``.
Rectangles are inclusive on both ends and must lie inside the 1520x960 scene.
"""
import json
from pathlib import Path

from . import actions

SCENE_WIDTH, SCENE_HEIGHT = 1520, 960
DATA_PATH = Path(__file__).resolve().parent / 'data' / 'rooms.json'
_ROOM_KEYS = {'name', 'swf', 'measured', 'source', 'client_checked', 'spawn', 'spawn_source',
              'walk', 'pois', 'exits', 'bot_cap'}


class RoomError(ValueError):
    """Invalid room catalog; the message names the room and the field."""


class Rect:
    """Inclusive integer rectangle in stage coordinates."""

    __slots__ = ('min_x', 'max_x', 'min_y', 'max_y')

    def __init__(self, min_x, max_x, min_y, max_y):
        self.min_x, self.max_x, self.min_y, self.max_y = min_x, max_x, min_y, max_y

    def contains(self, x, y):
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def center(self):
        return (self.min_x + self.max_x) // 2, (self.min_y + self.max_y) // 2

    def size(self):
        return (self.max_x - self.min_x + 1) * (self.max_y - self.min_y + 1)

    def point(self, rng):
        return rng.randint(self.min_x, self.max_x), rng.randint(self.min_y, self.max_y)

    def as_tuple(self):
        return self.min_x, self.max_x, self.min_y, self.max_y

    def __eq__(self, other):
        return isinstance(other, Rect) and self.as_tuple() == other.as_tuple()

    def __hash__(self):
        return hash(self.as_tuple())

    def __repr__(self):
        return f'Rect(x {self.min_x}..{self.max_x}, y {self.min_y}..{self.max_y})'


class Poi:
    """A named place in a measured room, with the body action that fits it."""

    __slots__ = ('name', 'rect', 'action', 'source')

    def __init__(self, name, rect, action, source):
        self.name, self.rect, self.action, self.source = name, rect, action, source


class Room:
    def __init__(self, room_id, name, swf, measured, source, client_checked, spawn,
                 spawn_source, walk, pois, exits, bot_cap):
        self.id = room_id
        self.name = name
        self.swf = swf
        self.measured = measured
        self.source = source
        self.client_checked = client_checked
        self.spawn = spawn              # (x, y) or None
        self.spawn_source = spawn_source
        self.walk = tuple(walk)         # tuple[Rect]
        self.pois = dict(pois)          # name -> Poi
        self.exits = tuple(exits)
        self.bot_cap = bot_cap

    def allows(self, x, y):
        """True only for a point the catalog vouches for (never a guess)."""
        if any(rect.contains(x, y) for rect in self.walk):
            return True
        if any(poi.rect.contains(x, y) for poi in self.pois.values()):
            return True
        return self.spawn is not None and (x, y) == self.spawn

    def walk_point(self, rng):
        """Random point on the measured floor, areas weighted. None if unmeasured."""
        if not self.walk:
            return None
        pick = rng.randrange(sum(rect.size() for rect in self.walk))
        for rect in self.walk:
            if pick < rect.size():
                return rect.point(rng)
            pick -= rect.size()
        return self.walk[-1].point(rng)  # pragma: no cover (unreachable)

    def poi_point(self, name, rng):
        poi = self.pois.get(name)
        return None if poi is None else poi.rect.point(rng)

    def post(self, poi_names, rng):
        """The point a bot homed here stands on, and the POI it belongs to (or None).

        First of ``poi_names`` that this room really has, else the room's own spawn.
        An unmeasured room has no POIs, so the answer is its fixed spawn post.
        """
        for name in poi_names:
            if name in self.pois:
                return self.poi_point(name, rng), self.pois[name]
        return self.spawn, None


class Catalog:
    def __init__(self, rooms):
        self.rooms = dict(rooms)

    def get(self, room_id):
        return self.rooms.get(int(room_id))

    def require(self, room_id):
        room = self.get(room_id)
        if room is None:
            raise RoomError(f'room {room_id} is not in the room catalog')
        return room

    def ids(self):
        return sorted(self.rooms)

    def measured_ids(self):
        return sorted(room_id for room_id, room in self.rooms.items() if room.measured)

    def __contains__(self, room_id):
        return int(room_id) in self.rooms


# ----------------------------------------------------------------- parsing
def _int(value, where):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomError(f'{where}: must be an integer, got {value!r}')
    return value


def _rect(raw, where, *, point_ok):
    if not isinstance(raw, dict):
        raise RoomError(f'{where}: needs min_x, max_x, min_y, max_y')
    if set(raw) - {'min_x', 'max_x', 'min_y', 'max_y', 'action', 'source'}:
        raise RoomError(f'{where}: unexpected keys {sorted(set(raw) - {"min_x", "max_x", "min_y", "max_y", "action", "source"})}')
    try:
        values = [_int(raw[key], f'{where}.{key}') for key in ('min_x', 'max_x', 'min_y', 'max_y')]
    except KeyError as missing:
        raise RoomError(f'{where}: missing {missing}') from None
    rect = Rect(*values)
    if rect.max_x < rect.min_x or rect.max_y < rect.min_y:
        raise RoomError(f'{where}: upper bounds must not be below lower bounds')
    if not point_ok and (rect.max_x == rect.min_x or rect.max_y == rect.min_y):
        raise RoomError(f'{where}: a walk rectangle must span more than one tile on each axis')
    if not (0 <= rect.min_x and rect.max_x <= SCENE_WIDTH and 0 <= rect.min_y and rect.max_y <= SCENE_HEIGHT):
        raise RoomError(f'{where}: outside the {SCENE_WIDTH}x{SCENE_HEIGHT} scene')
    return rect


def _point(raw, where):
    if not isinstance(raw, dict) or set(raw) != {'x', 'y'}:
        raise RoomError(f'{where}: needs exactly x and y')
    x, y = _int(raw['x'], f'{where}.x'), _int(raw['y'], f'{where}.y')
    if not (0 <= x <= SCENE_WIDTH and 0 <= y <= SCENE_HEIGHT):
        raise RoomError(f'{where}: outside the {SCENE_WIDTH}x{SCENE_HEIGHT} scene')
    return x, y


def _text(raw, key, where, *, required=True):
    value = raw.get(key)
    if value is None or value == '':
        if required:
            raise RoomError(f'{where}: "{key}" is required')
        return ''
    if not isinstance(value, str):
        raise RoomError(f'{where}: "{key}" must be text')
    return value


def _parse_room(room_id, raw):
    where = f'room {room_id}'
    if not isinstance(raw, dict):
        raise RoomError(f'{where}: must be an object')
    unknown = set(raw) - _ROOM_KEYS
    if unknown:
        raise RoomError(f'{where}: unexpected keys {sorted(unknown)}')
    measured = raw.get('measured')
    if not isinstance(measured, bool):
        raise RoomError(f'{where}: "measured" must be true or false')
    source = _text(raw, 'source', where)
    client_checked = raw.get('client_checked', False)
    if not isinstance(client_checked, bool):
        raise RoomError(f'{where}: "client_checked" must be true or false')
    walk = [_rect(item, f'{where}.walk[{i}]', point_ok=False) for i, item in enumerate(raw.get('walk') or [])]
    pois_raw = raw.get('pois') or {}
    if not isinstance(pois_raw, dict):
        raise RoomError(f'{where}: "pois" must be an object')
    spawn = _point(raw['spawn'], f'{where}.spawn') if raw.get('spawn') is not None else None
    spawn_source = _text(raw, 'spawn_source', where, required=spawn is not None)

    if not measured:
        # The no-guessing rule, enforced: nothing walkable without a measurement.
        if walk or pois_raw:
            raise RoomError(f'{where}: an unmeasured room cannot carry walk rectangles or points of interest')
        if client_checked:
            raise RoomError(f'{where}: an unmeasured room cannot be client-checked')
    else:
        if not walk:
            raise RoomError(f'{where}: a measured room needs at least one walk rectangle')
        if spawn is None:
            raise RoomError(f'{where}: a measured room needs a spawn')

    pois = {}
    for name, item in pois_raw.items():
        item_where = f'{where}.pois.{name}'
        if name not in actions.KNOWN_POIS:
            raise RoomError(f'{item_where}: unknown point of interest (known: {sorted(actions.KNOWN_POIS)})')
        rect = _rect(item, item_where, point_ok=True)
        action = item.get('action')
        if action is not None and action not in actions.BODY_ACTIONS:
            raise RoomError(f'{item_where}: unknown action {action!r}')
        poi_source = _text(item, 'source', item_where, required=False)
        inside_walk = all(any(w.contains(x, y) for w in walk)
                          for x, y in ((rect.min_x, rect.min_y), (rect.max_x, rect.min_y),
                                       (rect.min_x, rect.max_y), (rect.max_x, rect.max_y)))
        if not inside_walk and not poi_source:
            raise RoomError(f'{item_where}: lies outside every walk rectangle, so it needs its own "source"')
        pois[name] = Poi(name, rect, action, poi_source)

    room = Room(room_id, _text(raw, 'name', where), _text(raw, 'swf', where), measured, source,
                client_checked, spawn, spawn_source, walk, pois,
                _parse_exits(raw.get('exits') or [], where), _int(raw.get('bot_cap', 3), f'{where}.bot_cap'))
    if room.bot_cap < 1:
        raise RoomError(f'{where}: bot_cap must be at least 1')
    if measured and not any(rect.contains(*spawn) for rect in list(walk) + [p.rect for p in pois.values()]):
        raise RoomError(f'{where}: the spawn is not inside a walk rectangle or a point of interest')
    if not measured and room.exits:
        raise RoomError(f'{where}: exits require measurement')
    return room


def _parse_exits(raw, where):
    if not isinstance(raw, list):
        raise RoomError(f'{where}: "exits" must be a list')
    exits = []
    for index, item in enumerate(raw):
        item_where = f'{where}.exits[{index}]'
        if not isinstance(item, dict) or set(item) - {'to', 'x', 'y', 'source'}:
            raise RoomError(f'{item_where}: needs to, x, y and source')
        x, y = _point({'x': item.get('x'), 'y': item.get('y')}, item_where)
        exits.append({'to': _int(item.get('to'), f'{item_where}.to'), 'x': x, 'y': y,
                      'source': _text(item, 'source', item_where)})
    return exits


def parse_catalog(document):
    if not isinstance(document, dict) or document.get('version') != 1:
        raise RoomError('room catalog: "version" must be 1')
    scene = document.get('scene')
    if scene != {'width': SCENE_WIDTH, 'height': SCENE_HEIGHT}:
        raise RoomError(f'room catalog: "scene" must be {SCENE_WIDTH}x{SCENE_HEIGHT}')
    raw_rooms = document.get('rooms')
    if not isinstance(raw_rooms, dict) or not raw_rooms:
        raise RoomError('room catalog: "rooms" must be a non-empty object')
    rooms = {}
    for key, raw in raw_rooms.items():
        try:
            room_id = int(key)
        except ValueError:
            raise RoomError(f'room catalog: room id {key!r} is not a number') from None
        if room_id <= 0 or str(room_id) != key:
            raise RoomError(f'room catalog: room id {key!r} must be a positive integer')
        rooms[room_id] = _parse_room(room_id, raw)
    return Catalog(rooms)


def load_catalog(path=None):
    path = Path(path) if path else DATA_PATH
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except OSError as error:
        raise RoomError(f'room catalog {path} cannot be read: {error.strerror or error}') from None
    except json.JSONDecodeError as error:
        raise RoomError(f'room catalog {path} is not valid JSON: {error}') from None
    return parse_catalog(document)

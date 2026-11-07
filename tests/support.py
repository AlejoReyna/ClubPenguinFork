"""Shared test helpers: repo paths, config factory, fake world.

Nothing here talks to Moonshot or to the real Houdini; the fake world server
implements exactly the protocol subset the bots use (see bots/as2.py docstring).
"""
import asyncio
import contextlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / 'containers' / 'bots') not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / 'containers' / 'bots'))

from bots import as2  # noqa: E402


def config_document_world(port, **overrides):
    """A minimal valid configuration pointing every connection at ``port``.

    ``overrides`` use dotted keys, e.g. ``{'provider.mode': 'simulated'}``.
    """
    document = {
        'server': {
            'login_host': '127.0.0.1', 'login_port': port,
            'world_host': '127.0.0.1', 'world_port': port,
            'connect_timeout': 3.0, 'auth_timeout': 5.0,
        },
        'provider': {'mode': 'simulated', 'model': 'kimi-k2.6'},
        'policy': {'requests_per_minute': 10, 'queue_size': 4,
                   'player_cooldown': 0.2, 'bot_min_gap': 0.1,
                   'memory_turns': 4, 'memory_ttl': 120.0},
        'behavior': {'max_message_length': 40, 'walk_min_delay': 0.2,
                     'walk_max_delay': 0.4, 'respond_to_bots': False},
        'bots': [
            {'username': 'guiaia', 'password': 'secreto1', 'nickname': 'GuiaIA',
             'room': 100, 'aliases': ['Guia'], 'personality': 'Guia de la isla',
             'walk': {'min_x': 10, 'max_x': 60, 'min_y': 20, 'max_y': 90}},
            {'username': 'pescadoria', 'password': 'secreto2',
             'nickname': 'PescadorIA', 'room': 100, 'aliases': [],
             'personality': 'Pescador veterano'},
        ],
    }
    for key, value in overrides.items():
        section, _, field = key.partition('.')
        if field:
            document.setdefault(section, {})[field] = value
        else:
            document[section] = value
    return document


class FakeWorld:
    """Minimal Houdini stand-in on one port for both login and world roles.

    Deterministic selector: a login whose hashed password ends with a login
    key that this server previously issued is the world login; anything else
    is the login-server flow. Any hash is accepted because bots use the exact
    real password formulas (verified against vendor sources) and password
    strength is the server's concern, not the fake's.
    """

    SERVER_KEY = 'kotestkey01'

    def __init__(self, nicknames=None):
        self.nicknames = dict(nicknames or {})
        self.port = None
        self._server = None
        self.connections = 0
        self.logins_rejected = 0
        self.reject_codes = {}  # username -> server error code (login flow)
        self.rooms = {}
        self.worlds_connected = []
        self.chats = []
        self.moves = []
        self.actions = []
        self.frames = []
        self.join_errors = {}  # room -> queued error codes
        self.heartbeats = 0
        self.account_ids = {}
        self._player_id = 100
        self._login_keys = set()
        self._world_nicknames = {}  # player_id -> nickname (per rooms)

    def _next_id(self):
        self._player_id += 1
        return self._player_id

    async def start(self):
        self._server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def kick(self, player_id):
        """Drop one player's live TCP link (reconnect tests)."""
        for room in self.rooms.values():
            player = room.get(player_id)
            if player is not None:
                asyncio.get_running_loop().create_task(player['conn'].close())

    async def broadcast_human_chat(self, room, player_id, message):
        """A human walks in (ap) and speaks (sm broadcast to everyone)."""
        string = f'{player_id}|Humano{player_id}|0|1|0|0|0|0|0|0|0|0|120|320|1|0|0|0|'
        self.rooms.setdefault(room, {})[player_id] = {'conn': _NullConn(),
                                                      'string': string}
        await room_broadcast(self.rooms[room], f'%xt%ap%-1%{string}%')
        await room_broadcast(self.rooms[room], f'%xt%sm%-1%{player_id}%{message}%')

    # ------------------------------------------------------------------ server
    async def _handle(self, reader, writer):
        conn = as2.AS2Connection(reader, writer)
        self.connections += 1
        try:
            await self._session(conn)
        except (as2.Disconnected, as2.FramingError, TimeoutError,
                asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
            self._purge(conn)

    def _purge(self, conn):
        for members in list(self.rooms.values()):
            for player_id, player in list(members.items()):
                if player['conn'] is conn:
                    del members[player_id]
                    asyncio.get_running_loop().create_task(
                        room_broadcast(
                            members, f'%xt%rp%-1%{player_id}%'))
                    self.worlds_connected = [
                        pid for pid in self.worlds_connected if pid != player_id]

    async def _session(self, conn):
        raw = await conn.read_line(5.0)
        if not raw.startswith('<msg') or 'verChk' not in raw:
            return
        await conn.send_line('<msg t="sys"><body action="apiOK" r="0"/></msg>')
        request = await conn.read_line(5.0)
        if 'action="rndK"' not in request:
            return
        await conn.send_line('<msg t="sys"><body action="rndK" r="-1">'
                             f'<k>{self.SERVER_KEY}</k></body></msg>')
        login_xml = await conn.read_line(5.0)
        if not login_xml.startswith('<msg') or 'action="login"' not in login_xml:
            return
        username, password = _login_fields(login_xml)
        current_key = f'k{self.connections:08d}'
        # The world token embeds a key previously issued by the login flow;
        # login-server hashes are hex-only, so 'k' endings cannot collide.
        # Only keys from earlier connections count: the reply below hands out
        # `current_key`, which must never authenticate this same connection.
        is_world = password is not None and any(
            password.endswith(key) for key in self._login_keys)
        self._login_keys.add(current_key)

        reject_code = self.reject_codes.get(username.lower())
        if reject_code is not None and not is_world:
            self.logins_rejected += 1
            await conn.send_line(f'%xt%e%-1%{reject_code}%')
            return

        player_id = self.account_ids.setdefault(username.lower(), self._next_id())
        if not is_world:
            await conn.send_line(
                f'%xt%l%-1%{player_id}%{current_key}%-1%3101,0%')
            return
        # world login with the handed-out key
        await conn.send_line('%xt%l%-1%')
        while True:
            raw = await conn.read_line(5.0)
            if raw.startswith('%xt%s%j#js%'):
                await conn.send_line('%xt%js%-1%')
                break  # spawn join chatter is not needed by the fake
        await self._room_flow(conn, player_id, username)

    async def _room_flow(self, conn, player_id, username):
        nickname = self.nicknames.get(username.lower(), username)
        string = _player_string(player_id, nickname)
        self._world_nicknames[player_id] = nickname
        room_id = None
        while True:
            raw = await conn.read_line(120.0)
            if not raw.startswith('%xt%s%'):
                continue
            fields = raw.split('%')[1:-1]
            action = fields[2] if len(fields) > 2 else ''
            if action == 'j#jr':
                destination = int(fields[4])
                errors = self.join_errors.get(destination, [])
                if errors:
                    await conn.send_line(f'%xt%e%-1%{errors.pop(0)}%')
                    continue
                if room_id is not None:
                    old = self.rooms.get(room_id, {})
                    old.pop(player_id, None)
                    await room_broadcast(old, f'%xt%rp%-1%{player_id}%')
                room_id = int(fields[4])
                parts = string.split('|')
                parts[12:14] = fields[5:7]
                string = '|'.join(parts)
                members = self.rooms.setdefault(room_id, {})
                others = {pid: member for pid, member in members.items()
                          if pid != player_id}
                members[player_id] = {'conn': conn, 'string': string}
                roster = '%'.join(m['string'] for m in others.values())
                await conn.send_line(f'%xt%jr%-1%{room_id}%{roster}'
                                     + ('%' if others else '%'))
                self.worlds_connected.append(player_id)
                asyncio.get_running_loop().create_task(
                    room_broadcast(others, f'%xt%ap%-1%{string}%'))
            elif action == 'm#sm':
                sender, message = fields[4], fields[5]
                self.chats.append((room_id, sender, message))
                asyncio.get_running_loop().create_task(
                    room_broadcast(self.rooms.get(room_id, {}),
                                   f'%xt%sm%-1%{sender}%{message}%'))
            elif action == 'j#grs':
                roster = '%'.join(m['string'] for m in self.rooms.get(room_id, {}).values())
                await conn.send_line(f'%xt%grs%-1%{room_id}%{roster}%')
            elif action == 'u#sp':
                self.moves.append((room_id, fields[4], fields[5]))
                parts = string.split('|')
                parts[12:14] = fields[4:6]
                string = '|'.join(parts)
                self.rooms[room_id][player_id]['string'] = string
                asyncio.get_running_loop().create_task(
                    room_broadcast(self.rooms.get(room_id, {}),
                                    f'%xt%sp%-1%{player_id}%{fields[4]}%{fields[5]}%'))
            elif action == 'u#sa':
                action_id = fields[4]
                self.actions.append((room_id, player_id, action_id))
                asyncio.get_running_loop().create_task(
                    room_broadcast(self.rooms.get(room_id, {}),
                                   f'%xt%sa%-1%{player_id}%{action_id}%'))
            elif action == 'u#sf':
                self.frames.append((room_id, player_id, fields[4]))
                await room_broadcast(self.rooms.get(room_id, {}),
                                     f'%xt%sf%-1%{player_id}%{fields[4]}%')
            elif action == 'u#h':
                self.heartbeats += 1
                await conn.send_line('%xt%h%-1%')

    def nickname_of(self, player_id):
        return self._world_nicknames.get(player_id)


class _NullConn:
    """A member record used only to run a synthetic broadcast in tests."""

    async def send_line(self, payload):
        return None

    async def close(self):
        return None

    @property
    def closed(self):
        return False


async def room_broadcast(members, frame):
    for member in list(members.values()):
        conn = member['conn']
        if isinstance(conn, _NullConn):
            continue
        with contextlib.suppress(Exception):
            await conn.send_line(frame)


def _player_string(player_id, nickname):
    x = 50 + player_id
    y = 100 + player_id
    return f'{player_id}|{nickname}|1|1|1|0|0|0|0|0|0|0|{x}|{y}|1|0|0|0|'


def _login_fields(raw):
    root = ET.fromstring(raw)
    return root.findtext('./body/login/nick'), root.findtext('./body/login/pword')

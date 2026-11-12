"""AS2 wire client, aligned with the real lab servers.

Formats verified in this repository (scripts/game-smoke.py and the Houdini
handlers under vendor/houdini):
- handshake: XML verChk (v 253, the 2017 vanilla client) -> apiOK, then rndK -> static auth key;
- login server: XML login, password =
  swap(digest(swap(digest(password).upper()) + rndk + 'Y(02.>\'H}t":E1'));
- world server: XML login. Vanilla sends the login-server payload as nick and
  ``client_key#confirmation`` as the password; legacy sends the username and
  swap(digest(login_key + rndk)) + login_key,
  then XT join server ``%xt%s%j#js%-1%<id>%<key>%0%`` and
  room ``%xt%s%j#jr%-1%<room>%<x>%<y>%``;
- chat in: broadcast ``%xt%sm%-1%<id>%<message>%`` (own messages included);
- chat out: ``%xt%s%m#sm%-1%<id>%<message>%`` (the id must match the session
  or the server closes the connection);
- movement: ``%xt%s%u#sp%-1%<x>%<y>%`` broadcast as ``%xt%sp%-1%<id>%<x>%<y>%``;
- emote: ``%xt%s%u#se%-1%<emote>%`` broadcast as ``%xt%se%-1%<id>%<emote>%``;
- action (wave, dance, …): ``%xt%s%u#sa%-1%<action>%`` as ``%xt%sa%-1%<id>%<action>%``;
- frame (dance, sit): ``%xt%s%u#sf%-1%<frame>%`` as ``%xt%sf%-1%<id>%<frame>%``; the
  client's own dance/sit buttons send this one (see actions.py), Houdini keeps it;
- presence: ``%xt%ap%-1%<player string>`` and ``%xt%rp%-1%<id>%``;
- roster refresh: ``%xt%grs%-1%<room>%<strings...>%``;
- igloo join (legacy): ``%xt%s%j#jp%-1%<player_id+1000>%``, answer ``jr`` with
  external id ``player_id+2000``. ``j#grs`` inside an igloo echoes the database
  row id, not that external id, so the join does not use the ``grs`` barrier;
- igloo map: ``%xt%s%g#or%-1%`` opens, ``%xt%s%g#cr%-1%`` closes. Neither
  packet gets a reply;
- errors: ``%xt%e%-1%<code>%``.

Frames are NUL-terminated lines. The reader yields frames to the session as
they arrive, so provider latency never stalls packet processing.
"""
import asyncio
import collections
import hashlib
import re

STATIC_HASH_SUFFIX = 'Y(02.>\'H}t":E1'
# Basic igloo (igloos/1.swf) designer start. Not a measured walk; hold this post.
IGLOO_POST = (380, 320)
IGLOO_JOIN_OFFSET = 1000
IGLOO_EXTERNAL_OFFSET = 2000


def igloo_external_id(player_id):
    """Room id Houdini puts in ``jr`` for this penguin's igloo."""
    return int(player_id) + IGLOO_EXTERNAL_OFFSET


def is_igloo_room(room_id):
    """True for a Houdini igloo external id. Catalog rooms stay under 1000."""
    return isinstance(room_id, int) and room_id >= IGLOO_EXTERNAL_OFFSET
VER_CHECK = '<msg t="sys"><body action="verChk" r="0"><ver v="253"/></body></msg>'
Login = collections.namedtuple('Login', 'player_id login_key confirmation raw')
RANDOM_KEY_REQUEST = '<msg t="sys"><body action="rndK" r="-1"/></msg>'

_XML_ACTION_RE = re.compile(r'action="([A-Za-z]+)"')
_XML_KEY_RE = re.compile(r'<k>([^<]*)</k>')
_ERROR_CODE_RE = re.compile(r'^%xt%e%-1%(-?\d+)%')
_SEND_RATE_LIMIT_CODE = 7


def digest(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.md5(value).hexdigest()


def _swap(value):
    return value[16:32] + value[:16]


def login_server_password(password, rndk):
    """Password hash expected by the Houdini legacy login server."""
    return _swap(digest(_swap(digest(password).upper()) + rndk + STATIC_HASH_SUFFIX))


def world_login_password(login_key, rndk):
    """Token expected by the Houdini legacy world login."""
    return _swap(digest(login_key + rndk)) + login_key


def _cdata(value):
    """CDATA-safe embedding of XML login fields."""
    return '<![CDATA[' + value.replace(']]>', ']]]]><![CDATA[>') + ']]>'


def _xml_login(username, password):
    return ('<msg t="sys"><body action="login" r="0"><login z="w1">'
            f'<nick>{_cdata(username)}</nick><pword>{_cdata(password)}</pword>'
            '</login></body></msg>')


def parse_login(fields):
    """Login-server ``l`` args. Vanilla packs id|id|name|key|... into the first field."""
    if not fields:
        raise FramingError('login reply without id and key')
    head = str(fields[0])
    if '|' in head:
        parts = head.split('|')
        if len(parts) < 4 or not parts[0].isdigit() or not parts[3]:
            raise FramingError(f'vanilla login reply is malformed {head!r}')
        if len(fields) < 2 or not fields[1]:
            raise FramingError('vanilla login reply without confirmation hash')
        return Login(parts[0], parts[3], fields[1], head)
    if not head.isdigit() or len(fields) < 2:
        raise FramingError('login reply without id and key')
    return Login(head, fields[1], None, None)


class FramingError(Exception):
    """Received data that cannot be parsed as AS2 frames anymore."""


class Disconnected(Exception):
    """The server closed, reset or the link stalled; safe to reconnect."""


class ServerError(Exception):
    """Wire error packet ``%xt%e%-1%<code>%`` with its numeric code."""

    def __init__(self, code):
        super().__init__(f'Server error code {code}')
        self.code = int(code)

    @property
    def auth_related(self):
        # 100 unknown user, 101 bad password, 103 capacity/staff, 150 flood,
        # 601-603 bans, 900 inactive, 910-913 timers/grounding.
        return self.code in {100, 101, 150, 601, 602, 603, 900, 910, 911, 913}


class Frame:
    """One XT broadcast: ``%xt%<op>%-1%<args...>%``."""

    __slots__ = ('op', 'args', 'raw')

    def __init__(self, op, args, raw):
        self.op = op
        self.args = args
        self.raw = raw

    @classmethod
    def parse(cls, raw):
        if not raw.startswith('%xt%') or not raw.endswith('%'):
            raise FramingError('unexpected XT framing')
        fields = raw.split('%')[1:-1]
        if len(fields) < 3 or fields[0] != 'xt':
            raise FramingError('unexpected XT field count')
        return cls(fields[1], fields[3:], raw)

    def as_error(self):
        """ServerError when this frame is an error packet, else None."""
        match = _ERROR_CODE_RE.match(self.raw)
        return ServerError(match.group(1)) if match else None

    def __repr__(self):
        return f'Frame({self.op}, fields={len(self.args)})'


class XMLReply:
    """One XML server reply with its parsed action and optional key."""

    __slots__ = ('action', 'key', 'raw')

    def __init__(self, action, key, raw):
        self.action = action
        self.key = key
        self.raw = raw


class AS2Connection:
    """Reader/writer over one NUL-framed TCP session."""

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    async def connect(cls, host, port, timeout=10.0):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), timeout=timeout)
        except asyncio.TimeoutError:
            raise Disconnected(f'timeout connecting to {host}:{int(port)}') from None
        except OSError as error:
            raise Disconnected(f'cannot connect to {host}:{int(port)}: {error}') from error
        return cls(reader, writer)

    @property
    def closed(self):
        return self._closed

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except OSError:
            pass
        try:
            await self._writer.wait_closed()
        except (OSError, asyncio.IncompleteReadError, ConnectionError):
            pass

    async def send_line(self, payload, timeout=5.0):
        """Send one NUL-terminated frame; a dead link becomes Disconnected."""
        if self._closed:
            raise Disconnected('connection already closed')
        try:
            self._writer.write(str(payload).encode('utf-8') + b'\x00')
            await asyncio.wait_for(self._writer.drain(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise Disconnected('the write queue stalled') from error
        except (ConnectionError, BrokenPipeError) as error:
            raise Disconnected(f'write failed: {error}') from error
        except (OSError, RuntimeError) as error:
            raise Disconnected(f'write failed: {error}') from error

    async def read_line(self, timeout):
        """Wait for the next NUL-terminated frame as decoded text."""
        try:
            line = await asyncio.wait_for(
                self._reader.readuntil(b'\x00'), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError('no frame arrived inside the deadline') from None
        except asyncio.IncompleteReadError as error:
            raise Disconnected('server closed the connection') from error
        except (ConnectionError, OSError) as error:
            raise Disconnected(f'read failed: {error}') from error
        except asyncio.LimitOverrunError as error:
            raise Disconnected('a frame exceeded the reader limit') from error
        if line == b'\x00':  # discard stray empty frames defensively
            return await self.read_line(timeout)
        return line[:-1].decode('utf-8', errors='replace')

    async def read_xml(self, timeout):
        """Next XML block; XT frames and errors are surfaced as exceptions."""
        while True:
            raw = await self.read_line(timeout)
            if raw.startswith('%'):
                frame = Frame.parse(raw)
                error = frame.as_error()
                if error is not None:
                    raise error
                continue  # not an XML reply; skip it
            if not raw.startswith('<'):
                continue
            action_match = _XML_ACTION_RE.search(raw)
            if action_match is None:
                raise FramingError('XML reply without action')
            key_match = _XML_KEY_RE.search(raw)
            return XMLReply(action_match.group(1), key_match.group(1) if key_match else None, raw)

    # -------------------------------------------------- authentication steps
    async def handshake(self, timeout):
        """verChk then rndK; returns the static auth key of this server."""
        await self.send_line(VER_CHECK)
        version = await self.read_xml(timeout)
        if version.action != 'apiOK':
            raise FramingError('the server refuses the protocol version '
                               f'({version.action})')
        await self.send_line(RANDOM_KEY_REQUEST)
        key_reply = await self.read_xml(timeout)
        if key_reply.action != 'rndK' or not key_reply.key:
            raise FramingError('the server did not provide a random key')
        return key_reply.key

    async def login_legacy(self, username, hashed_password, timeout):
        """XML login; returns the XT 'l' fields. The login server uses this for both clients."""
        await self.send_line(_xml_login(username, hashed_password))
        return await self._read_login(timeout)

    async def login_world(self, username, identity, rndk, timeout):
        """World login. Vanilla requires the confirmation hash from the login server."""
        token = world_login_password(identity.login_key, rndk)
        if identity.confirmation:
            await self.send_line(_xml_login(identity.raw, token + '#' + identity.confirmation))
        else:
            await self.send_line(_xml_login(username, token))
        return await self._read_login(timeout)

    async def _read_login(self, timeout):
        """Read until the XT login reply."""
        while True:
            raw = await self.read_line(timeout)
            if raw.startswith('%'):
                frame = Frame.parse(raw)
                error = frame.as_error()
                if error is not None:
                    raise error
                if frame.op == 'l':
                    return frame.args
                continue  # unrelated XT frame; keep waiting for the login
            if raw.startswith('<'):
                action = _XML_ACTION_RE.search(raw)
                if action is not None and action.group(1) == 'l':
                    raise FramingError('vanilla login payload on a legacy flow')
                continue
            raise FramingError('unexpected reply during login')

    # ----------------------------------------------------------- world steps
    async def join_world(self, player_id, login_key, timeout):
        """Send j#js; the server answers with pgu/js/lp and a spawn join."""
        await self.send_line(f'%xt%s%j#js%-1%{player_id}%{login_key}%0%')
        while True:
            raw = await self.read_line(timeout)
            if raw.startswith('%xt%js%'):
                return
            if raw.startswith('%'):
                frame = Frame.parse(raw)
                error = frame.as_error()
                if error is not None:
                    raise error
                continue
            raise FramingError('unexpected reply joining the world')

    async def join_room(self, room, timeout, x=0, y=0):
        """Send j#jr and wait for the room roster (jr) or an error."""
        await self.send_line(f'%xt%s%j#jr%-1%{room}%{int(x)}%{int(y)}%')
        while True:
            raw = await self.read_line(timeout)
            if raw.startswith('%xt%jr%'):
                frame = Frame.parse(raw)
                if frame.args and frame.args[0] == str(room):
                    # An old spawn jr may have been pending. grs is a barrier
                    # after our room request, and gives the current full roster.
                    await self.send_line('%xt%s%j#grs%-1%')
                    while True:
                        current = Frame.parse(await self.read_line(timeout))
                        error = current.as_error()
                        if error:
                            raise error
                        if current.op == 'grs' and current.args and current.args[0] == str(room):
                            return current
                continue
            if raw.startswith('%'):
                frame = Frame.parse(raw)
                error = frame.as_error()
                if error is not None:
                    raise error
                continue
            raise FramingError('unexpected reply joining the room')

    async def join_player_igloo(self, player_id, timeout):
        """Send ``j#jp`` and wait for ``jr`` with external id ``player_id+2000``.

        No ``j#grs`` after it: inside an igloo Houdini's refresh uses the
        ``penguin_igloo_room`` serial, which is not the external id.
        """
        external = igloo_external_id(player_id)
        await self.send_line(f'%xt%s%j#jp%-1%{int(player_id) + IGLOO_JOIN_OFFSET}%')
        while True:
            raw = await self.read_line(timeout)
            if raw.startswith('%xt%jr%'):
                frame = Frame.parse(raw)
                if frame.args and frame.args[0] == str(external):
                    return frame
                continue
            if raw.startswith('%'):
                frame = Frame.parse(raw)
                error = frame.as_error()
                if error is not None:
                    raise error
                continue
            raise FramingError('unexpected reply joining the igloo')

    async def set_igloo_open(self, opened):
        """``g#or`` or ``g#cr``. The legacy handlers send no answer."""
        op = 'or' if opened else 'cr'
        await self.send_line(f'%xt%s%g#{op}%-1%')

    # ------------------------------------------------------------- gameplay
    async def send_chat(self, player_id, message):
        await self.send_line(f'%xt%s%m#sm%-1%{int(player_id)}%{message}%')

    async def send_move(self, x, y):
        await self.send_line(f'%xt%s%u#sp%-1%{int(x)}%{int(y)}%')

    async def send_emote(self, emote):
        await self.send_line(f'%xt%s%u#se%-1%{int(emote)}%')

    async def send_action(self, action):
        await self.send_line(f'%xt%s%u#sa%-1%{int(action)}%')

    async def send_frame(self, frame):
        await self.send_line(f'%xt%s%u#sf%-1%{int(frame)}%')

    async def send_snowball(self, x, y):
        await self.send_line(f'%xt%s%u#sb%-1%{int(x)}%{int(y)}%')

    async def send_heartbeat(self):
        await self.send_line('%xt%s%u#h%-1%')

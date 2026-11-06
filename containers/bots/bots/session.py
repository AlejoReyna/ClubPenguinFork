"""Session lifecycle: authenticate, join the room, run behavior, reconnect.

One :class:`BotSession` per configured bot. It keeps the identity: sessions
always log in with the same account (never re-registering), reconnect with
full-jitter exponential backoff after drops, and treat credential errors as
an operator problem (slow retry) instead of hammering the login server.
Only the room roster, its own id and the configured fellow bots are needed to
avoid echoing itself or other bots. Every frame about the room also feeds the
shared world model (world.py), and ``change_room`` moves a live session to
another room without reconnecting.
"""
import asyncio
import logging
import random
import time

from . import as2
from .behavior import BotBehavior
from .policy import CooldownMap, RateLimiter, ReplyGate

logger = logging.getLogger('bots.session')

_RECONNECT_BASE = 2.0
_RECONNECT_CAP = 60.0
_AUTH_RETRY_CAP = 600.0
_STABLE_SESSION_SECONDS = 300.0  # a drop after this counts as a fresh attempt
_PLAYER_AUTH_ERRORS = {100, 101}  # unknown account, wrong password
_READ_SLICE = 2.0  # how long the packet reader waits before checking for a stop request
# Houdini's j#jr cooldown is 0.5 s (handlers/play/navigation.py); stay above it.
_JOIN_COOLDOWN = 0.6
_ROOM_FULL_OR_COOLDOWN = 210  # Houdini sends 210 for BOTH the cooldown and a full room


class RoomChangeError(Exception):
    """``change_room`` did not complete. ``kind`` is a bounded metric label."""

    KINDS = ('full', 'timeout', 'error')

    def __init__(self, kind, detail=''):
        super().__init__(f'{kind}: {detail}')
        if kind not in self.KINDS:
            raise ValueError(f'unknown room change failure {kind}')
        self.kind = kind


class AuthFailure(Exception):
    """Credentials rejected by the server; needs the operator, not retries."""

    def __init__(self, bot_index, message, retry_seconds):
        super().__init__(message)
        self.retry_seconds = retry_seconds


def _jitter(cap, base=_RECONNECT_BASE, attempts=0):
    """Full jitter exponential backoff with a small floor."""
    attempt = max(0, attempts)
    ceiling = min(cap, base * (2 ** min(attempt, 16)))
    return random.uniform(max(0.5, ceiling * 0.25), ceiling)


class BotSession:
    """Owns one bot's world session and its behavior tasks."""

    def __init__(self, index, bot, server, provider, policy, behavior,
                 metrics, fallback_replies, music=None, world=None, service=None,
                 catalog=None, output_filter=None):
        self.index = index
        self.music = music  # music.MusicClient shared by all bots, or None
        self.world = world  # world.World shared by all sessions, or None
        self.service = service  # config.ServiceConfig (time zone, seed), or None
        self.catalog = catalog  # rooms.Catalog in cast mode, or None
        self.output_filter = output_filter  # safety.OutputFilter, or None for the default
        self.director = None  # opt-in Phase 2; otherwise the legacy reply pipeline owns chat
        self.travel = None
        self.traveling = False
        self.body_lock = asyncio.Lock()
        self.bot = bot
        self.server = server
        self.provider = provider
        self.metrics = metrics

        self.rate_limiter = RateLimiter(policy.requests_per_minute)
        self.player_cooldown = CooldownMap(policy.player_cooldown)
        self.bot_gap = CooldownMap(behavior.bot_chat_interval)
        self.gate = ReplyGate(bot_gap=policy.bot_min_gap,
                              rate_limit=self.rate_limiter,
                              player_cooldown=self.player_cooldown)
        self.behavior = BotBehavior(self, bot, policy, behavior, metrics,
                                    fallback_replies)
        self.mutations_lock = asyncio.Lock()  # serializes writes to the wire
        # One reader per socket: the observer holds this while it waits for a frame, and
        # change_room takes it so as2.join_room can read the answer itself.
        self._reader_lock = asyncio.Lock()
        self._room_change_lock = asyncio.Lock()
        self._last_join = float('-inf')  # monotonic time of the last j#jr

        self.conn = None  # as2.AS2Connection once inside the world
        self.player_id = None
        self.roster = {}
        self.joined_room = False
        self.room_id = None  # the room the server has this bot in right now
        self.stable_since = None

    @property
    def log_context(self):
        return f'[{self.index}]'

    # ---------------------------------------------------------------- runloop
    async def run(self, stop_event):
        """Keep this bot connected until the service stops."""
        attempts = 0
        while not stop_event.is_set():
            delay = 0.0
            credential_rejection = None
            try:
                await self._establish()
                await self._observe(stop_event)
            except as2.ServerError as error:
                credential_rejection, delay = await self._handle_server_error(
                    error, attempts)
                delay = 0.0 if credential_rejection is not None else delay
            except as2.Disconnected as error:
                logger.info('%s disconnected: %s', self.log_context, error)
                delay = self._stable_reset(attempts)
            except (TimeoutError, asyncio.TimeoutError, OSError,
                    as2.FramingError) as error:
                logger.info('%s connection problem: %s%s', self.log_context,
                            type(error).__name__, f': {error}')
                delay = self._stable_reset(attempts)
            await self._shutdown_tasks()
            await self._set_connected(False)
            if credential_rejection is not None:
                raise credential_rejection
            if stop_event.is_set():
                break
            if delay > 0:
                await self.metrics.reconnected(self.index)
                logger.info('%s reconnecting in %.1fs', self.log_context, delay)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    break  # stop signalled during the backoff sleep
                except asyncio.TimeoutError:
                    pass
            attempts += 1

    def _stable_reset(self, attempts):
        """Fresh backoff when the previous session was long lived."""
        stable = (self.stable_since is not None
                  and time.monotonic() - self.stable_since > _STABLE_SESSION_SECONDS)
        if stable:
            return _jitter(_RECONNECT_CAP, attempts=0)
        return _jitter(_RECONNECT_CAP, attempts=attempts)

    async def _handle_server_error(self, error, attempts=0):
        """Classify one error packet.

        Returns ``(AuthFailure|None, reopen_delay)``: player authentication
        codes surface as an :class:`AuthFailure` for the manager (the operator
        must fix the credentials), other auth codes slow the next attempt down
        (flood/ban timers), and plain codes reconnect like any drop.
        """

        def rejection(message):
            return AuthFailure(
                self.index,
                f'{self.log_context} {message} (server code '
                f'{error.code}): verify the bot account credentials',
                retry_seconds=_AUTH_RETRY_CAP)

        if error.auth_related:
            await self.metrics.auth_failed(self.index)
            if error.code in _PLAYER_AUTH_ERRORS:
                return rejection(
                    'login rejected with code ' + str(error.code)), 0.0
            return None, _jitter(_AUTH_RETRY_CAP, attempts=attempts)
        logger.info('%s server error %s', self.log_context, error.code)
        return None, _jitter(_RECONNECT_CAP, attempts=attempts)

    # --------------------------------------------------------------- establish
    async def _establish(self):
        login_conn = await as2.AS2Connection.connect(
            self.server.login_host, self.server.login_port,
            timeout=self.server.connect_timeout)
        try:
            await self._authenticate(login_conn)
        finally:
            await login_conn.close()
        world_conn = await as2.AS2Connection.connect(
            self.server.world_host, self.server.world_port,
            timeout=self.server.connect_timeout)
        try:
            await self._enter_world(world_conn)
        except BaseException:
            await world_conn.close()
            raise
        self.conn = world_conn
        self.player_id = int(self.player_id)
        self.stable_since = time.monotonic()
        await self.behavior.start()
        await self._set_connected(True)
        logger.info('%s bot online (id=%s)', self.log_context, self.player_id)

    async def _authenticate(self, conn):
        rndk = await conn.handshake(self.server.auth_timeout)
        password = as2.login_server_password(self.bot.password, rndk)
        fields = await conn.login_legacy(self.bot.username.lower(), password,
                                         self.server.auth_timeout)
        if len(fields) < 2:
            raise as2.FramingError('login reply without id and key')
        player_id, login_key = fields[0], fields[1]
        if not str(player_id).isdigit():
            raise as2.FramingError(f'login reply has a malformed id {player_id!r}')
        self.player_id = int(player_id)
        self._login_key = login_key
        if self.world is not None:
            self.world.register_bot(self.player_id)

    async def _enter_world(self, conn):
        world_rndk = await conn.handshake(self.server.auth_timeout)
        token = as2.world_login_password(self._login_key, world_rndk)
        fields = await conn.login_legacy(self.bot.username.lower(), token,
                                         self.server.auth_timeout)
        del fields
        await conn.join_world(self.player_id, self._login_key,
                              self.server.auth_timeout)
        if self.bot.spawn is not None:
            spawn_x, spawn_y = self.bot.spawn
        else:
            min_x, max_x, min_y, max_y = self.bot.area
            spawn_x = (min_x + max_x) // 2
            spawn_y = (min_y + max_y) // 2
        self._last_join = time.monotonic()
        room_frame = await conn.join_room(self.bot.room, self.server.auth_timeout,
                                          spawn_x, spawn_y)
        self.joined_room = True
        self._enter_room(self.bot.room, room_frame.args)
        if self.bot.card is not None:
            # Houdini jitters j#jr coordinates. Cast posts must be exact, including
            # unmeasured rooms whose only allowed point is their designer spawn.
            await conn.send_move(spawn_x, spawn_y)

    # ------------------------------------------------------------------- read
    async def _observe(self, stop_event):
        """Consume frames until stopped or disconnected.

        The read deadline is short so a stop request is honoured within a
        couple of seconds even with a silent room; an idle session is kept
        warm by u#h instead of by a long blocking read.
        """
        heartbeat = _Heartbeat(self)
        heartbeat.start()
        try:
            while not stop_event.is_set():
                async with self._reader_lock:
                    try:
                        raw = await self.conn.read_line(timeout=_READ_SLICE)
                    except TimeoutError:
                        continue  # silent room; heartbeat keeps the link warm
                raw = raw.strip()
                if not raw:
                    continue
                if raw.startswith('%'):
                    await self._handle_xt(raw)
                # XML frames here are server chatter; login steps already
                # consumed their XML blocks during establish.
        finally:
            await heartbeat.stop()

    async def _handle_xt(self, raw):
        frame = as2.Frame.parse(raw)
        error = frame.as_error()
        if error is not None:
            # Flood / room pacing must not tear down the whole bot session.
            if error.code in (7, 150, 210):
                logger.debug('%s server code %s (ignored)', self.log_context, error.code)
                return
            raise error
        op = frame.op
        if op == 'sm':
            await self._on_chat(frame.args)
        elif op == 'ap':
            self._absorb_roster(frame.args)
            if self.world is not None and self.room_id is not None:
                for record in frame.args:
                    self.world.member_added(self.room_id, record)
                    if self.director is not None:
                        from .world import parse_record
                        member = parse_record(record)
                        if member:
                            self.director.member_added(self.room_id, member[0])
        elif op == 'rp':
            if frame.args and str(frame.args[0]).isdigit():
                self.roster.pop(int(frame.args[0]), None)
                if self.world is not None and self.room_id is not None:
                    self.world.member_removed(self.room_id, int(frame.args[0]))
                    if self.director is not None:
                        self.director.presence_changed(self.room_id)
        elif op == 'grs':
            self._absorb_roster(frame.args[1:])
            if (self.world is not None and frame.args and str(frame.args[0]).isdigit()
                    and int(frame.args[0]) == self.room_id):
                self.world.snapshot(self.room_id, frame.args[1:])
                if self.director is not None:
                    self.director.presence_changed(self.room_id)
        elif op == 'sp':
            # Other penguins walking (and our own echo). Positions go to the world model.
            if (self.world is not None and self.room_id is not None and len(frame.args) >= 3
                    and str(frame.args[0]).isdigit()):
                self.world.member_moved(self.room_id, int(frame.args[0]), frame.args[1], frame.args[2])
        elif op == 'sb' and self.director is not None and len(frame.args) >= 3:
            try:
                sender, x, y = map(int, frame.args[:3])
            except ValueError:
                return
            await self.director.snowball(self, sender, x, y)
        elif op == 'h':
            await self.metrics.heartbeat(self.index)
        elif op == 'jr':
            # The server moved us (we did not call change_room): follow it.
            if frame.args and str(frame.args[0]).isdigit():
                self._enter_room(int(frame.args[0]), frame.args)
        # Remaining ops (lp, pgu, sf, se, ...) carry nothing bots need.

    def _absorb_roster(self, args):
        """Learn ``id|nickname|...`` member strings from ap/jr/grs frames."""
        for record in args:
            if not isinstance(record, str) or '|' not in record:
                continue
            fields = record.split('|')
            if len(fields) < 2 or not fields[0].isdigit():
                continue
            self.roster[int(fields[0])] = fields[1]

    def _enter_room(self, room_id, args, forget=True):
        """Record that this bot is now in ``room_id``; ``args`` is the roster frame's args."""
        self.room_id = int(room_id)
        if forget:
            self.roster = {}
        self._absorb_roster(args)
        if self.world is not None and self.player_id is not None:
            self.world.bot_entered(self.player_id, self.room_id, args)

    # ------------------------------------------------------------------- chat
    async def _on_chat(self, args):
        if len(args) < 2:
            return
        raw_id, message = args[0], args[1]
        if not str(raw_id).isdigit():
            return
        sender_id = int(raw_id)
        await self.metrics.chat_received(self.index)
        await self.behavior.on_chat(sender_id, message)

    # ------------------------------------------------------------------- send
    def _require_conn(self):
        if self.conn is None:
            raise as2.Disconnected('the session is offline')

    async def send_chat(self, player_id, message):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_chat(player_id, message)

    async def send_move(self, x, y):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_move(x, y)

    async def send_action(self, action):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_action(action)

    async def send_emote(self, emote):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_emote(emote)

    async def send_frame(self, frame):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_frame(frame)

    async def send_snowball(self, x, y):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_snowball(x, y)

    async def change_room(self, room, x, y, timeout=None):
        """Move this live session to ``room`` at ``(x, y)`` without reconnecting.

        Phase 3's travel controller owns occupancy reservations around this call.

        It reuses ``as2.join_room`` (``j#jr`` and the ``grs`` barrier that discards a
        stale ``jr``). That reads the socket, so the packet reader is parked first.

        Houdini answers a refused join with code 210 both for its 0.5 s join cooldown
        and for a full room, and the two cannot be told apart on the wire. The
        cooldown is waited out locally, so a 210 is retried once and, if it repeats,
        reported as ``full``. A refused join leaves the bot in the room it was in.
        A join that gets no answer drops the link, so ``run`` reconnects at home.
        """
        self._require_conn()
        room, x, y = int(room), int(x), int(y)
        timeout = float(self.server.auth_timeout if timeout is None else timeout)
        async with self._room_change_lock:
            for attempt in (1, 2):
                await self._wait_join_cooldown()
                self._require_conn()
                conn = self.conn
                try:
                    async with self._reader_lock:
                        async with self.mutations_lock:
                            self._last_join = time.monotonic()
                            frame = await asyncio.wait_for(conn.join_room(room, timeout, x, y), timeout)
                            await conn.send_move(x, y)
                except as2.ServerError as error:
                    if error.code == _ROOM_FULL_OR_COOLDOWN and attempt == 1:
                        continue
                    if error.code == _ROOM_FULL_OR_COOLDOWN:
                        await self.metrics.travel('full')
                        raise RoomChangeError('full', f'room {room} refused the join') from None
                    await self.metrics.travel('error')
                    raise RoomChangeError('error', f'server code {error.code}') from None
                except (TimeoutError, asyncio.TimeoutError):
                    await self.metrics.travel('timeout')
                    await conn.close()  # where the server put us is unknown: reconnect at home
                    raise RoomChangeError('timeout', f'no answer joining room {room}') from None
                except asyncio.CancelledError:
                    await conn.close()  # an interrupted join can leave the location unknown
                    raise
                self._enter_room(room, frame.args)
                await self.metrics.travel('ok')
                return frame

    async def _wait_join_cooldown(self):
        remaining = self._last_join + _JOIN_COOLDOWN - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def send_heartbeat(self):
        self._require_conn()
        async with self.mutations_lock:
            await self.conn.send_heartbeat()

    # -------------------------------------------------------------- lifecycle
    async def _shutdown_tasks(self):
        await self.behavior.stop()
        if self.conn is not None and not self.conn.closed:
            await self.conn.close()
        self.conn = None
        self.joined_room = False
        self.room_id = None
        self.roster = {}
        if self.world is not None and self.player_id is not None:
            self.world.bot_left(self.player_id)
            if self.director is not None:
                for room in list(self.director.rooms):
                    self.director.presence_changed(room)

    async def _set_connected(self, connected):
        await self.metrics.connected(self.index, connected)
        if connected:
            await self.metrics.session_established(self.index)
        self.stable_since = time.monotonic() if connected else None

    async def close(self):
        """Orderly shutdown; behavior tasks stop first, then the wire."""
        await self._shutdown_tasks()
        await self._set_connected(False)


class _Heartbeat:
    """Periodic u#h inside the configured cadence (server cooldown is 59 s)."""

    def __init__(self, session):
        self._session = session
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        try:
            if self._session.catalog is not None:
                # Reset the server's connection-time heartbeat after auth/join,
                # which may itself take time under load. This first packet has
                # no cooldown predecessor on a fresh connection.
                await self._session.send_heartbeat()
            while True:
                if self._session.catalog is not None:
                    # Houdini sweeps at 61 s and enforces a 59 s packet cooldown.
                    # With 26 staggered logins, 60-65 s can miss the first sweep
                    # for a connection made just before a sweep boundary. Keep
                    # this tighter cadence opt-in with the cast; legacy is intact.
                    delay = random.uniform(59.5, 60.0)
                else:
                    delay = 60.0 + random.uniform(0.0, 5.0)
                await asyncio.sleep(delay)
                try:
                    await self._session.send_heartbeat()
                except as2.Disconnected:
                    return
        except asyncio.CancelledError:
            raise

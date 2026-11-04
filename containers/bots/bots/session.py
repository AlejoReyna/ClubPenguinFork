"""Session lifecycle: authenticate, join the room, run behavior, reconnect.

One :class:`BotSession` per configured bot. It keeps the identity: sessions
always log in with the same account (never re-registering), reconnect with
full-jitter exponential backoff after drops, and treat credential errors as
an operator problem (slow retry) instead of hammering the login server.
Only the room roster, its own id and the configured fellow bots are needed to
avoid echoing itself or other bots.
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
                 metrics, fallback_replies):
        self.index = index
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

        self.conn = None  # as2.AS2Connection once inside the world
        self.player_id = None
        self.roster = {}
        self.joined_room = False
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

    async def _enter_world(self, conn):
        world_rndk = await conn.handshake(self.server.auth_timeout)
        token = as2.world_login_password(self._login_key, world_rndk)
        fields = await conn.login_legacy(self.bot.username.lower(), token,
                                         self.server.auth_timeout)
        del fields
        await conn.join_world(self.player_id, self._login_key,
                              self.server.auth_timeout)
        min_x, max_x, min_y, max_y = self.bot.area
        room_frame = await conn.join_room(self.bot.room, self.server.auth_timeout,
                                          (min_x + max_x) // 2, (min_y + max_y) // 2)
        self.joined_room = True
        await self._forget_roster()
        self._absorb_roster(room_frame.args)

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
                try:
                    raw = await self.conn.read_line(timeout=2.0)
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
            raise error
        op = frame.op
        if op == 'sm':
            await self._on_chat(frame.args)
        elif op == 'ap':
            self._absorb_roster(frame.args)
        elif op == 'rp':
            if frame.args and str(frame.args[0]).isdigit():
                self.roster.pop(int(frame.args[0]), None)
        elif op == 'grs':
            self._absorb_roster(frame.args[1:])
        elif op == 'sp':
            pass  # other penguins walking; bots only emit their own positions
        elif op == 'h':
            await self.metrics.heartbeat(self.index)
        elif op == 'jr':
            self._absorb_roster(frame.args[1:])
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

    async def _forget_roster(self):
        self.roster = {}

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
        self.roster = {}

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
            while True:
                await asyncio.sleep(60.0 + random.uniform(0.0, 5.0))
                try:
                    await self._session.send_heartbeat()
                except as2.Disconnected:
                    return
        except asyncio.CancelledError:
            raise

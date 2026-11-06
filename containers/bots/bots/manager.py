"""BotManager: run every configured bot concurrently, resiliently."""
import asyncio
import logging
import math
import os
import random

from .config import Config
from .provider import MoonshotClient, SimulatedProvider
from .session import AuthFailure, BotSession
from . import metrics as metrics_mod
from . import music as music_mod
from . import spend as spend_mod
from .world import World

logger = logging.getLogger('bots.manager')


class BotManager:
    """Owns sessions and shared components (metrics, provider, spend ledger, world model)."""

    def __init__(self, config: Config, registry, rng=None):
        self.config = config
        self.registry = registry
        self.rng = rng if rng is not None else random.Random()
        self.world = World()
        self.ledger = None
        if config.provider.mode == 'simulated':
            provider = SimulatedProvider(
                replies=config.provider.fallback_replies,
                model=config.provider.model, record_messages=False)
        else:
            # Fail-closed USD ledger (spend.py): a request is only sent once it is saved.
            self.ledger = spend_mod.SpendLedger(
                config.provider.budget_path,
                daily_usd=config.provider.daily_usd,
                monthly_usd=config.provider.monthly_usd,
                per_minute=config.provider.max_requests_per_minute,
                max_requests_per_day=config.provider.max_requests_per_day,
                prices=config.provider.prices)
            provider = MoonshotClient(
                base_url=config.provider.base_url,
                api_key=config.provider.api_key,
                model=config.provider.model,
                max_completion_tokens=config.provider.max_completion_tokens,
                timeout=config.provider.timeout,
                retries=config.provider.retries,
                retry_wait=config.provider.retry_wait,
                thinking=config.provider.thinking,
                max_concurrent=min(config.provider.max_concurrent, config.policy.max_concurrent_requests),
                metrics=self.metrics_facade(),
                ledger=self.ledger)
        self.provider = provider
        try:
            self.music = music_mod.from_env(os.environ)
        except (OSError, ValueError) as error:
            logger.error('music disabled: %s', type(error).__name__)
            self.music = None
        if self.music is not None:
            logger.info('music orders enabled for bots with music=true')
        self.sessions = []
        nicknames = [bot.nickname for bot in config.bots]
        for position, bot in enumerate(config.bots):
            session = BotSession(
                index=bot.index, bot=bot, server=config.server,
                provider=provider, policy=self.config.policy,
                behavior=self.config.behavior,
                metrics=self.metrics_facade(),
                fallback_replies=self.config.provider.fallback_replies,
                music=self.music, world=self.world, service=config.service,
                catalog=config.catalog, output_filter=config.output_filter)
            # Peer knowledge prevents bot-to-bot echoes and unbounded threads.
            session.behavior.peer_bot_names = frozenset(
                name for other, name in enumerate(nicknames) if other != position)
            self.sessions.append(session)
        for session in self.sessions:
            session.peer_bot_ids = lambda own=session: {
                other.player_id for other in self.sessions if other is not own and other.player_id is not None}
        self.recall = None
        if config.recall.enabled:
            from .recall import RecallStore
            self.recall = RecallStore(
                config.recall.path,
                ttl_seconds=config.recall.ttl_seconds,
                max_note_chars=int(config.recall.max_note_chars))
            for session in self.sessions:
                session.recall = self.recall
        self.director = None
        if config.dialogue.enabled:
            from .director import Director
            self.director = Director(config, self.sessions, self.world, ledger=self.ledger,
                                     recall=self.recall)
            for session in self.sessions:
                session.director = self.director
        self._register_collectors()

    def metrics_facade(self):
        return metrics_mod.BotMetrics(self.registry)

    def _register_collectors(self):
        """Gauges read from live state at scrape time, so they can never go stale."""
        collect = getattr(self.registry, 'collect', None)
        if collect is None:
            return
        homes = {}
        for bot in self.config.bots:
            homes[bot.room] = homes.get(bot.room, 0) + 1
        catalog_rooms = self.config.catalog.rooms if self.config.catalog is not None else {}
        known = set(homes) | set(catalog_rooms)  # bounded label set; anything else is "other"

        def in_room():
            counts = {str(room): 0 for room in known}
            for room, count in self.world.bot_counts().items():
                name = str(room) if room in known else 'other'
                counts[name] = counts.get(name, 0) + count
            return [({'room': name}, value) for name, value in sorted(counts.items())]

        collect(metrics_mod.BOTS_IN_ROOM, in_room)
        collect(metrics_mod.BOTS_PLANNED,
                lambda: [({'room': str(room)}, count) for room, count in sorted(homes.items())])
        collect(metrics_mod.SPEND_RATIO,
                lambda: [({}, self.ledger.snapshot()['ratio'] if self.ledger is not None else 0.0)])
        collect(metrics_mod.SPEND_LEDGER_OK,
                lambda: [({}, 1.0 if self.ledger is None or self.ledger.state_ok else 0.0)])

    # -------------------------------------------------------------- readiness
    def sessions_in_room(self):
        return sum(1 for s in self.sessions if s.conn is not None and not s.conn.closed and s.joined_room)

    def required_sessions(self):
        """ceil(ratio x N): with 26 sessions travelling or reconnecting, "all" would flap."""
        count = len(self.sessions)
        if not count:
            return 0
        # round() first: 0.7 * 10 is 7.000000000000001 and must not ask for 8.
        return max(1, math.ceil(round(self.config.service.ready_ratio * count, 9)))

    def ready(self):
        return self.sessions_in_room() >= self.required_sessions()

    # ------------------------------------------------------------------- run
    async def run(self, stop_event):
        """Wait until every session task ends (stop or fatal AuthFailure)."""
        if not self.sessions:
            logger.warning('no bots configured; service stays idle')
            return
        if self.director:
            self.director.start()
        tasks = []
        try:
            for position, session in enumerate(self.sessions):
                # Staggered logins: 26 bcrypt checks and joins at once would hit the Pi.
                if position and await self._stagger(stop_event):
                    break
                tasks.append(asyncio.create_task(self._run_session(session, stop_event),
                                                 name=f'session-{session.bot.username}'))
            # Keep waiting until all done; stop_event coordination lives in tasks.
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close()

    async def _stagger(self, stop_event):
        """Wait 2-4 s (configurable) with jitter; True when a stop was requested meanwhile."""
        service = self.config.service
        delay = self.rng.uniform(service.startup_stagger_min, service.startup_stagger_max)
        if delay <= 0:
            return stop_event.is_set()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    def _log_auth(self, session, auth_error):
        logger.error('%s', auth_error)

    async def _run_session(self, session, stop_event):
        while not stop_event.is_set():
            try:
                await session.run(stop_event)
                return  # run() only returns when stop was requested
            except AuthFailure as failure:
                self._log_auth(session, failure)
                try:
                    await asyncio.wait_for(stop_event.wait(),
                                           timeout=failure.retry_seconds)
                    return
                except asyncio.TimeoutError:
                    continue
            except Exception as error:
                await session.close()
                # Defensive: keep the service alive even on unexpected bugs.
                logger.error('bot %s encountered %s', session.index, type(error).__name__)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30)
                    return
                except asyncio.TimeoutError:
                    continue

    async def close(self):
        """Close every session; cancels any still-running task."""
        if self.director:
            await self.director.close()
        await asyncio.gather(*(session.close() for session in self.sessions),
                             return_exceptions=True)

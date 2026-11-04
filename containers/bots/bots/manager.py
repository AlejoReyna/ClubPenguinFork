"""BotManager: run every configured bot concurrently, resiliently."""
import asyncio
import logging

from .config import Config
from .provider import MoonshotClient, SimulatedProvider
from .session import AuthFailure, BotSession
from .budget import RequestBudget

logger = logging.getLogger('bots.manager')


class BotManager:
    """Owns sessions and shared components (metrics, provider)."""

    def __init__(self, config: Config, registry):
        self.config = config
        self.registry = registry
        if config.provider.mode == 'simulated':
            provider = SimulatedProvider(
                replies=config.provider.fallback_replies,
                model=config.provider.model, record_messages=False)
        else:
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
                budget=RequestBudget(config.policy.requests_per_minute,
                                     config.provider.max_requests_per_day,
                                     config.provider.budget_path))
        self.provider = provider
        self.sessions = []
        nicknames = [bot.nickname for bot in config.bots]
        for index, bot in enumerate(config.bots):
            session = BotSession(
                index=index, bot=bot, server=config.server,
                provider=provider, policy=self.config.policy,
                behavior=self.config.behavior,
                metrics=self.metrics_facade(),
                fallback_replies=self.config.provider.fallback_replies)
            # Peer knowledge prevents bot-to-bot echoes and unbounded threads.
            session.behavior.peer_bot_names = frozenset(
                name for other_index, name in enumerate(nicknames)
                if other_index != index)
            self.sessions.append(session)
        for session in self.sessions:
            session.peer_bot_ids = lambda own=session: {
                other.player_id for other in self.sessions if other is not own and other.player_id is not None}

    def metrics_facade(self):
        from .metrics import BotMetrics  # late import avoids a cycle at import
        return BotMetrics(self.registry)

    async def run(self, stop_event):
        """Wait until every session task ends (stop or fatal AuthFailure)."""
        tasks = [asyncio.create_task(self._run_session(session, stop_event),
                                     name=f'session-{session.bot.username}')
                 for session in self.sessions]
        if not tasks:
            logger.warning('no bots configured; service stays idle')
            return
        # Keep waiting until all done; stop_event coordination lives in tasks.
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close()

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
        await asyncio.gather(*(session.close() for session in self.sessions),
                             return_exceptions=True)

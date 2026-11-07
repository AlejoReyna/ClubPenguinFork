"""Process entrypoint: metrics server, manager and signal handling."""
import asyncio
import logging
import os
import signal
import sys

from .config import ConfigError, load_config
from .manager import BotManager
from .metrics import BotMetrics, MetricsServer, Registry


def _configure_logging():
    level = os.environ.get('BOTS_LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=level if level in {'DEBUG', 'INFO', 'WARNING', 'ERROR'} else 'INFO',
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('bots.config').setLevel(logging.ERROR)


async def _amain():
    _configure_logging()
    logger = logging.getLogger('bots')
    try:
        config = load_config()
    except ConfigError as error:
        print(f'configuration error: {error}', file=sys.stderr)
        raise SystemExit(2) from None

    registry = Registry()
    manager = BotManager(config, registry)
    # Ready when at least ceil(ratio x N) sessions are in a room (default 0.8). /healthz is unchanged.
    metrics_server = MetricsServer(registry, config.metrics_port, ready=manager.ready)
    await metrics_server.start()
    facade = BotMetrics(registry)
    await facade.mark_up()
    logger.info('metrics on port %s (%d bots configured)',
                config.metrics_port, len(config.bots))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    graceful = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: graceful.set())
        except NotImplementedError:  # pragma: no cover (non-main loop/Windows)
            pass

    try:
        runner = asyncio.create_task(manager.run(stop_event))
        done = asyncio.create_task(graceful.wait())
        finished, _ = await asyncio.wait((runner, done), return_when=asyncio.FIRST_COMPLETED)
        if runner in finished:
            await runner  # surface a fatal manager error to the process supervisor
        stop_event.set()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    finally:
        done.cancel()
        await asyncio.gather(done, return_exceptions=True)
        await manager.close()
        await metrics_server.stop()
        logger.info('bots service stopped')


def main():
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

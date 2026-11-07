"""Readiness threshold and staggered start of the BotManager."""
import asyncio
import unittest

from bots.config import build_config
from bots.manager import BotManager
from bots.metrics import Registry
from support import config_document_world


def make_manager(count, **service):
    document = config_document_world(6112)
    document['bots'] = [{'username': f'bota{i:02d}', 'password': 'secreto1', 'nickname': f'Bot{i}',
                         'room': 100} for i in range(count)]
    if service:
        document['service'] = service
    return BotManager(build_config(document, {}), Registry())


class _OpenConn:
    closed = False


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    """Async only because Python 3.9 builds asyncio.Queue (in BotBehavior) needing a loop."""


    async def test_required_sessions_is_ceil_of_ratio_times_n(self):
        # The live three-bot deployment keeps meaning "all three".
        for count, ratio, expected in ((3, 0.8, 3), (26, 0.8, 21), (5, 0.8, 4), (1, 0.8, 1),
                                       (10, 0.7, 7),    # 0.7 * 10 == 7.000000000000001 in floats
                                       (4, 1.0, 4), (26, 0.5, 13)):
            manager = make_manager(count, ready_ratio=ratio)
            self.assertEqual(manager.required_sessions(), expected, (count, ratio))

    async def test_ready_flips_at_the_threshold(self):
        manager = make_manager(10, ready_ratio=0.8)
        self.assertFalse(manager.ready())
        for session in manager.sessions[:7]:
            session.conn, session.joined_room = _OpenConn(), True
        self.assertEqual(manager.sessions_in_room(), 7)
        self.assertFalse(manager.ready())
        manager.sessions[7].conn, manager.sessions[7].joined_room = _OpenConn(), True
        self.assertTrue(manager.ready())
        manager.sessions[0].conn.closed = True   # a dropped link stops counting
        manager.sessions[1].joined_room = False
        self.assertFalse(manager.ready())

    async def test_ratio_and_stagger_are_validated(self):
        from bots.config import ConfigError
        for bad in ({'ready_ratio': 1.5}, {'ready_ratio': 0}, {'startup_stagger_min': 5, 'startup_stagger_max': 1},
                    {'tz': 'Not/AZone'}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                make_manager(2, **bad)

    async def test_defaults_match_the_design(self):
        manager = make_manager(3)
        service = manager.config.service
        self.assertEqual((service.ready_ratio, service.startup_stagger_min, service.startup_stagger_max),
                         (0.8, 2.0, 4.0))
        self.assertEqual(service.tz_name, 'UTC')


class StaggerTests(unittest.IsolatedAsyncioTestCase):

    async def run_manager(self, manager, until_started):
        starts = []

        async def fake_session(session, stop_event):
            starts.append(asyncio.get_running_loop().time())
            await stop_event.wait()

        manager._run_session = fake_session
        stop = asyncio.Event()
        task = asyncio.create_task(manager.run(stop))
        deadline = asyncio.get_running_loop().time() + 5
        while len(starts) < until_started and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        return starts, stop, task

    async def test_sessions_log_in_one_after_another_with_jitter(self):
        manager = make_manager(4, startup_stagger_min=0.05, startup_stagger_max=0.12)
        starts, stop, task = await self.run_manager(manager, 4)
        stop.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(len(starts), 4)
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.045)
            self.assertLess(gap, 0.30)
        self.assertGreater(len(set(round(gap, 3) for gap in gaps)), 1)   # jittered, not lockstep

    async def test_zero_stagger_starts_everything_at_once(self):
        manager = make_manager(3, startup_stagger_min=0, startup_stagger_max=0)
        starts, stop, task = await self.run_manager(manager, 3)
        stop.set()
        await asyncio.wait_for(task, 5)
        self.assertLess(starts[-1] - starts[0], 0.2)

    async def test_a_stop_during_the_stagger_starts_no_more_sessions(self):
        manager = make_manager(4, startup_stagger_min=5, startup_stagger_max=6)
        starts, stop, task = await self.run_manager(manager, 1)
        stop.set()
        await asyncio.wait_for(task, 3)       # does not sit out the 5 s wait
        self.assertEqual(len(starts), 1)


if __name__ == '__main__':
    unittest.main()

"""Offline tests for the host relay: auth, limits, sanitising, no content logged."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from experiments.puffle_sim.chatgpt_process import ProcessChatGPTProvider
from experiments.puffle_sim.engine import QuotaExhausted
from experiments.puffle_sim.host_relay import MAX_REQUEST, Relay, RelayClient

SECRET = 's' * 43
CHAT = 'secreto-del-chat-12345'


class FakeProvider:
    def __init__(self, result=(True, 'hola'), error=None, delay=0):
        self.result, self.error, self.delay, self.calls = result, error, delay, []

    async def evaluate(self, context):
        self.calls.append(context)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def request(context=(('player', CHAT),), secret=SECRET):
    return json.dumps({'secret': secret, 'context': [list(x) for x in context]}).encode() + b'\n'


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        self.logs = []
        self.provider = FakeProvider(**kw)
        return Relay(self.provider, SECRET, timeout=0.5, log=self.logs.append)

    def assert_no_content_logged(self):
        joined = ' '.join(self.logs)
        self.assertNotIn(CHAT, joined)
        self.assertNotIn(SECRET, joined)

    async def test_rejects_bad_secret_without_calling_provider(self):
        relay = self.make()
        self.assertIsNone(await relay.answer(request(secret='x' * 43)))
        self.assertIsNone(await relay.answer(json.dumps({'context': []}).encode()))
        self.assertEqual(self.provider.calls, [])
        self.assert_no_content_logged()

    async def test_limits_and_shape(self):
        relay = self.make()
        big = (('player', 'a' * MAX_REQUEST),)
        self.assertIsNone(await relay.answer(request(big)))
        self.assertIsNone(await relay.answer(request((('system', 'x'),))))
        self.assertIsNone(await relay.answer(request((('player', 'x'),) * 33)))
        self.assertIsNone(await relay.answer(b'{"secret": "' + SECRET.encode() + b'", "context": [], "x": 1}'))
        self.assertEqual(self.provider.calls, [])

    async def test_sanitises_and_cuts_to_48(self):
        relay = self.make(result=(True, '<b>Hola|%</b>\x07 ' + 'ñ' * 80))
        out = await relay.answer(request())
        self.assertEqual(set(out), {'reply', 'text'})
        self.assertTrue(out['reply'])
        self.assertLessEqual(len(out['text']), 48)
        for ch in '<>|%\x07':
            self.assertNotIn(ch, out['text'])

    async def test_silence_errors_and_quota(self):
        for kw in ({'result': (True, '(silencio)')}, {'result': (False, 'x')},
                   {'error': RuntimeError(CHAT)}, {'delay': 2}):
            relay = self.make(**kw)
            self.assertEqual(await relay.answer(request()), {'reply': False, 'text': ''})
            self.assert_no_content_logged()
        relay = self.make(error=QuotaExhausted())
        self.assertTrue((await relay.answer(request()))['reply'])

    async def test_concurrency_one(self):
        relay = self.make(delay=0.2)
        a, b = await asyncio.gather(relay.answer(request()), relay.answer(request()))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIn({'reply': False, 'text': ''}, (a, b))

    async def test_round_trip_over_tcp(self):
        logs = []
        provider = FakeProvider(result=(True, 'Hola, amigo'))
        relay = Relay(provider, SECRET, log=logs.append)
        server = await asyncio.start_server(relay.handle, '127.0.0.1', 0, limit=MAX_REQUEST + 1)
        port = server.sockets[0].getsockname()[1]
        async with server:
            self.assertEqual(await RelayClient('127.0.0.1', port, SECRET).evaluate((('player', CHAT),)),
                             (True, 'Hola, amigo'))
            with self.assertRaises(ValueError):
                await RelayClient('127.0.0.1', port, 'z' * 43).evaluate((('player', CHAT),))
        self.assertNotIn(CHAT, ' '.join(logs))

    async def test_client_disconnect_cancels_inflight_provider(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        class Blocking:
            async def evaluate(self, context):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
        relay = Relay(Blocking(), SECRET, timeout=10, log=lambda _: None)
        server = await asyncio.start_server(relay.handle, '127.0.0.1', 0, limit=MAX_REQUEST + 1)
        async with server:
            port = server.sockets[0].getsockname()[1]
            client = asyncio.create_task(RelayClient('127.0.0.1', port, SECRET).evaluate((('player', CHAT),)))
            await asyncio.wait_for(entered.wait(), 2)
            client.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await client
            await asyncio.wait_for(stopped.wait(), 2)
            self.assertFalse(relay.busy.locked())

    async def test_cancellation_terminates_inference_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pid'
            code = ("import os,sys,time;sys.stdin.buffer.readline();"
                    "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)")
            provider = ProcessChatGPTProvider((sys.executable, '-c', code, str(path)))
            task = asyncio.create_task(provider.evaluate((('player', CHAT),)))
            try:
                for _ in range(200):
                    if path.exists() and path.read_text():
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail('worker did not start')
                pid = int(path.read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_disconnect_stops_relay_inference_process_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pid'
            code = ("import os,sys,time;sys.stdin.buffer.readline();"
                    "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)")
            provider = ProcessChatGPTProvider((sys.executable, '-c', code, str(path)))
            relay = Relay(provider, SECRET, timeout=10, log=lambda _: None)
            server = await asyncio.start_server(relay.handle, '127.0.0.1', 0, limit=MAX_REQUEST + 1)
            async with server:
                port = server.sockets[0].getsockname()[1]
                task = asyncio.create_task(RelayClient('127.0.0.1', port, SECRET).evaluate((('player', CHAT),)))
                try:
                    for _ in range(200):
                        if path.exists() and path.read_text():
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail('worker did not start')
                    pid = int(path.read_text())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    for _ in range(200):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail('relay worker survived client disconnect')
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()

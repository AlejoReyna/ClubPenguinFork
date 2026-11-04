import asyncio
import contextlib
import socket
import unittest

from production.puffle.broker import Broker, serve
from production.puffle.http import request


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = Broker('s' * 48)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.task = asyncio.create_task(serve(self.b, '127.0.0.1', self.port))
        self.url = f'http://127.0.0.1:{self.port}'
        for _ in range(50):
            try:
                await request(self.url, '/healthz')
                break
            except OSError:
                await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.b.db.close()

    async def test_http_auth_and_bounded_payload(self):
        self.assertTrue((await request(self.url, '/healthz'))['ok'])
        with self.assertRaises(ConnectionError):
            await request(self.url, '/internal/status', 'wrong', {'player': 1})
        with self.assertRaises(ValueError):
            await request(self.url, '/puffle/api/pair', data={'large': 'x' * 17000})

    async def test_public_route_cannot_spoof_player(self):
        pending = await request(self.url, '/puffle/api/pair')
        with self.assertRaises(ConnectionError):
            await request(self.url, '/puffle/api/confirm', pending['token'], {'player': 1})

    async def test_plaintext_external_services_are_rejected(self):
        with self.assertRaises(ValueError):
            await request('http://outside.invalid', '/healthz')

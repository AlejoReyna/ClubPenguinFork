"""Real loopback sockets verify bounded transport and cancellation, without an API."""
import asyncio
import unittest

from bots.http_client import MAX_BODY, post_json


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.closed = asyncio.Event()
        self.mode = 'chunked'

        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                length = int(head.split(b'Content-Length: ')[1].split(b'\r\n')[0])
                await reader.readexactly(length)
                if self.mode == 'wait':
                    await reader.read()
                    self.closed.set()
                    return
                if self.mode == 'large':
                    response = f'HTTP/1.1 200 OK\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n'.encode()
                else:
                    response = (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                                b'4\r\n{"a"\r\n3\r\n:1}\r\n0\r\n\r\n')
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/chat/completions'

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_chunked_body(self):
        status, body = await post_json(self.url, 'Bearer synthetic', b'{}')
        self.assertEqual((status, body), (200, b'{"a":1}'))

    async def test_oversized_response_is_rejected_before_reading(self):
        self.mode = 'large'
        with self.assertRaises(ValueError):
            await post_json(self.url, 'Bearer synthetic', b'{}')

    async def test_timeout_closes_actual_socket(self):
        self.mode = 'wait'
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(post_json(self.url, 'Bearer synthetic', b'{}'), 0.1)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_plaintext_non_loopback_and_header_injection_are_rejected(self):
        for url, auth in [('http://example.com/v1', 'Bearer synthetic'),
                          (self.url, 'Bearer synthetic\r\nX-Injected: yes')]:
            with self.assertRaises(ValueError):
                await post_json(url, auth, b'{}')

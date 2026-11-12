"""Origin gate logging for the WebSocket proxy (no live sockets)."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault('websockets', MagicMock())

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('socket_proxy', ROOT / 'containers/runtime/socket_proxy.py')
socket_proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(socket_proxy)


class SocketProxyLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_unknown_origin_emits_warning(self):
        websocket = MagicMock()
        websocket.request_headers = {'Origin': 'https://evil.test', 'Host': 'club.lab.dev', 'User-Agent': 'Brave'}
        websocket.remote_address = ('198.51.100.2', 4242)
        websocket.close = AsyncMock()

        with self.assertLogs('socket_proxy', level='WARNING') as captured:
            await socket_proxy.handle(websocket, '/socket/login')

        self.assertIn('ws_reject', captured.output[0])
        self.assertIn('evil.test', captured.output[0])
        websocket.close.assert_awaited_once_with(code=1008, reason='Unsupported endpoint or origin')

    async def test_accept_allowed_origin_emits_info(self):
        websocket = MagicMock()
        websocket.request_headers = {'Origin': 'https://127.0.0.1:8080', 'Host': '127.0.0.1:8080'}
        websocket.remote_address = ('127.0.0.1', 5555)
        websocket.close = AsyncMock()
        # Stop before opening the upstream AS2 port.
        websocket.__aiter__ = lambda self: self
        websocket.__anext__ = AsyncMock(side_effect=StopAsyncIteration)

        with unittest.mock.patch.object(socket_proxy.asyncio, 'open_connection',
                                        AsyncMock(side_effect=OSError('refused'))):
            with self.assertLogs('socket_proxy', level='ERROR') as captured:
                await socket_proxy.handle(websocket, '/socket/login')

        self.assertTrue(any('ws_backend_fail' in line for line in captured.output))


if __name__ == '__main__':
    unittest.main()

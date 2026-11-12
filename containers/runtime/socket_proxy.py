"""Bridge Ruffle WebSockets to the two allowed game endpoints only."""
import asyncio
import logging
import os
from urllib.parse import urlparse

import websockets

log = logging.getLogger('socket_proxy')

TARGETS = {'/socket/login': ('login', 6112), '/socket/world': ('glaciar', 9876)}
# Defaults for production; a lab can extend them with ALLOWED_ORIGINS (comma-separated),
# e.g. to test from a phone on the LAN without touching this file.
_extra = {o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '').split(',') if o.strip()}
ALLOWED_ORIGINS = {'192.168.0.10', '127.0.0.1', 'localhost', 'club.example.com'} | _extra


def _peer(websocket):
    peer = websocket.remote_address
    return f'{peer[0]}:{peer[1]}' if peer else 'unknown'


async def handle(websocket, path):
    headers = websocket.request_headers
    origin_raw = headers.get('Origin', '')
    host_raw = headers.get('Host', '')
    origin = urlparse(origin_raw).hostname
    host = urlparse('//' + host_raw).hostname if host_raw else None
    ua = (headers.get('User-Agent') or '')[:160]
    if path not in TARGETS:
        log.warning(
            'ws_reject peer=%s path=%r unknown_path origin_raw=%r origin_host=%r host=%r ua=%r',
            _peer(websocket), path, origin_raw, origin, host, ua)
        await websocket.close(code=1008, reason='Unsupported endpoint or origin')
        return
    if origin not in ALLOWED_ORIGINS:
        log.warning(
            'ws_reject peer=%s path=%s origin_raw=%r origin_host=%r host=%r '
            'allowed_hosts=%s ua=%r',
            _peer(websocket), path, origin_raw, origin, host,
            ','.join(sorted(ALLOWED_ORIGINS)), ua)
        await websocket.close(code=1008, reason='Unsupported endpoint or origin')
        return
    log.info('ws_accept peer=%s path=%s origin_host=%r host=%r ua=%r',
             _peer(websocket), path, origin, host, ua)
    try:
        reader, writer = await asyncio.open_connection(*TARGETS[path])
    except OSError as exc:
        log.error('ws_backend_fail peer=%s path=%s target=%s:%s err=%s',
                  _peer(websocket), path, TARGETS[path][0], TARGETS[path][1], exc)
        await websocket.close(code=1011, reason='Upstream unavailable')
        return

    async def browser_to_server():
        async for message in websocket:
            writer.write(message.encode() if isinstance(message, str) else message)
            await writer.drain()

    async def server_to_browser():
        while data := await reader.read(65536):
            await websocket.send(data)

    tasks = [asyncio.create_task(browser_to_server()), asyncio.create_task(server_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        await websocket.close()
        log.info('ws_close peer=%s path=%s', _peer(websocket), path)


async def main():
    level = getattr(logging, os.environ.get('SOCKET_PROXY_LOG', 'INFO').upper(), logging.INFO)
    logging.basicConfig(level=level, format='socket_proxy %(levelname)s %(message)s')
    log.info('listening allowed_origins=%s targets=%s', ','.join(sorted(ALLOWED_ORIGINS)), list(TARGETS))
    async with websockets.serve(handle, '0.0.0.0', 8765, max_size=65536):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())

"""Bridge Ruffle WebSockets to the two allowed game endpoints only."""
import asyncio
import os
from urllib.parse import urlparse

import websockets

TARGETS = {'/socket/login': ('login', 6112), '/socket/world': ('glaciar', 9876)}
# Defaults for production; a lab can extend them with ALLOWED_ORIGINS (comma-separated),
# e.g. to test from a phone on the LAN without touching this file.
_extra = {o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '').split(',') if o.strip()}
ALLOWED_ORIGINS = {'192.168.0.10', '127.0.0.1', 'localhost', 'club.example.com'} | _extra


async def handle(websocket, path):
    origin = urlparse(websocket.request_headers.get('Origin', '')).hostname
    if path not in TARGETS or origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008, reason='Unsupported endpoint or origin')
        return
    reader, writer = await asyncio.open_connection(*TARGETS[path])

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


async def main():
    async with websockets.serve(handle, '0.0.0.0', 8765, max_size=65536):
        await asyncio.Future()


asyncio.run(main())

"""Small bounded JSON transport with cancellable sockets and no redirects/logs."""
import asyncio
import json
import ssl
from urllib.parse import urlsplit

MAX_BODY = 16384


async def request(base, path, token='', data=None, timeout=8):
    url = urlsplit(base)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username
            or url.password or url.query or url.fragment or url.path not in ('', '/')):
        raise ValueError('invalid service URL')
    if url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', 'puffle'):
        raise ValueError('HTTPS required')
    body = json.dumps(data or {}, ensure_ascii=True).encode()
    if len(body) > MAX_BODY or '\r' in token or '\n' in token:
        raise ValueError('invalid request')
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection(
            url.hostname, url.port or (443 if url.scheme == 'https' else 80),
            ssl=ssl.create_default_context() if url.scheme == 'https' else None,
            limit=MAX_BODY + 8192)
        try:
            host = url.netloc
            head = (f'POST {path} HTTP/1.1\r\nHost: {host}\r\n'
                    f'Authorization: Bearer {token}\r\nContent-Type: application/json\r\n'
                    f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n')
            writer.write(head.encode('ascii') + body)
            await writer.drain()
            headers = await reader.readuntil(b'\r\n\r\n')
            if len(headers) > 8192:
                raise ValueError('response headers too large')
            lines = headers.decode('ascii').split('\r\n')
            status = int(lines[0].split()[1])
            fields = dict(line.lower().split(':', 1) for line in lines[1:] if ':' in line)
            size = int(fields.get('content-length', '-1'))
            if not 0 <= size <= MAX_BODY:
                raise ValueError('response too large')
            result = json.loads(await reader.readexactly(size))
            if status != 200:
                raise ConnectionError(f'service status {status}')
            return result
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
            raise ConnectionError('incomplete service response') from error
        finally:
            writer.close()
            await writer.wait_closed()

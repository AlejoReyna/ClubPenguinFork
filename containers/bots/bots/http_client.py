"""Bounded async HTTP transport: cancellation closes the actual socket."""
import asyncio
import ssl
from urllib.parse import urlsplit

MAX_BODY = 256 * 1024


async def post_json(url, authorization, body, plain_hosts=()):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('invalid provider endpoint')
    # Plain HTTP only for loopback tests or an explicitly configured internal service
    # (the puffle host on the cluster/Docker network); providers always need HTTPS.
    if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1', *plain_hosts):
        raise ValueError('provider requires HTTPS; HTTP is only allowed for loopback tests')
    if any(c in authorization for c in '\r\n'):
        raise ValueError('invalid authorization value')
    encrypted = parts.scheme == 'https'
    port = parts.port or (443 if encrypted else 80)
    kwargs = {'ssl': ssl.create_default_context(), 'server_hostname': parts.hostname} if encrypted else {}
    reader, writer = await asyncio.open_connection(parts.hostname, port, limit=65536, **kwargs)
    try:
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        host = parts.hostname if parts.port is None else f'{parts.hostname}:{port}'
        headers = (f'POST {path} HTTP/1.1\r\nHost: {host}\r\n'
                   'Content-Type: application/json\r\nAccept: application/json\r\n'
                   f'Authorization: {authorization}\r\nContent-Length: {len(body)}\r\n'
                   'Connection: close\r\nUser-Agent: clubpenguin-lab-bots/1\r\n\r\n')
        writer.write(headers.encode('ascii') + body)
        await writer.drain()
        head = await reader.readuntil(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        status = int(lines[0].split()[1])
        fields = {}
        for line in lines[1:]:
            if ':' in line:
                key, value = line.split(':', 1)
                fields[key.lower().strip()] = value.strip()
        data = bytearray()
        if fields.get('transfer-encoding', '').lower() == 'chunked':
            while True:
                size = int((await reader.readline()).split(b';', 1)[0].strip(), 16)
                if size < 0 or len(data) + size > MAX_BODY:
                    raise ValueError('provider response exceeds limit')
                if not size:
                    break
                data.extend(await reader.readexactly(size))
                if await reader.readexactly(2) != b'\r\n':
                    raise ValueError('invalid chunk framing')
        elif 'content-length' in fields:
            length = int(fields['content-length'])
            if not 0 <= length <= MAX_BODY:
                raise ValueError('provider response exceeds limit')
            data.extend(await reader.readexactly(length))
        else:
            while True:
                chunk = await reader.read(min(65536, MAX_BODY - len(data) + 1))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    raise ValueError('provider response exceeds limit')
        return status, bytes(data)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        except (OSError, asyncio.TimeoutError):
            pass

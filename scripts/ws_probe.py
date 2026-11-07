#!/usr/bin/env python3
"""Minimal WebSocket probe for the AS2 socket proxy (standard library only).

Opens a WebSocket, sends the AS2 version check and expects the server's `apiOK`
reply. That proves the whole path (Cloudflare or nginx -> socket proxy -> login
server) speaks the game protocol, which an HTTP 200 does not.

  ws_probe.py --url wss://host/socket/login --origin https://host
  ws_probe.py --url ws://web:8080/socket/login --origin https://evil.test --expect-reject

Runs anywhere with Python 3.9+ (the Mac agent, a CI runner, or inside a pod).
Prints only a verdict; it never sends credentials.
"""
import argparse
import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
VERCHK = b'<msg t="sys"><body action="verChk" r="0"><ver v="153"/></body></msg>\0'


class ProbeError(Exception):
    pass


class Stream:
    """A socket plus bytes already read past the HTTP headers (a server may send its
    first frame, e.g. a close, in the same TCP segment as the 101 response)."""

    def __init__(self, sock, pending=b''):
        self.sock = sock
        self.pending = pending

    def __getattr__(self, name):
        return getattr(self.sock, name)


def read_exactly(sock, count):
    data = b''
    if isinstance(sock, Stream) and sock.pending:
        data, sock.pending = sock.pending[:count], sock.pending[count:]
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ProbeError('connection closed by the peer')
        data += chunk
    return data


def read_frame(sock):
    first, second = read_exactly(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack('!H', read_exactly(sock, 2))[0]
    elif length == 127:
        length = struct.unpack('!Q', read_exactly(sock, 8))[0]
    if length > 1 << 20:
        raise ProbeError('frame too large')
    mask = read_exactly(sock, 4) if second & 0x80 else b''
    payload = read_exactly(sock, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def send_frame(sock, opcode, payload):
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack('!H', length)
    else:
        header += bytes([0x80 | 127]) + struct.pack('!Q', length)
    mask = os.urandom(4)
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def probe(url, origin, expect, timeout, text=False, user_agent='clubpenguin-lab-probe/1', message=VERCHK):
    """Return ('ok', reply) | ('rejected', reason). Raise ProbeError on other failures."""
    parts = urlparse(url)
    if parts.scheme not in ('ws', 'wss') or not parts.hostname:
        raise ProbeError('url must be ws:// or wss://')
    port = parts.port or (443 if parts.scheme == 'wss' else 80)
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    deadline = time.monotonic() + timeout
    sock = socket.create_connection((parts.hostname, port), timeout=timeout)
    try:
        if parts.scheme == 'wss':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        host = parts.hostname + (f':{parts.port}' if parts.port else '')
        request = (f'GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {user_agent}\r\n')
        if origin:
            request += f'Origin: {origin}\r\n'
        sock.sendall((request + '\r\n').encode())
        response = b''
        while b'\r\n\r\n' not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProbeError('connection closed during the handshake')
            response += chunk
            if len(response) > 16384:
                raise ProbeError('handshake response too large')
        head, _, rest = response.partition(b'\r\n\r\n')
        status = head.split(b'\r\n', 1)[0].decode('latin-1')
        if ' 101 ' not in status:
            return 'rejected', 'handshake refused: ' + status[:60]
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if ('sec-websocket-accept: ' + accept).lower() not in head.decode('latin-1').lower():
            raise ProbeError('invalid Sec-WebSocket-Accept')
        sock = Stream(sock, rest)
        send_frame(sock, 1 if text else 2, message)
        while time.monotonic() < deadline:
            sock.settimeout(max(0.5, deadline - time.monotonic()))
            opcode, payload = read_frame(sock)
            if opcode == 8:
                code = struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else 0
                return 'rejected', f'closed by the server with code {code}'
            if opcode == 9:
                send_frame(sock, 10, payload)
                continue
            if opcode in (1, 2):
                if expect.encode() in payload:
                    return 'ok', payload.decode('latin-1')[:80]
                raise ProbeError('unexpected reply from the server')
        raise ProbeError('timed out waiting for the reply')
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', required=True)
    parser.add_argument('--origin', default='')
    parser.add_argument('--expect', default='apiOK', help='text that must appear in the first reply')
    parser.add_argument('--expect-reject', action='store_true', help='succeed only if the server refuses')
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--text', action='store_true', help='send a text frame instead of binary')
    args = parser.parse_args()
    try:
        verdict, detail = probe(args.url, args.origin, args.expect, args.timeout, args.text)
    except (ProbeError, OSError, ssl.SSLError) as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}')
        raise SystemExit(1) from None
    if args.expect_reject:
        if verdict == 'rejected':
            print(f'PASS: origin refused as expected ({detail})')
            return
        print('FAIL: the server accepted an origin that must be refused')
        raise SystemExit(1)
    if verdict != 'ok':
        print(f'FAIL: {detail}')
        raise SystemExit(1)
    print('PASS: WebSocket path reached the login server and returned apiOK')


if __name__ == '__main__':
    main()

import base64
import hashlib
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ws_probe  # noqa: E402


class FakeServer:
    """A tiny RFC 6455 server: one connection, scripted behaviour."""

    def __init__(self, mode):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.origin = None
        threading.Thread(target=self._guarded, daemon=True).start()

    def _guarded(self):
        try:
            self.serve()
        except (OSError, ws_probe.ProbeError):
            pass  # the client may disconnect first in failure-mode tests
        finally:
            conn = getattr(self, 'conn', None)
            if conn:
                conn.close()

    def serve(self):
        conn, _ = self.sock.accept()
        self.sock.close()
        self.conn = conn
        request = b''
        while b'\r\n\r\n' not in request:
            request += conn.recv(4096)
        headers = dict(line.split(': ', 1) for line in request.decode().split('\r\n')[1:] if ': ' in line)
        self.origin = headers.get('Origin')
        if self.mode == 'refuse':
            conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            return
        accept = base64.b64encode(hashlib.sha1((headers['Sec-WebSocket-Key'] + ws_probe.GUID).encode()).digest()).decode()
        if self.mode == 'bad-accept':
            accept = 'AAAA'
        conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                      f'Sec-WebSocket-Accept: {accept}\r\n\r\n').encode())
        if self.mode == 'close-1008':
            conn.sendall(b'\x88\x02' + struct.pack('!H', 1008))
            return
        ws_probe.read_frame(conn)  # the client's verChk
        reply = b'<msg t="sys"><body action="apiOK" r="0"></body></msg>\0' if self.mode == 'ok' else b'garbage'
        conn.sendall(b'\x82' + bytes([len(reply)]) + reply)

    def url(self):
        return f'ws://127.0.0.1:{self.port}/socket/login'


class ProbeTests(unittest.TestCase):
    def test_ok_reply_and_origin_header(self):
        server = FakeServer('ok')
        verdict, detail = ws_probe.probe(server.url(), 'https://club.lab.dev', 'apiOK', 5)
        self.assertEqual(verdict, 'ok')
        self.assertEqual(server.origin, 'https://club.lab.dev')

    def test_close_1008_counts_as_rejected(self):
        verdict, detail = ws_probe.probe(FakeServer('close-1008').url(), 'https://evil.test', 'apiOK', 5)
        self.assertEqual(verdict, 'rejected')
        self.assertIn('1008', detail)

    def test_http_refusal_counts_as_rejected(self):
        verdict, _ = ws_probe.probe(FakeServer('refuse').url(), '', 'apiOK', 5)
        self.assertEqual(verdict, 'rejected')

    def test_wrong_reply_and_bad_accept_are_errors(self):
        with self.assertRaises(ws_probe.ProbeError):
            ws_probe.probe(FakeServer('garbage').url(), '', 'apiOK', 5)
        with self.assertRaises(ws_probe.ProbeError):
            ws_probe.probe(FakeServer('bad-accept').url(), '', 'apiOK', 5)

    def test_non_websocket_urls_are_refused(self):
        with self.assertRaises(ws_probe.ProbeError):
            ws_probe.probe('http://x/', '', 'apiOK', 1)


if __name__ == '__main__':
    unittest.main()

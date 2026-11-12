#!/usr/bin/env python3
"""Create two synthetic accounts, authenticate, and verify a shared AS2 room.

Only run against this lab: the test intentionally creates two accounts.
No passwords or session keys are printed.
"""
import argparse
import hashlib
import http.cookiejar
import secrets
import socket
import string
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def digest(value):
    return hashlib.md5(value.encode()).hexdigest()


def swap(value):
    return value[16:32] + value[:16]


class Client:
    def __init__(self, host, port):
        self.socket = socket.create_connection((host, port), timeout=10)
        self.buffer = b''

    def send(self, value):
        self.socket.sendall(value.encode() + b'\0')

    def receive(self):
        while b'\0' not in self.buffer:
            data = self.socket.recv(65536)
            if not data:
                raise RuntimeError('Server disconnected the synthetic client')
            self.buffer += data
        frame, self.buffer = self.buffer.split(b'\0', 1)
        return frame.decode()

    def expect(self, prefix):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frame = self.receive()
            if frame.startswith('%xt%e%'):
                raise RuntimeError('Game returned an error packet')
            if frame.startswith(prefix):
                return frame
        raise RuntimeError('Expected game response was not received')

    def handshake(self):
        self.send('<msg t="sys"><body action="verChk" r="0"><ver v="253"/></body></msg>')
        if 'apiOK' not in self.receive():
            raise RuntimeError('AS2 version handshake failed')
        self.send('<msg t="sys"><body action="rndK" r="-1"/></msg>')
        return ET.fromstring(self.receive()).find('./body/k').text

    def login(self, username, password):
        self.send('<msg t="sys"><body action="login" r="0"><login z="w1">'
                  f'<nick><![CDATA[{username}]]></nick><pword><![CDATA[{password}]]></pword>'
                  '</login></body></msg>')
        return self.expect('%xt%l%')

    def close(self):
        self.socket.close()


def register(base):
    name = 'lab' + ''.join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    password = secrets.token_urlsafe(16)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(fields):
        req = urllib.request.Request(base.rstrip('/') + '/create/legacy/', data=urllib.parse.urlencode(fields).encode())
        with opener.open(req, timeout=15) as response:
            result = urllib.parse.parse_qs(response.read().decode())
        if result.get('success') != ['1']:
            raise RuntimeError('Synthetic account registration failed')
        return result

    session = post({'action': 'validate_username', 'username': name, 'colour': '1', 'lang': 'es'})['sid'][0]
    post({'action': 'validate_password_email', 'sid': session, 'password': password,
          'password_confirm': password, 'email': name + '@example.com', 'lang': 'es'})
    return name, password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dash-url', default='http://dash:3000')
    parser.add_argument('--login-host', default='login')
    parser.add_argument('--login-port', type=int, default=6112)
    parser.add_argument('--world-host', default='glaciar')
    parser.add_argument('--world-port', type=int, default=9876)
    args = parser.parse_args()
    worlds = []
    ids = []
    try:
        for _ in range(2):
            name, password = register(args.dash_url)
            login = Client(args.login_host, args.login_port)
            try:
                key = login.handshake()
                hashed = swap(digest(swap(digest(password).upper()) + key + 'Y(02.>\'H}t":E1'))
                fields = login.login(name, hashed).strip('%').split('%')
                head = fields[3]
                if '|' in head:
                    parts = head.split('|')
                    player_id, login_key, confirmation = parts[0], parts[3], fields[4]
                else:
                    player_id, login_key, confirmation = head, fields[4], None
            finally:
                login.close()
            world = Client(args.world_host, args.world_port)
            worlds.append(world)
            world_key = world.handshake()
            token = swap(digest(login_key + world_key)) + login_key
            if confirmation:
                world.login(head, token + '#' + confirmation)
            else:
                world.login(name, token)
            world.send(f'%xt%s%j#js%-1%{player_id}%{login_key}%0%')
            world.expect('%xt%js%')
            world.send('%xt%s%j#jr%-1%100%0%0%')
            world.expect('%xt%jr%')
            ids.append(player_id)
        worlds[-1].send('%xt%s%j#grs%-1%')
        roster = worlds[-1].expect('%xt%grs%').strip('%').split('%')[3:]
        present = {record.split('|', 1)[0] for record in roster}
        if not set(ids).issubset(present):
            raise RuntimeError('Both synthetic players were not present in the same room')
        print('PASS: two accounts registered, authenticated, joined Glaciar, and shared room 100.')
    finally:
        for world in worlds:
            world.close()


if __name__ == '__main__':
    main()

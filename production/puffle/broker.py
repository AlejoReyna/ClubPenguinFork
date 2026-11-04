"""Bounded per-player job broker. No OAuth credentials or chat on disk.

Private /internal routes authenticate the world. Public /puffle/api routes
authenticate one device. Pairing requires an unbroadcast in-game code and a
second confirmation on the device before any chat can leave the world.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from .http import MAX_BODY


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Rejected(Exception):
    def __init__(self, status=403):
        self.status = status


class Broker:
    def __init__(self, secret, database=':memory:', *, clock=time.monotonic,
                 enabled=True, capacity=32, job_ttl=25):
        if len(secret) < 32 or not 1 <= capacity <= 128:
            raise ValueError('invalid broker configuration')
        self.secret, self.clock, self.enabled = secret, clock, enabled
        self.capacity, self.job_ttl = capacity, job_ttl
        self.pending, self.jobs, self.leases, self.recent = {}, {}, {}, {}
        self.counts = {'paired': 0, 'submitted': 0, 'completed': 0, 'cancelled': 0, 'expired': 0}
        self.db = sqlite3.connect(database)
        self.db.execute('CREATE TABLE IF NOT EXISTS devices (player INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL)')
        self.db.commit()

    def prune(self):
        now = self.clock()
        for key, item in list(self.pending.items()):
            if item['expires'] <= now:
                del self.pending[key]
        for key, item in list(self.jobs.items()):
            if item['expires'] <= now:
                self.drop(key, 'expired')
        for player, expiry in list(self.leases.items()):
            if expiry <= now:
                self.cancel_player(player)
                self.leases.pop(player, None)
        for player, recent in list(self.recent.items()):
            if not recent or now - recent[-1] >= 60:
                self.recent.pop(player, None)

    def drop(self, key, reason):
        job = self.jobs.pop(key, None)
        if job:
            job['context'] = []
            job.pop('result', None)
            self.counts[reason] += 1

    def cancel_player(self, player):
        for key, item in list(self.jobs.items()):
            if item['player'] == player:
                self.drop(key, 'cancelled')

    def device(self, token):
        row = self.db.execute('SELECT player FROM devices WHERE token=?', (digest(token),)).fetchone()
        if not row:
            raise Rejected()
        return row[0]

    @staticmethod
    def player(data):
        player = data.get('player')
        if type(player) is not int or not 0 < player < 2**31:
            raise Rejected(400)
        return player

    def dispatch(self, path, token, data):
        self.prune()
        if not isinstance(data, dict):
            raise Rejected(400)
        if path == '/healthz':
            return {'ok': True, 'enabled': self.enabled}
        if not self.enabled:
            raise Rejected(503)
        if path.startswith('/internal/'):
            if not hmac.compare_digest(token, self.secret):
                raise Rejected()
            player = self.player(data)
            return self.internal(path, player, data)
        if path == '/puffle/api/pair':
            if len(self.pending) >= self.capacity:
                raise Rejected(429)
            token, code = secrets.token_urlsafe(32), secrets.token_hex(5).upper()
            self.pending[digest(token)] = {'code': code, 'expires': self.clock() + 300, 'player': None}
            return {'token': token, 'code': code, 'expires_in': 300}
        if path in ('/puffle/api/pair-status', '/puffle/api/confirm'):
            item = self.pending.get(digest(token))
            if not item:
                raise Rejected()
            if path.endswith('pair-status'):
                return {'player': item['player'], 'nickname': item.get('nickname', '')}
            player = self.player(data)
            if item['player'] != player:
                raise Rejected()
            if self.db.execute('SELECT count(*) FROM devices').fetchone()[0] >= self.capacity and not self.db.execute(
                    'SELECT 1 FROM devices WHERE player=?', (player,)).fetchone():
                raise Rejected(429)
            self.cancel_player(player)
            self.db.execute('INSERT OR REPLACE INTO devices VALUES (?,?)', (player, digest(token)))
            self.db.commit()
            del self.pending[digest(token)]
            self.counts['paired'] += 1
            return {'player': player}
        player = self.device(token)
        if path == '/puffle/api/disconnect':
            self.cancel_player(player)
            self.db.execute('DELETE FROM devices WHERE player=?', (player,))
            self.db.commit()
            self.leases.pop(player, None)
            return {'ok': True}
        if path == '/puffle/api/poll':
            self.leases[player] = self.clock() + 4
            for key, job in self.jobs.items():
                if job['player'] == player and not job['claimed']:
                    job['claimed'] = True
                    return {'job': key, 'context': job['context'],
                            'expires_in': max(0, job['expires'] - self.clock())}
            return {'job': None}
        job = self.jobs.get(data.get('job'))
        if not job or job['player'] != player:
            raise Rejected(410)
        if path == '/puffle/api/active':
            self.leases[player] = self.clock() + 4
            return {'active': True}
        if path == '/puffle/api/result':
            if not job['claimed'] or 'result' in job:
                raise Rejected(409)
            if type(data.get('reply')) is not bool or not isinstance(data.get('text'), str):
                raise Rejected(400)
            from experiments.puffle_sim.engine import speech_text
            job['result'] = {'reply': data['reply'], 'text': speech_text(data['text'])}
            job['context'] = []
            return {'ok': True}
        raise Rejected(404)

    def internal(self, path, player, data):
        if path == '/internal/pair':
            code = data.get('code')
            if not isinstance(code, str) or len(code) != 10:
                raise Rejected(400)
            for item in self.pending.values():
                if hmac.compare_digest(item['code'], code) and item['player'] is None:
                    item.update(player=player, nickname=str(data.get('nickname', ''))[:48])
                    item['code'] = ''  # one use, never broadcast to other players
                    return {'ok': True}
            raise Rejected()
        if path == '/internal/status':
            row = self.db.execute('SELECT 1 FROM devices WHERE player=?', (player,)).fetchone()
            return {'paired': bool(row), 'online': self.leases.get(player, 0) > self.clock()}
        if path == '/internal/revoke':
            self.cancel_player(player)
            self.db.execute('DELETE FROM devices WHERE player=?', (player,))
            self.db.commit()
            self.leases.pop(player, None)
            return {'ok': True}
        if path == '/internal/submit':
            if not self.db.execute('SELECT 1 FROM devices WHERE player=?', (player,)).fetchone():
                raise Rejected()
            if self.leases.get(player, 0) <= self.clock():
                raise Rejected(503)
            if len(self.jobs) >= self.capacity or any(j['player'] == player for j in self.jobs.values()):
                raise Rejected(429)
            recent = [t for t in self.recent.get(player, ()) if self.clock() - t < 60]
            if recent and (self.clock() - recent[-1] < 2 or len(recent) >= 8):
                raise Rejected(429)
            context = data.get('context')
            if (not isinstance(context, list) or not 1 <= len(context) <= 32 or any(
                    not isinstance(x, list) or len(x) != 2 or x[0] not in ('player', 'puffle')
                    or not isinstance(x[1], str) or len(x[1].encode()) > 512 for x in context)):
                raise Rejected(400)
            key = secrets.token_hex(16)
            self.jobs[key] = {'player': player, 'context': context, 'expires': self.clock() + self.job_ttl,
                              'claimed': False}
            self.recent[player] = (recent + [self.clock()])[-8:]
            self.counts['submitted'] += 1
            return {'job': key}
        key = data.get('job')
        job = self.jobs.get(key)
        if not job or job['player'] != player:
            raise Rejected(410)
        if path == '/internal/cancel':
            self.drop(key, 'cancelled')
            return {'ok': True}
        if path == '/internal/result':
            result = job.get('result')
            if result is not None:
                self.jobs.pop(key)
                self.counts['completed'] += 1
            return {'result': result}
        raise Rejected(404)


async def serve(broker, host='0.0.0.0', port=9103):
    connections = 0

    async def handle(reader, writer):
        nonlocal connections
        connections += 1
        try:
            if connections > 64:
                raise Rejected(429)
            async with asyncio.timeout(5):
                header = await reader.readuntil(b'\r\n\r\n')
                if len(header) > 8192:
                    raise Rejected(413)
                lines = header.decode('ascii').split('\r\n')
                method, path, _ = lines[0].split()
                fields = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    key, value = line.split(':', 1)
                    if key.lower() in fields:
                        raise Rejected(400)
                    fields[key.lower()] = value.strip()
                size = int(fields.get('content-length', '0'))
                if not 0 <= size <= MAX_BODY or 'transfer-encoding' in fields:
                    raise Rejected(413)
                if path == '/metrics' and method == 'GET':
                    broker.prune()
                    body = ''.join(f'puffle_{key}_total {value}\n' for key, value in broker.counts.items())
                    body += f'puffle_jobs {len(broker.jobs)}\npuffle_enabled {int(broker.enabled)}\n'
                    await respond(writer, 200, body.encode(), 'text/plain')
                    return
                if method not in ('POST', 'GET') or (method == 'GET' and path != '/healthz'):
                    raise Rejected(405)
                data = json.loads(await reader.readexactly(size)) if size else {}
                token = fields.get('authorization', '').removeprefix('Bearer ')
                result = broker.dispatch(path, token, data)
                await respond(writer, 200, json.dumps(result).encode())
        except Rejected as error:
            await respond(writer, error.status, b'{"error":"unavailable"}')
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await respond(writer, 400, b'{"error":"invalid"}')
        except (OSError, TimeoutError):
            pass
        finally:
            connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    server = await asyncio.start_server(handle, host, port, limit=MAX_BODY + 8192)
    async with server:
        await server.serve_forever()


async def respond(writer, status, body, content_type='application/json'):
    writer.write((f'HTTP/1.1 {status} Result\r\nContent-Length: {len(body)}\r\n'
                  f'Content-Type: {content_type}\r\nCache-Control: no-store\r\n'
                  'Connection: close\r\n\r\n').encode() + body)
    await writer.drain()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9103)
    args = parser.parse_args()
    path = Path(os.environ.get('PUFFLE_DATABASE', '/var/lib/puffle/devices.sqlite'))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    enabled = os.environ.get('PUFFLE_ENABLED') == 'true'
    secret = Path(os.environ['PUFFLE_SECRET_FILE']).read_text().strip() if enabled else secrets.token_hex(32)
    broker = Broker(secret, str(path), enabled=enabled)
    asyncio.run(serve(broker, args.host, args.port))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Integration smoke for /party on a running glaciar-dev stack (Docker network)."""
import argparse
import asyncio
import http.cookiejar
import json
import secrets
import urllib.parse
import urllib.request
from pathlib import Path

from bots import as2  # noqa: E402  # PYTHONPATH=/app/bots when run in Docker


def register_player(base):
    """Same two-step flow as scripts/game-smoke.py / bot_accounts.register."""
    name = 'lab' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8))
    password = secrets.token_hex(8)

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(base.rstrip('/') + '/create/legacy/', data=data)
        with opener.open(req, timeout=15) as resp:
            return urllib.parse.parse_qs(resp.read().decode())

    first = post({'action': 'validate_username', 'username': name, 'colour': '1', 'lang': 'es'})
    sid = first['sid'][0]
    result = post({'action': 'validate_password_email', 'sid': sid, 'password': password,
                   'password_confirm': password, 'email': name + '@example.com', 'lang': 'es'})
    if result.get('success') != ['1']:
        raise RuntimeError(f'registration failed: {result}')
    return name, password


async def join_disco(login_host, world_host, username, password):
    login = await as2.AS2Connection.connect(login_host, 6112, timeout=15)
    try:
        key = await login.handshake(15)
        fields = await login.login_legacy(username, as2.login_server_password(password, key), 15)
    finally:
        await login.close()
    logged = as2.parse_login(fields)
    world = await as2.AS2Connection.connect(world_host, 9876, timeout=15)
    try:
        rndk = await world.handshake(15)
        await world.login_world(username, logged, rndk, 15)
        await world.join_world(logged.player_id, logged.login_key, 15)
        frame = await world.join_room(120, 15, 330, 247)
        return world, logged.player_id, frame
    except BaseException:
        await world.close()
        raise


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dash', default='http://web:8080')
    parser.add_argument('--login', default='login')
    parser.add_argument('--world', default='glaciar')
    parser.add_argument('--bus', default='/var/lib/lab-party/scene.json')
    args = parser.parse_args()
    user, password = register_player(args.dash)
    world, pid, _frame = await join_disco(args.login, args.world, user, password)
    try:
        await world.send_line(f'%xt%s%m#sm%-1%{int(pid)}%/party%')
        await asyncio.sleep(1.5)
        try:
            bus = json.loads(Path(args.bus).read_text())
        except (OSError, json.JSONDecodeError):
            bus = None
        if not bus or bus.get('status') not in ('requested', 'arriving', 'ready', 'dancing'):
            raise SystemExit(f'FAIL: bus document: {bus!r}')
        if int(bus.get('invoker_id')) != int(pid):
            raise SystemExit(f'FAIL: invoker_id {bus.get("invoker_id")} != {pid}')
        await world.send_line('%xt%s%u#sf%-1%26%')
        for _ in range(40):
            await asyncio.sleep(0.5)
            try:
                bus = json.loads(Path(args.bus).read_text())
            except (OSError, json.JSONDecodeError):
                bus = {}
            if bus.get('status') in ('dancing', 'ready', 'arriving'):
                break
        print('PASS: /party bus=', bus.get('status'), 'invoker=', pid)
    finally:
        await world.close()


if __name__ == '__main__':
    asyncio.run(main())
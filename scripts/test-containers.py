#!/usr/bin/env python3
"""Test an isolated release before publishing or deploying it."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', default='lab-002')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
prefix = 'penguin-test-' + secrets.token_hex(4)
containers = []


def run(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


try:
    run(['docker', 'network', 'create', prefix])
    with tempfile.TemporaryDirectory(prefix='penguin-test-') as temp:
        env = Path(temp) / 'runtime.env'
        env.write_text('POSTGRES_USER=houdini\nPOSTGRES_DB=houdini\nPOSTGRES_PASSWORD=' + secrets.token_hex(16)
                       + '\nPOSTGRES_HOST=postgres\nREDIS_HOST=redis\n')
        env.chmod(0o600)

        def start(alias, image, mode=None):
            name = prefix + '-' + alias
            command = ['docker', 'run', '-d', '--name', name, '--network', prefix,
                       '--network-alias', alias, '--env-file', str(env)]
            if mode:
                command += ['-e', 'MODE=' + mode]
            run(command + [image])
            containers.append(name)
            return name

        postgres = start('postgres', f'clubpenguin/database:{args.tag}')
        start('redis', 'redis:7.4-alpine')
        for _ in range(40):
            ready = subprocess.run(['docker', 'exec', postgres, 'pg_isready', '-U', 'houdini', '-d', 'houdini'], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Postgres did not start')
        for alias, mode in [('login', 'login'), ('glaciar', 'world'), ('dash', 'dash'), ('proxy', 'proxy')]:
            start(alias, f'clubpenguin/runtime:{args.tag}', mode)
        # Wait for the application listeners, not just container startup.
        readiness = '''import socket,time
for host,port in [('login',6112),('glaciar',9876),('dash',3000),('proxy',8765)]:
 for attempt in range(40):
  try:
   s=socket.create_connection((host,port),timeout=1);s.close();break
  except OSError:time.sleep(0.5)
 else:raise SystemExit('An application listener did not become ready')
'''
        run(['docker', 'exec', '-i', prefix + '-dash', 'python', '-'], input=readiness)
        result = run(['docker', 'exec', '-i', prefix + '-dash', 'python', '-'], input=(root / 'scripts/game-smoke.py').read_text())
        print(result.stdout.strip())
        proxy_test = '''import asyncio,websockets
async def main():
 async with websockets.connect('ws://proxy:8765/socket/login',origin='http://localhost') as ws:
  await ws.send(b'<msg t="sys"><body action="verChk" r="0"><ver v="153"/></body></msg>\\0')
  reply=await asyncio.wait_for(ws.recv(),timeout=10)
  assert b'apiOK' in reply
asyncio.run(main())
'''
        run(['docker', 'exec', '-i', prefix + '-dash', 'python', '-'], input=proxy_test)
        print('PASS: WebSocket proxy forwards the AS2 handshake to the allowed login endpoint.')
except subprocess.CalledProcessError as exc:
    print((exc.stderr or '')[-2000:])
    for name in containers:
        result = subprocess.run(['docker', 'logs', '--tail', '8', name], capture_output=True, text=True)
        print(name + ':\n' + (result.stdout + result.stderr)[-1200:])
    raise SystemExit(1)
finally:
    for name in reversed(containers):
        subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True)
    subprocess.run(['docker', 'network', 'rm', prefix], capture_output=True)

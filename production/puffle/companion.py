"""Player-owned companion. OAuth and music run locally; outbound HTTPS only."""
import argparse
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import sys

from .http import request


def save(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load(path):
    if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError('companion configuration must be private (0600)')
    data = json.loads(path.read_text())
    if not isinstance(data.get('url'), str) or not isinstance(data.get('token'), str):
        raise ValueError('invalid companion configuration')
    return data


async def pair(args):
    if args.config.exists():
        raise ValueError('Already paired; disconnect before pairing again')
    result = await request(args.url, '/puffle/api/pair')
    token = result['token']
    # Save before showing the code, so failure never leaves an unknown paired device.
    save(args.config, {'url': args.url, 'token': token, 'pending': True})
    print('En tu propio iglú, escribe en el chat: puffle conectar ' + result['code'], flush=True)
    print('El código es privado. La conversación se envía a esta computadora; las visitas ven los globos.', flush=True)
    for _ in range(150):
        await asyncio.sleep(2)
        result = await request(args.url, '/puffle/api/pair-status', token)
        if result['player'] is not None:
            print(f'Pingüino: {result["nickname"]} (ID {result["player"]})', flush=True)
            answer = await asyncio.to_thread(input, '¿Es tu pingüino y autorizas conversación/música en esta computadora? [si/NO] ')
            if answer.strip().casefold() != 'si':
                args.config.unlink(missing_ok=True)
                return
            await request(args.url, '/puffle/api/confirm', token, {'player': result['player']})
            save(args.config, {'url': args.url, 'token': token, 'player': result['player']})
            print('Emparejado. Ejecuta el comando run para iniciar.', flush=True)
            return
    args.config.unlink(missing_ok=True)
    raise ValueError('Pairing expired')


def provider(args):
    from experiments.puffle_sim.chatgpt_provider import ChatGPTProvider
    from experiments.puffle_sim.provider import SimulatedProcess
    result = SimulatedProcess() if args.simulated else ChatGPTProvider(retries=0, allow_actions=args.allow_local_actions)
    backend = None
    if args.music_mac:
        from experiments.puffle_sim.music_mac import MusicMacBackend
        backend = MusicMacBackend()
    elif args.music_safari_window_id is not None:
        from experiments.puffle_sim.music_safari import SafariMusicBackend
        backend = SafariMusicBackend(args.music_safari_window_id)
    elif args.spotify:
        from experiments.puffle_sim.spotify import SpotifyWebAPI
        from experiments.puffle_sim.spotify_auth import SpotifyAuth
        backend = SpotifyWebAPI(SpotifyAuth())
    if backend is not None:
        from experiments.puffle_sim.spotify import SpotifyAgent
        result = SpotifyAgent(result, lambda key: backend if key == 'local' else None)
    return result


async def run_job(cfg, job, evaluator, allow_actions=False):
    from experiments.puffle_sim.host_relay import Relay
    from experiments.puffle_sim.computer_tools import confirm_and_open
    # Reuse validation, fixed responses and cancellable child inference from the
    # existing relay, without exposing a local listener or sharing an account.
    relay = Relay(evaluator, 'companion-local', timeout=min(23, job['expires_in']),
                  log=lambda _: None, action_handler=confirm_and_open if allow_actions else None)
    raw = json.dumps({'secret': 'companion-local', 'context': job['context']}).encode()
    task = asyncio.create_task(relay.answer(raw))
    try:
        while not task.done():
            await request(cfg['url'], '/puffle/api/active', cfg['token'], {'job': job['job']})
            await asyncio.sleep(0.25)
        answer = await task
        await request(cfg['url'], '/puffle/api/result', cfg['token'], dict(answer or {'reply': False, 'text': ''}, job=job['job']))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run(args):
    cfg = load(args.config)
    if cfg.get('pending'):
        raise ValueError('Pairing was not completed; remove the pending configuration and pair again')
    from experiments.puffle_sim.chatgpt_process import ProcessChatGPTProvider
    command = [sys.executable, '-m', 'production.puffle.device_worker']
    for flag in ('music_mac', 'spotify', 'simulated', 'allow_local_actions'):
        if getattr(args, flag):
            command.append('--' + flag.replace('_', '-'))
    if args.music_safari_window_id is not None:
        command += ['--music-safari-window-id', str(args.music_safari_window_id)]
    evaluator = ProcessChatGPTProvider(command=command)
    print('Puffle conectado. Ctrl+C detiene conversación y música.', flush=True)
    while True:
        try:
            job = await request(cfg['url'], '/puffle/api/poll', cfg['token'])
            if job['job']:
                await run_job(cfg, job, evaluator, args.allow_local_actions)
            await asyncio.sleep(0.5)
        except (OSError, ConnectionError, TimeoutError, ValueError):
            print('Puffle sin conexión; reintentando.', file=sys.stderr, flush=True)
            await asyncio.sleep(3)


async def disconnect(args):
    cfg = load(args.config)
    await request(cfg['url'], '/puffle/api/disconnect', cfg['token'])
    args.config.unlink()
    print('Dispositivo desconectado. Las cuentas locales se administran en la página de conexión.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/puffle-lab/companion.json')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('pair')
    p.add_argument('--url', required=True)
    p.set_defaults(func=pair)
    p = commands.add_parser('run')
    music = p.add_mutually_exclusive_group()
    music.add_argument('--music-mac', action='store_true')
    music.add_argument('--music-safari-window-id', type=int)
    music.add_argument('--spotify', action='store_true')
    p.add_argument('--simulated', action='store_true', help='no inference or quota consumption')
    p.add_argument('--allow-local-actions', action='store_true')
    p.set_defaults(func=run)
    commands.add_parser('disconnect').set_defaults(func=disconnect)
    args = parser.parse_args()
    args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One process owns this device and its OAuth renewal at a time.
    with (args.config.parent / 'companion.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another companion process is running')
        try:
            asyncio.run(args.func(args))
        except KeyboardInterrupt:
            pass
        except (ValueError, OSError, ConnectionError, TimeoutError):
            raise SystemExit('No se pudo completar la operación. Revisa la conexión y la configuración privada.')


if __name__ == '__main__':
    main()

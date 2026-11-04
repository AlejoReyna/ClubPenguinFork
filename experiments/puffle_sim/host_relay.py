"""Host-side inference relay for the disposable fixture.

The world container never gets internet or OpenAI credentials. It sends one
newline-delimited JSON request {secret, context} (<= 16 KiB) and receives only
{reply: bool, text: str}, already cut with speech_text. The secret is random
per start and unrelated to any OpenAI token. Concurrency 1, absolute timeout,
and no chat text, tokens or provider errors are ever logged (only counters).
"""
import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import sys

MAX_REQUEST = 16384
MAX_RESPONSE = 2048
TIMEOUT = 25.0
ROLES = ('player', 'puffle')
QUOTA_TEXT = 'Necesito descansar un rato.'
# Model instructions live in chatgpt_provider.INSTRUCTIONS (host side only).


def _log(event):
    # Only fixed event names; never request content.
    print('[puffle-relay] ' + event, file=sys.stderr, flush=True)


def parse_request(raw, secret):
    """Returns a validated context tuple or raises ValueError (no content in message)."""
    if len(raw) > MAX_REQUEST:
        raise ValueError('too large')
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {'secret', 'context'}:
        raise ValueError('shape')
    given = data['secret']
    if not isinstance(given, str) or not hmac.compare_digest(given.encode(), secret.encode()):
        raise PermissionError('auth')
    context = data['context']
    if not isinstance(context, list) or len(context) > 32:
        raise ValueError('context')
    out = []
    for item in context:
        if (not isinstance(item, list) or len(item) != 2 or item[0] not in ROLES
                or not isinstance(item[1], str)):
            raise ValueError('item')
        out.append((item[0], item[1]))
    return tuple(out)


class Relay:
    def __init__(self, provider, secret, timeout=TIMEOUT, log=_log, action_handler=None):
        from .engine import speech_text
        self.provider, self.secret, self.timeout, self.log = provider, secret, timeout, log
        self.speech_text = speech_text
        self.busy = asyncio.Lock()
        self.action_handler = action_handler

    async def answer(self, raw):
        from .engine import QuotaExhausted
        try:
            context = parse_request(raw, self.secret)
        except PermissionError:
            self.log('rejected: auth')
            return None
        except (ValueError, TypeError):
            self.log('rejected: invalid')
            return None
        if self.busy.locked():
            self.log('busy')
            return {'reply': False, 'text': ''}
        async with self.busy:
            try:
                answer = await asyncio.wait_for(self.provider.evaluate(context), self.timeout)
                if isinstance(answer, dict):
                    from .computer_tools import requested_action
                    action = answer.get('action')
                    app = action.get('application') if isinstance(action, dict) else None
                    tool_call = {'type': 'function_call', 'name': 'abrir_aplicacion',
                                 'arguments': json.dumps({'aplicacion': app})}
                    if not self.action_handler or requested_action(tool_call, context) != action:
                        return {'reply': True, 'text': 'No puedo hacer esa acción.'}
                    text = await self.action_handler(action)
                    return {'reply': True, 'text': self.speech_text(text)}
                decision, text = answer
            except QuotaExhausted:
                self.log('quota exhausted; paused')
                return {'reply': True, 'text': QUOTA_TEXT}
            except asyncio.TimeoutError:
                self.log('timeout')
                return {'reply': False, 'text': ''}
            except Exception as error:  # class name only, never the message
                self.log('provider error: ' + type(error).__name__)
                return {'reply': False, 'text': ''}
        text = self.speech_text(text)
        if decision is not True or not text or text == '(silencio)':
            self.log('ok: silent')
            return {'reply': False, 'text': ''}
        self.log('ok: reply')
        return {'reply': True, 'text': text}

    async def handle(self, reader, writer):
        answer_task = disconnect_task = None
        try:
            raw = await asyncio.wait_for(reader.readline(), 5)
            if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
                self.log('rejected: invalid')
                return
            answer_task = asyncio.create_task(self.answer(raw))
            # A departing player cancels the client connection. Propagate that
            # cancellation to the provider before considering another request.
            disconnect_task = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait((answer_task, disconnect_task),
                                         return_when=asyncio.FIRST_COMPLETED)
            if disconnect_task in done:
                return
            result = answer_task.result()
            if result is not None:
                writer.write(json.dumps(result, ensure_ascii=True).encode() + b'\n')
                await writer.drain()
        except (asyncio.TimeoutError, OSError, ValueError):
            self.log('connection error')
        finally:
            for task in (answer_task, disconnect_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (answer_task, disconnect_task) if task is not None),
                                 return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class RelayClient:
    """Container-side provider: same evaluate(context) interface as SimulatedProcess."""
    def __init__(self, host, port, secret, timeout=TIMEOUT + 5):
        self.host, self.port, self.secret, self.timeout = host, port, secret, timeout

    async def evaluate(self, context):
        data = json.dumps({'secret': self.secret, 'context': [list(x) for x in context]},
                          ensure_ascii=True).encode() + b'\n'
        if len(data) > MAX_REQUEST:
            raise ValueError('context too large')
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(data)
            await writer.drain()
            out = await asyncio.wait_for(reader.readline(), self.timeout)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if not out or len(out) > MAX_RESPONSE:
            raise ValueError('relay failed')
        result = json.loads(out)
        if (not isinstance(result, dict) or type(result.get('reply')) is not bool
                or not isinstance(result.get('text'), str)):
            raise ValueError('invalid relay result')
        return result['reply'], result['text']


async def serve(provider, secret, host, port, ready_file=None, action_handler=None):
    relay = Relay(provider, secret, action_handler=action_handler)
    server = await asyncio.start_server(relay.handle, host, port, limit=MAX_REQUEST + 1)
    bound = server.sockets[0].getsockname()[1]
    if ready_file:
        ready_file.write_text(str(bound))
    _log('listening on %s:%d' % (host, bound))
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--secret-file', type=Path, required=True)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--allow-local-actions', action='store_true')
    parser.add_argument('--spotify', action='store_true',
                        help='owner Spotify control with the local connection (spotify_auth)')
    parser.add_argument('--music-mac', action='store_true',
                        help='owner controls the local macOS Music application')
    parser.add_argument('--music-safari-window-id', type=int,
                        help='experimental: owner controls Apple Music in this Safari window')
    args = parser.parse_args()
    if sum((bool(args.spotify), bool(args.music_mac), args.music_safari_window_id is not None)) > 1:
        parser.error('Choose one music player for this local session')
    if args.music_safari_window_id is not None and not 0 < args.music_safari_window_id < 2**31:
        parser.error('Invalid Safari window')
    secret = args.secret_file.read_text().strip()
    if len(secret) < 32:
        raise SystemExit('secret too short')
    from .chatgpt_process import ProcessChatGPTProvider
    # Credentials and HTTP calls live in a cancellable child process.
    provider = ProcessChatGPTProvider(allow_actions=args.allow_local_actions)
    if args.spotify:
        # Single local owner: the bridge already admitted only his igloo chat.
        from .spotify import SpotifyAgent, SpotifyWebAPI
        from .spotify_auth import SpotifyAuth
        backend = SpotifyWebAPI(SpotifyAuth())
        provider = SpotifyAgent(provider, lambda key: backend if backend.auth.status() == 'conectado' else None)
    elif args.music_mac:
        from .music_mac import MusicMacBackend
        from .spotify import SpotifyAgent
        backend = MusicMacBackend()
        provider = SpotifyAgent(provider, lambda key: backend if key == 'local' else None)
    elif args.music_safari_window_id is not None:
        from .music_safari import SafariMusicBackend
        from .spotify import SpotifyAgent
        backend = SafariMusicBackend(args.music_safari_window_id)
        provider = SpotifyAgent(provider, lambda key: backend if key == 'local' else None)
    action_handler = None
    if args.allow_local_actions:
        from .computer_tools import confirm_and_open
        action_handler = confirm_and_open
    asyncio.run(serve(provider, secret, args.host, args.port, args.ready_file, action_handler))


if __name__ == '__main__':
    main()

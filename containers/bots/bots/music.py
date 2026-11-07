"""Forward a player's OWN music order, said to a bot, to the puffle host (Spotify).

«Cobra, pon despacito» in the Night Club changes the music of whoever said it, on
the Spotify that player connected in «Mi puffle». A bot never picks music on its
own initiative and never acts for someone else: it forwards the sender id that
the world assigned to the chat line plus the speaker's words without the bot's
name. The host parses them with the same deterministic parser and per-player
limits as the puffle; anything that is not a music order comes back unhandled
and the bot answers normally (song names never reach the bot's language model).
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from .http_client import post_json

MAX_TEXT = 300


class MusicClient:
    def __init__(self, url, secret, timeout=28.0):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
                or parts.path not in ('', '/') or parts.query):
            raise ValueError('invalid BOTS_MUSIC_URL')
        if len(secret) < 32 or any(c in secret for c in '\r\n '):
            raise ValueError('invalid music secret')
        self.endpoint = url.rstrip('/') + '/internal/music'
        self.hostname, self.secret, self.timeout = parts.hostname, secret, timeout

    async def handle(self, player_id, text):
        """The bubble to say when the host ran a music order, else None."""
        if not text or type(player_id) is not int or player_id <= 0:
            return None
        body = json.dumps({'player': player_id, 'text': text[:MAX_TEXT]}).encode()
        status, raw = await asyncio.wait_for(
            post_json(self.endpoint, 'Bearer ' + self.secret, body, plain_hosts=(self.hostname,)),
            self.timeout)
        if status != 200:
            raise ConnectionError(f'music service status {status}')
        data = json.loads(raw)
        if isinstance(data, dict) and data.get('handled') is True and isinstance(data.get('text'), str):
            return data['text']
        return None


def from_env(env):
    """MusicClient when BOTS_MUSIC_URL and BOTS_MUSIC_SECRET_FILE are set, else None."""
    url, path = env.get('BOTS_MUSIC_URL', '').strip(), env.get('BOTS_MUSIC_SECRET_FILE', '').strip()
    if not url or not path:
        return None
    return MusicClient(url, Path(path).read_text().strip())

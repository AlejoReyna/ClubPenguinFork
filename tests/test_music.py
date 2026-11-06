"""Players can ask any bot for THEIR OWN music; the bot never decides it for them."""
import asyncio
import json
import tempfile
import unittest

from bots import music as music_mod
from bots.config import parse_bot
from bots.provider import SimulatedProvider
from bots.text import strip_mention

from support import FakeWorld
from test_session import chats_from, make_session, start_running, stop_running, wait_until

COBRA = ['DJ Cobra', 'djia', 'DJ', 'Cobra', 'Cadence']


class StripMentionTests(unittest.TestCase):
    def test_bot_name_and_greeting_disappear(self):
        for said in ('Cobra, pon despacito', 'pon despacito, cobra!', 'oye DJ Cobra: pon despacito',
                     'Cadence pon despacito', 'hey cadence, ¡pon despacito!'):
            self.assertEqual(strip_mention(said, COBRA), 'pon despacito', said)

    def test_other_words_stay(self):
        self.assertEqual(strip_mention('Cobra siguiente canción', COBRA), 'siguiente canción')
        self.assertEqual(strip_mention('Juanito, un café', ['Juanito', 'baristaia']), 'un café')
        self.assertEqual(strip_mention('cobrador pon algo', COBRA), 'cobrador pon algo')


class ConfigTests(unittest.TestCase):
    def test_music_is_on_for_every_bot_unless_disabled(self):
        self.assertTrue(parse_bot(0, {'username': 'nuevobot', 'password': 'clave123'}).music)
        self.assertFalse(parse_bot(0, {'username': 'nuevobot', 'password': 'clave123', 'music': False}).music)

    def test_client_from_environment(self):
        self.assertIsNone(music_mod.from_env({}))
        with tempfile.NamedTemporaryFile('w') as secret:
            secret.write('s' * 40)
            secret.flush()
            client = music_mod.from_env({'BOTS_MUSIC_URL': 'http://puffle:9104', 'BOTS_MUSIC_SECRET_FILE': secret.name})
            self.assertEqual(client.endpoint, 'http://puffle:9104/internal/music')
            with self.assertRaises(ValueError):
                music_mod.MusicClient('http://puffle:9104', 'corto')
            with self.assertRaises(ValueError):
                music_mod.MusicClient('http://puffle:9104/otra/ruta', 's' * 40)


class HostProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_sender_and_words_with_bearer(self):
        seen = {}

        async def handler(reader, writer):
            head = await reader.readuntil(b'\r\n\r\n')
            length = int([l for l in head.split(b'\r\n') if l.lower().startswith(b'content-length')][0].split(b':')[1])
            seen['head'] = head.decode()
            seen['body'] = json.loads(await reader.readexactly(length))
            answer = json.dumps({'handled': True, 'text': 'Puse «Despacito».'}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(answer) + answer)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            client = music_mod.MusicClient(f'http://127.0.0.1:{port}', 's' * 40)
            self.assertEqual(await client.handle(171, 'pon despacito'), 'Puse «Despacito».')
            self.assertIsNone(await client.handle('171', 'pon despacito'))  # never a client-made id
        self.assertIn('POST /internal/music', seen['head'])
        self.assertIn('Authorization: Bearer ' + 's' * 40, seen['head'])
        self.assertEqual(seen['body'], {'player': 171, 'text': 'pon despacito'})


class FakeMusic:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def handle(self, player_id, text):
        self.calls.append((player_id, text))
        if self.fail:
            raise ConnectionError('host down')
        return 'Puse «Despacito».' if text == 'pon despacito' else None


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_bot(self, music, lines):
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        provider = SimulatedProvider(replies=['Hola desde el muelle'])
        session = make_session(world, provider)
        session.music = music
        stop_event, task = await start_running(session, 'music')
        try:
            await wait_until(lambda: session.joined_room)
            for count, (player, line) in enumerate(lines, 1):
                await world.broadcast_human_chat(100, player, line)
                await wait_until(lambda: len(chats_from(world, session.player_id)) >= count, timeout=8)
                await asyncio.sleep(0.3)  # player cooldown in the test policy
            return chats_from(world, session.player_id), provider
        finally:
            await stop_running(stop_event, task)
            await world.stop()

    async def test_music_order_goes_to_the_host_not_to_the_model(self):
        music = FakeMusic()
        chats, provider = await self.run_bot(music, [(171, 'GuiaIA, pon despacito'), (171, 'GuiaIA hola')])
        self.assertEqual(chats, ['Puse «Despacito».', 'Hola desde el muelle'])
        self.assertEqual(music.calls, [(171, 'pon despacito'), (171, '')])  # «GuiaIA hola»: only a greeting
        self.assertEqual(provider.calls, 1)
        self.assertTrue(all('despacito' not in str(m) for call in provider.messages_received for m in call))

    async def test_host_down_means_a_normal_reply(self):
        chats, provider = await self.run_bot(FakeMusic(fail=True), [(171, 'GuiaIA, pon despacito')])
        self.assertEqual(chats, ['Hola desde el muelle'])

    async def test_bot_with_music_disabled_never_calls_the_host(self):
        music = FakeMusic()
        world = FakeWorld({'guiaia': 'GuiaIA'})
        await world.start()
        session = make_session(world, SimulatedProvider(replies=['Hola']))
        session.music = music
        session.bot.music = False
        stop_event, task = await start_running(session, 'no-music')
        try:
            await wait_until(lambda: session.joined_room)
            await world.broadcast_human_chat(100, 171, 'GuiaIA pon despacito')
            await wait_until(lambda: chats_from(world, session.player_id), timeout=8)
        finally:
            await stop_running(stop_event, task)
            await world.stop()
        self.assertEqual(music.calls, [])


if __name__ == '__main__':
    unittest.main()

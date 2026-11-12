"""Public dialog feed (containers/runtime/dialog_feed.py): allow-list, limits and failure handling.

The contract test at the bottom is the privacy gate: the public JSON must never carry the
player hash or the human message, whatever the day files contain.
"""
import datetime
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('dialog_feed', ROOT / 'containers/runtime/dialog_feed.py')
feed = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = feed
_spec.loader.exec_module(feed)

NOW = datetime.datetime(2026, 11, 12, 15, 0, tzinfo=datetime.timezone.utc).timestamp()
TODAY, YESTERDAY = '2026-11-12', '2026-11-11'
TS = int(NOW * 1000)


def event(**overrides):
    base = {
        'v': 1, 'ts': TS, 'room_id': 110, 'speaker': 'Rayo', 'kind': 'reply', 'target_kind': 'player',
        'target': None, 'player': 'a' * 64, 'text': '¡Hola! ¿Quieres café?', 'human_text': 'mi nombre es Ana',
        'source': 'model', 'priority': 'P0', 'scene_id': None, 'invite_id': None, 'reason': None,
        'dest_room_id': None,
    }
    base.update(overrides)
    return base


class Store:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = self.root / 'bots-dialog'
        self.dir.mkdir()
        self.switch = self.root / 'enabled'
        self.switch.write_text('true')

    def write(self, day, rows, raw=b''):
        with open(self.dir / f'{day}.jsonl', 'ab') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False).encode() + b'\n')
            handle.write(raw)

    def feed(self, clock=None):
        return feed.Feed(env={'DIALOG_DIR': str(self.dir), 'DIALOG_SWITCH': str(self.switch)},
                         now=clock or (lambda: NOW))

    def close(self):
        self._tmp.cleanup()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def body(store, limit=feed.MAX_EVENTS, clock=None):
    return json.loads(store.feed(clock).response(limit))


class PublicEventTest(unittest.TestCase):
    def test_only_allow_listed_keys_survive(self):
        result = feed.public_event(event(extra='x', player_id=42))
        self.assertEqual(tuple(result), feed.PUBLIC_FIELDS)
        for key in feed.FORBIDDEN_FIELDS + ('extra', 'priority', 'v'):
            self.assertNotIn(key, result)

    def test_a_player_target_is_never_named(self):
        self.assertIsNone(feed.public_event(event(target_kind='player', target='Ana'))['target'])
        self.assertEqual(feed.public_event(event(kind='scene', target_kind='bot', target='Nieve'))['target'], 'Nieve')
        self.assertIsNone(feed.public_event(event(kind='scene', target_kind='bot', target='<script>'))['target'])

    def test_invalid_core_fields_reject_the_event(self):
        for bad in ({'ts': '1'}, {'ts': 5}, {'room_id': 0}, {'room_id': '110'}, {'speaker': 'a b'},
                    {'speaker': 'x' * 13}, {'kind': 'whisper'}, {'target_kind': 'all'}, {'source': 'human'}):
            with self.subTest(bad=bad):
                self.assertIsNone(feed.public_event(event(**bad)))
        self.assertIsNone(feed.public_event(['not', 'a', 'dict']))

    def test_optional_fields_are_revalidated(self):
        result = feed.public_event(event(kind='decline', reason='made_up', scene_id='a/b', invite_id='ok-1',
                                         dest_room_id=-3))
        self.assertIsNone(result['reason'])
        self.assertIsNone(result['scene_id'])
        self.assertEqual(result['invite_id'], 'ok-1')
        self.assertIsNone(result['dest_room_id'])

    def test_text_loses_control_characters_and_is_bounded(self):
        self.assertEqual(feed.public_event(event(text='hola\x00\u202e  mundo\n'))['text'], 'hola mundo')
        self.assertEqual(len(feed.public_event(event(text='a' * 500))['text']), feed.TEXT_MAX)
        self.assertIsNone(feed.public_event(event(text=7))['text'])


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_reads_yesterday_and_today_oldest_first(self):
        self.store.write(TODAY, [event(ts=TS, text='hoy')])
        self.store.write(YESTERDAY, [event(ts=TS - 86_400_000, text='ayer')])
        self.store.write('2026-11-10', [event(ts=TS - 2 * 86_400_000, text='antier')])
        result = body(self.store)
        self.assertTrue(result['enabled'])
        self.assertEqual([e['text'] for e in result['events']], ['ayer', 'hoy'])
        self.assertEqual(result['count'], 2)

    def test_corrupt_lines_are_skipped_and_counted(self):
        self.store.write(TODAY, [event(text='uno'), event(kind='nope')], raw=b'{not json\n\xff\xfe\n[]\n')
        self.store.write(TODAY, [event(text='dos')])
        result = body(self.store)
        self.assertEqual([e['text'] for e in result['events']], ['uno', 'dos'])
        self.assertEqual(result['skipped'], 4)

    def test_a_torn_tail_is_not_an_event(self):
        self.store.write(TODAY, [event(text='uno')], raw=b'{"ts":')
        result = body(self.store)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['skipped'], 0)

    def test_switch_off_or_missing_empties_the_feed(self):
        self.store.write(TODAY, [event()])
        for value in ('false', 'TRUE ', 'yes', ''):
            with self.subTest(value=value):
                self.store.switch.write_text(value)
                result = body(self.store)
                self.assertEqual(result['enabled'], value.strip().lower() == 'true')
        self.store.switch.unlink()
        self.assertEqual(body(self.store), {'enabled': False, 'events': [], 'skipped': 0,
                                            'generatedAt': int(NOW), 'count': 0})

    def test_event_count_is_capped_and_limit_is_clamped(self):
        self.store.write(TODAY, [event(ts=TS + i, text=str(i)) for i in range(feed.MAX_EVENTS + 50)])
        result = body(self.store)
        self.assertEqual(result['count'], feed.MAX_EVENTS)
        self.assertEqual(result['events'][-1]['text'], str(feed.MAX_EVENTS + 49))
        self.assertEqual(body(self.store, limit=0)['events'], [])
        self.assertEqual(body(self.store, limit=3)['count'], 3)
        self.assertEqual(feed.parse_limit('limit=9999'), feed.MAX_EVENTS)
        self.assertEqual(feed.parse_limit('limit=-4'), 0)
        self.assertEqual(feed.parse_limit('limit=abc'), feed.MAX_EVENTS)

    def test_body_is_capped_in_bytes(self):
        payload = {'enabled': True, 'events': [{'text': 'x' * 1000} for _ in range(50)], 'count': 50}
        encoded = feed.encode(payload, body_max=10_000)
        self.assertLessEqual(len(encoded), 10_000)
        self.assertEqual(json.loads(encoded)['count'], len(json.loads(encoded)['events']))

    def test_only_a_bounded_tail_is_read(self):
        self.store.write(TODAY, [event(ts=TS + i, text='z' * 50) for i in range(4000)])
        lines = feed.read_tail(str(self.store.dir / f'{TODAY}.jsonl'), limit=4096)
        self.assertLess(sum(len(line) for line in lines), 4096)
        for line in lines:
            json.loads(line)   # the cut first line was dropped

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'needs O_NOFOLLOW')
    def test_symlinks_are_refused(self):
        outside = self.store.root / 'secret.jsonl'
        outside.write_text(json.dumps(event(text='fuera')) + '\n')
        os.symlink(outside, self.store.dir / f'{TODAY}.jsonl')
        self.assertEqual(body(self.store)['events'], [])
        linked = self.store.root / 'linked-dir'
        os.symlink(self.store.dir, linked)
        self.assertEqual(feed.collect(str(linked), NOW), ([], 0))

    def test_missing_directory_is_an_empty_feed(self):
        self.assertEqual(feed.collect(str(self.store.root / 'nope'), NOW), ([], 0))

    def test_reads_are_cached_briefly(self):
        clock = Clock()
        f = self.store.feed(clock)
        self.store.write(TODAY, [event(text='uno')])
        self.assertEqual(json.loads(f.response())['count'], 1)
        self.store.write(TODAY, [event(text='dos')])
        self.assertEqual(json.loads(f.response())['count'], 1)
        clock.t += feed.CACHE_SECONDS + 0.1
        self.assertEqual(json.loads(f.response())['count'], 2)


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.write(TODAY, [event()])
        feed.Handler.feed = self.store.feed()
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), feed.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def test_feed_health_and_unknown_paths(self):
        with urllib.request.urlopen(self.base + '/feed?limit=5') as r:
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
            self.assertEqual(r.headers['Content-Type'], 'application/json')
            self.assertEqual(json.loads(r.read())['count'], 1)
        with urllib.request.urlopen(self.base + '/healthz') as r:
            self.assertEqual(r.read(), b'ok')
        for path in ('/', '/feed/../etc', '/2026-11-12.jsonl', '/summary'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(caught.exception.code, 404)


class WriterContractTest(unittest.TestCase):
    def test_vocabularies_match_the_writer(self):
        sys.path.insert(0, str(ROOT / 'containers/bots'))
        from bots import dialog_log
        self.assertEqual(feed.KINDS, dialog_log.KINDS)
        self.assertEqual(feed.TARGET_KINDS, dialog_log.TARGET_KINDS)
        self.assertEqual(feed.SOURCES, dialog_log.SOURCES)
        self.assertEqual(feed.REASONS, dialog_log.REASONS)
        # Every public field is a writer field; the forbidden ones are exactly what is left out.
        self.assertLessEqual(set(feed.PUBLIC_FIELDS), set(dialog_log.FIELDS))
        self.assertEqual(set(dialog_log.FIELDS) - set(feed.PUBLIC_FIELDS), {'v', 'priority', 'player', 'human_text'})


class PrivacyContractTest(unittest.TestCase):
    """Fails the build if a forbidden key can reach the public JSON."""

    def test_forbidden_keys_never_reach_the_response(self):
        store = Store()
        try:
            store.write(TODAY, [
                event(), event(kind='greeting', human_text='x'), event(player_id=99, player='b' * 64),
                {**event(), 'human_text': {'nested': 'x'}, 'player': ['c']},
            ])
            raw = store.feed().response()
        finally:
            store.close()
        document = json.loads(raw)
        self.assertEqual(document['count'], 4)

        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys(value)
            elif isinstance(node, list):
                for value in node:
                    yield from keys(value)

        found = set(keys(document))
        for forbidden in feed.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, found)
        self.assertNotIn(b'mi nombre es Ana', raw)
        self.assertNotIn(b'a' * 64, raw)
        for e in document['events']:
            self.assertEqual(tuple(e), feed.PUBLIC_FIELDS)


if __name__ == '__main__':
    unittest.main()

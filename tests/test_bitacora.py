"""CLI over a fixture of JSONL day files, including a torn last line."""
import csv
import datetime
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'containers' / 'bots'))

import bitacora  # noqa: E402
from bots.dialog_log import FIELDS, REASONS, hash_player  # noqa: E402

UTC = datetime.timezone.utc
PLAYER = 8675309
SALT = 'cli-salt'
DIGEST = hash_player(PLAYER, SALT)
TORN_ID = '999001'
TORN_TEXT = 'torn-secret-phrase'


def _stamp(day, hour, minute=0):
    moment = datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def _event(**overrides):
    event = {
        'v': 1, 'ts': 0, 'room_id': 110, 'speaker': 'rayo', 'kind': 'reply',
        'target_kind': 'player', 'target': None, 'player': DIGEST,
        'text': None, 'human_text': None, 'source': 'local', 'priority': None,
        'scene_id': None, 'invite_id': None, 'reason': None, 'dest_room_id': None,
    }
    event.update(overrides)
    return event


class BitacoraCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        day = datetime.date(2026, 11, 11)
        earlier = datetime.date(2026, 11, 1)
        base = _stamp(day, 12)
        rows = [
            _event(ts=base, text='hola plaza', human_text='oye rayo', source='model', priority='P0'),
            _event(ts=base + 60000, text='hola plaza', source='local', priority='P0'),
            _event(ts=base + 700000, text='otra cosa', source='fallback', priority='P0'),
            _event(ts=base + 800000, speaker='rayo', kind='scene', target_kind='bot', target='lolaritmo',
                   player=None, text='te sigo', source='local', priority='P2', scene_id='sc1'),
            _event(ts=base + 801000, speaker='lolaritmo', kind='scene', target_kind='bot', target='rayo',
                   player=None, text='voy', source='local', priority='P2', scene_id='sc1'),
            _event(ts=base + 900000, kind='invite', target_kind='bot', target='lolaritmo', player=None,
                   invite_id='inv1', dest_room_id=220),
            _event(ts=base + 901000, speaker='lolaritmo', kind='accept', target_kind='bot', target='rayo',
                   player=None, invite_id='inv1', reason='relation_friend', dest_room_id=220),
            _event(ts=base + 902000, speaker='lolaritmo', kind='travel', target_kind='room', player=None,
                   invite_id='inv1', reason='relation_friend', dest_room_id=220),
            _event(ts=base + 903000, kind='decline', target_kind='bot', target='lolaritmo', player=None,
                   invite_id='inv2', reason='p2_cutoff', dest_room_id=220),
            _event(ts=base + 904000, kind='travel', target_kind='room', player=None,
                   invite_id='inv3', reason='restart', dest_room_id=100),
            _event(ts=base + 905000, speaker='lolaritmo', kind='accept', target_kind='bot', target='rayo',
                   player=None, invite_id='inv4', reason='seeded_roll', dest_room_id=330),
            _event(ts=base + 906000, room_id=100, speaker='nube', kind='greeting', text='hola plaza',
                   source='local', priority='P1', player=DIGEST),
        ]
        body = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
        torn = '{"v":1,"player_id":"%s","text":"%s"' % (TORN_ID, TORN_TEXT)
        (self.root / '2026-11-11.jsonl').write_text(body + torn, encoding='utf-8')
        early = _event(ts=_stamp(earlier, 9), room_id=300, speaker='pixel', text='lejos', player=None)
        (self.root / '2026-11-01.jsonl').write_text(json.dumps(early) + '\n', encoding='utf-8')
        (self.root / 'notes.txt').write_text('not a log\n', encoding='utf-8')
        self.day = day
        self.base = base

    def test_vocabulary_matches_the_service(self):
        self.assertEqual(bitacora.FIELDS, FIELDS)
        self.assertEqual(bitacora.REASONS, REASONS)

    def test_torn_line_is_ignored(self):
        events = bitacora.load_events(self.root)
        blob = json.dumps(events)
        self.assertNotIn(TORN_ID, blob)
        self.assertNotIn(TORN_TEXT, blob)
        self.assertNotIn('player_id', blob)
        self.assertTrue(any(event['text'] == 'lejos' for event in events))
        self.assertTrue(any(event['text'] == 'hola plaza' for event in events))

    def test_filter_by_room_penguin_and_dates(self):
        events = bitacora.load_events(
            self.root, room=110, penguin='Rayo', since=self.day, until=self.day)
        self.assertTrue(events)
        self.assertTrue(all(event['room_id'] == 110 and event['speaker'] == 'rayo' for event in events))
        self.assertFalse(any(event['speaker'] == 'pixel' for event in events))
        self.assertFalse(any(event['speaker'] == 'nube' for event in events))
        output = io.StringIO()
        with redirect_stdout(output):
            code = bitacora.main([
                '--dir', str(self.root), 'filter', '--room', '110', '--penguin', 'rayo',
                '--since', '2026-11-11', '--until', '2026-11-11'])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn('hola plaza', text)
        self.assertNotIn('lejos', text)
        self.assertNotIn(TORN_TEXT, text)
        self.assertNotIn(str(PLAYER), text)
        self.assertEqual(bitacora.main(['--dir', str(self.root), 'filter', '--since', '11-11-2026']), 2)
        self.assertEqual(bitacora.main(['--dir', str(self.root / 'missing'), 'filter']), 2)

    def test_conversations_group_scenes_invites_and_gaps(self):
        events = bitacora.load_events(self.root, since=self.day, until=self.day)
        groups = bitacora.conversations(events)
        scene = next(group for group in groups if group['kind'] == 'scene')
        self.assertEqual(scene['id'], 'sc1')
        self.assertEqual([event['text'] for event in scene['events']], ['te sigo', 'voy'])
        invite = next(group for group in groups if group['kind'] == 'invite' and group['id'] == 'inv1')
        self.assertEqual([event['kind'] for event in invite['events']], ['invite', 'accept', 'travel'])
        chats = [group for group in groups
                 if group['kind'] == 'chat' and group['room_id'] == 110 and group['id'] == DIGEST]
        self.assertEqual(len(chats), 2)
        self.assertEqual([event['text'] for event in chats[0]['events']], ['hola plaza', 'hola plaza'])
        self.assertEqual([event['text'] for event in chats[1]['events']], ['otra cosa'])
        rendered = bitacora.render_conversations(groups)
        self.assertIn('oye rayo', rendered)
        self.assertNotIn(TORN_TEXT, rendered)
        self.assertNotIn(str(PLAYER), rendered)

    def test_matrix_counts_who_talks_to_whom(self):
        counts = bitacora.matrix(bitacora.load_events(self.root, since=self.day, until=self.day))
        self.assertEqual(counts[('rayo', 'player:' + DIGEST)], 3)
        self.assertEqual(counts[('rayo', 'lolaritmo')], 3)
        self.assertEqual(counts[('lolaritmo', 'rayo')], 3)
        self.assertEqual(counts[('lolaritmo', 'room:220')], 1)
        rendered = bitacora.render_matrix(counts)
        self.assertIn('player:' + DIGEST, rendered)
        self.assertNotIn(str(PLAYER), rendered)

    def test_invitations_and_reason_breakdown(self):
        events = bitacora.load_events(self.root, since=self.day, until=self.day)
        rows = {row['invite_id']: row for row in bitacora.invitations(events)}
        self.assertEqual(rows['inv1']['made'], 1)
        self.assertEqual(rows['inv1']['accepted'], 1)
        self.assertEqual(rows['inv1']['declined'], 0)
        self.assertEqual(rows['inv1']['travels'], 1)
        self.assertEqual(rows['inv1']['reasons'], [('accept', 'relation_friend'), ('travel', 'relation_friend')])
        self.assertEqual(rows['inv2']['declined'], 1)
        self.assertIn(('decline', 'p2_cutoff'), rows['inv2']['reasons'])
        self.assertIn(('travel', 'restart'), rows['inv3']['reasons'])
        self.assertIn(('accept', 'seeded_roll'), rows['inv4']['reasons'])
        breakdown = bitacora.reason_breakdown(events)
        self.assertEqual(breakdown['relation_friend'], 2)
        self.assertEqual(breakdown['p2_cutoff'], 1)
        self.assertEqual(breakdown['restart'], 1)
        self.assertEqual(breakdown['seeded_roll'], 1)
        self.assertEqual(breakdown['lease_expired'], 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(bitacora.main(['--dir', str(self.root), 'invites', '--since', '2026-11-11']), 0)
        text = output.getvalue()
        for reason in ('relation_friend', 'p2_cutoff', 'restart', 'seeded_roll'):
            self.assertIn(reason, text)
        self.assertNotIn(TORN_ID, text)

    def test_source_ratio(self):
        events = bitacora.load_events(self.root, since=self.day, until=self.day)
        spoken = bitacora.source_ratio(events, bitacora.SPOKEN)
        # replies: model, local, fallback; scenes: local, local; greeting: local. Spoken texts:
        # hola plaza model, hola plaza local, otra cosa fallback, te sigo local, voy local, hola plaza greeting local.
        self.assertEqual(spoken['counts'], {'model': 1, 'local': 4, 'fallback': 1})
        self.assertAlmostEqual(spoken['ratio']['model'], 1 / 6)
        everything = bitacora.source_ratio(events)
        self.assertEqual(everything['total'], 12)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(bitacora.main(['--dir', str(self.root), 'sources', '--since', '2026-11-11']), 0)
        self.assertIn('model\t1\t0.167', output.getvalue())

    def test_repeated_phrases(self):
        ranked = bitacora.repeated_phrases(
            bitacora.load_events(self.root, since=self.day, until=self.day), top=2)
        self.assertEqual(ranked[0], ('hola plaza', 3))
        self.assertEqual(len(ranked), 2)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                bitacora.main(['--dir', str(self.root), 'phrases', '--top', '1', '--since', '2026-11-11']),
                0)
        self.assertIn('3\thola plaza', output.getvalue())
        self.assertNotIn('oye rayo', output.getvalue())
        self.assertNotIn(TORN_TEXT, output.getvalue())

    def test_export_csv_and_jsonl(self):
        csv_path = self.root / 'out.csv'
        jsonl_path = self.root / 'out.jsonl'
        with redirect_stdout(io.StringIO()):
            code = bitacora.main([
                '--dir', str(self.root), 'export', '--format', 'csv', '--output', str(csv_path),
                '--since', '2026-11-11', '--room', '110', '--penguin', 'rayo'])
        self.assertEqual(code, 0)
        with csv_path.open(encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, list(FIELDS))
            rows = list(reader)
        self.assertTrue(rows)
        self.assertTrue(all(row['speaker'] == 'rayo' and row['room_id'] == '110' for row in rows))
        blob = csv_path.read_text(encoding='utf-8')
        self.assertNotIn(TORN_TEXT, blob)
        self.assertNotIn(str(PLAYER), blob.replace(DIGEST, ''))
        with redirect_stdout(io.StringIO()):
            code = bitacora.main([
                '--dir', str(self.root), 'export', '--format', 'jsonl', '--output', str(jsonl_path),
                '--since', '2026-11-11'])
        self.assertEqual(code, 0)
        exported = [json.loads(line) for line in jsonl_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(exported), 12)
        self.assertTrue(all(set(row) == set(FIELDS) for row in exported))
        self.assertNotIn(TORN_TEXT, jsonl_path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

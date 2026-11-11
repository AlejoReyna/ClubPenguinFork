"""Dialog log: contract v1, privacy, switch, retention, metrics. No network."""
import ast
import asyncio
import datetime
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from bots import dialog_log
from bots.config import ConfigError, build_config
from bots.dialog_log import (
    FIELDS, KINDS, REASONS, DialogLog, emit, hash_player, install,
)
from bots.manager import BotManager
from bots.metrics import MetricsServer, Registry
from support import config_document_world

UTC = datetime.timezone.utc
DAY = datetime.datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
TS = int(DAY.timestamp() * 1000)
PLAYER = 4242424242
PHRASE = 'fraseunicaqwerty'
SECRET_HUMAN = 'SECRETO-HUMANO'


def _until(predicate, timeout=2.0):
    async def wait():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError('timed out')
    return wait()


def _lines(directory):
    files = sorted(Path(directory).glob('*.jsonl'))
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text().splitlines() if line]


class DialogLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: install(None))

    def _log(self, enabled='true', salt='test-salt', **kwargs):
        switch = self.root / 'enabled'
        switch.write_text(enabled + '\n')
        kwargs.setdefault('flush_seconds', 0.05)
        kwargs.setdefault('switch_seconds', 0.05)
        kwargs.setdefault('batch_size', 8)
        kwargs.setdefault('retention_interval', 10 ** 9)
        kwargs.setdefault('error_backoff', 0.05)
        kwargs.setdefault('error_backoff_max', 0.05)
        log = DialogLog(self.root / 'days', switch, salt, **kwargs)
        install(log)
        return log

    def _conversation(self, player_id=PLAYER):
        """Test-only caller. Vida owns the real emit sites in director/travel/life."""
        emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='model',
             player_id=player_id, text='¿Vamos a la plaza?', human_text='oye rayo vamos',
             priority='P0', ts=TS)
        emit(room_id=110, speaker='rayo', kind='scene', target_kind='bot', target='lolaritmo',
             source='local', text='Te sigo', scene_id='sc1', priority='P2', ts=TS + 1000)
        emit(room_id=110, speaker='lolaritmo', kind='scene', target_kind='bot', target='rayo',
             source='local', text='Voy', scene_id='sc1', priority='P2', ts=TS + 2000)
        emit(room_id=110, speaker='rayo', kind='invite', target_kind='bot', target='lolaritmo',
             source='local', invite_id='inv1', dest_room_id=220, ts=TS + 3000)
        emit(room_id=110, speaker='lolaritmo', kind='accept', target_kind='bot', target='rayo',
             source='local', invite_id='inv1', reason='relation_friend', dest_room_id=220,
             ts=TS + 4000)
        emit(room_id=110, speaker='lolaritmo', kind='travel', target_kind='room', source='local',
             invite_id='inv1', reason='relation_friend', dest_room_id=220, player_id=player_id,
             human_text=SECRET_HUMAN, ts=TS + 5000)
        emit(room_id=110, speaker='rayo', kind='decline', target_kind='bot', target='lolaritmo',
             source='local', invite_id='inv2', reason='p2_cutoff', dest_room_id=220, ts=TS + 6000)
        emit(room_id=110, speaker='rayo', kind='travel', target_kind='room', source='fallback',
             invite_id='inv3', reason='restart', dest_room_id=100, ts=TS + 7000)
        emit(room_id=110, speaker='lolaritmo', kind='accept', target_kind='bot', target='rayo',
             source='local', invite_id='inv4', reason='seeded_roll', dest_room_id=330, ts=TS + 8000)
        emit(room_id=100, speaker='nube', kind='greeting', target_kind='player', source='local',
             player_id=player_id, text='Hola', priority='P1', ts=TS + 9000)

    async def test_conversation_lands_with_every_field_and_no_raw_player(self):
        log = self._log()
        task = asyncio.create_task(log.run())
        try:
            self._conversation()
            directory = self.root / 'days'
            await _until(lambda: len(_lines(directory)) >= 10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        lines = _lines(self.root / 'days')
        self.assertEqual(len(lines), 10)
        reply = lines[0]
        self.assertEqual(set(reply), set(FIELDS))
        digest = hash_player(PLAYER, 'test-salt')
        self.assertEqual(reply, {
            'v': 1, 'ts': TS, 'room_id': 110, 'speaker': 'rayo', 'kind': 'reply',
            'target_kind': 'player', 'target': None, 'player': digest,
            'text': '¿Vamos a la plaza?', 'human_text': 'oye rayo vamos',
            'source': 'model', 'priority': 'P0', 'scene_id': None, 'invite_id': None,
            'reason': None, 'dest_room_id': None,
        })
        travel = lines[5]
        self.assertEqual(travel['kind'], 'travel')
        self.assertEqual(travel['invite_id'], 'inv1')
        self.assertEqual(travel['reason'], 'relation_friend')
        self.assertEqual(travel['dest_room_id'], 220)
        self.assertIsNone(travel['human_text'])
        self.assertEqual(travel['player'], digest)
        reasons = {line['reason'] for line in lines}
        self.assertTrue({'relation_friend', 'p2_cutoff', 'restart', 'seeded_roll'} <= reasons)
        raw = (self.root / 'days').joinpath('2026-11-11.jsonl').read_text().replace(digest, '')
        self.assertNotIn(str(PLAYER), raw)
        self.assertNotIn('player_id', raw)
        self.assertNotIn(SECRET_HUMAN, raw)
        self.assertNotIn('test-salt', raw)
        info = (self.root / 'days' / '2026-11-11.jsonl').stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE((self.root / 'days').stat().st_mode), 0o700)

    async def test_switch_off_writes_nothing_and_hot_reload_toggles(self):
        log = self._log(enabled='false', batch_size=1)
        task = asyncio.create_task(log.run())
        try:
            self.assertFalse(log.enabled)
            await _until(lambda: log._last_retain is not None)
            emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
                 player_id=PLAYER, text='no-deberia', human_text='nada', ts=TS)
            await asyncio.sleep(0.2)
            self.assertFalse((self.root / 'days').exists())
            self.assertEqual(log.dropped_full, 0)
            (self.root / 'enabled').write_text('true\n')
            await _until(lambda: log.enabled)
            emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
                 player_id=PLAYER, text='ahora-si', human_text='oye', ts=TS)
            await _until(lambda: (self.root / 'days' / '2026-11-11.jsonl').exists())
            await _until(lambda: 'ahora-si' in (self.root / 'days' / '2026-11-11.jsonl').read_text())
            before = (self.root / 'days' / '2026-11-11.jsonl').read_text()
            (self.root / 'enabled').write_text('false\n')
            await _until(lambda: not log.enabled)
            emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
                 text='NO-TRAS-APAGAR', ts=TS)
            await asyncio.sleep(0.25)
            self.assertEqual((self.root / 'days' / '2026-11-11.jsonl').read_text(), before)
            self.assertNotIn('NO-TRAS-APAGAR', before)
            self.assertEqual(log.dropped_full, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unwritable_directory_does_not_block_and_counts_an_error(self):
        blocker = self.root / 'not-a-directory'
        blocker.write_text('x')
        switch = self.root / 'enabled'
        switch.write_text('true\n')
        log = DialogLog(blocker, switch, 'test-salt', flush_seconds=0.01, switch_seconds=0.05,
                        batch_size=1, retention_interval=10 ** 9,
                        error_backoff=0.05, error_backoff_max=0.05)
        install(log)
        task = asyncio.create_task(log.run())
        try:
            started = time.monotonic()
            for _ in range(80):
                emit(room_id=110, speaker='rayo', kind='reply', target_kind='player',
                     source='local', text='sigue', ts=TS)
            self.assertLess(time.monotonic() - started, 0.3)
            ran = []

            async def other():
                ran.append(True)
                await asyncio.sleep(0)

            await asyncio.wait_for(other(), 0.5)
            self.assertTrue(ran)
            await _until(lambda: log.errors['io'] > 0)
            emit(room_id=110, speaker='rayo', kind='reply', target_kind='player',
                 source='local', text='todavia', ts=TS)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_raising_writer_does_not_block_the_loop(self):
        def boom(_day, _payload):
            raise OSError('disk full')

        log = self._log(write_batch=boom, batch_size=1, flush_seconds=0.01)
        task = asyncio.create_task(log.run())
        try:
            started = time.monotonic()
            for _ in range(40):
                emit(room_id=110, speaker='rayo', kind='reply', target_kind='player',
                     source='local', text='sigue', ts=TS)
            self.assertLess(time.monotonic() - started, 0.3)
            marker = asyncio.Event()

            async def other():
                marker.set()

            await asyncio.wait_for(other(), 0.5)
            self.assertTrue(marker.is_set())
            await _until(lambda: log.errors['io'] > 0)
            self.assertFalse((self.root / 'days').exists())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_full_queue_drops_and_disabled_does_not(self):
        log = self._log(queue_size=2, batch_size=50, flush_seconds=30)
        # Hold the events in the queue: do not start the writer.
        for index in range(3):
            emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
                 text=f'linea{index}', ts=TS)
        self.assertEqual(log.dropped_full, 1)
        self.assertEqual(log.depth(), 2)
        (self.root / 'enabled').write_text('false\n')
        log.reload_switch()
        emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
             text='apagado', ts=TS)
        self.assertEqual(log.dropped_full, 1)
        self.assertEqual(log.depth(), 2)
        registry = Registry()
        log.register(registry)
        rendered = registry.render()
        self.assertIn('bots_dialog_log_dropped_total{reason="queue_full"} 1', rendered)
        self.assertNotIn('linea0', rendered)
        self.assertNotIn('apagado', rendered)

    def test_retention_deletes_only_old_day_files(self):
        directory = self.root / 'days'
        directory.mkdir()
        def clock():
            return datetime.datetime(2026, 11, 11, tzinfo=UTC).timestamp()

        log = DialogLog(directory, self.root / 'missing-switch', 'salt', clock=clock)
        self.assertFalse(log.enabled)

        def touch(name, body=b'x\n'):
            path = directory / name
            path.write_bytes(body)
            return path

        old = touch('2026-10-11.jsonl')
        edge = touch('2026-10-12.jsonl')
        today = touch('2026-11-11.jsonl')
        notes = touch('notes.txt', b'keep me')
        not_jsonl = touch('2026-10-01.json', b'{}')
        invalid_day = touch('2026-13-40.jsonl', b'nope')
        sentinel = self.root / 'sentinel'
        sentinel.write_text('SAFE')
        link = directory / '2020-01-01.jsonl'
        link.symlink_to(sentinel)
        deleted = log.retain()
        self.assertFalse(old.exists())
        self.assertTrue(edge.exists())
        self.assertTrue(today.exists())
        self.assertEqual(notes.read_text(), 'keep me')
        self.assertTrue(not_jsonl.exists())
        self.assertTrue(invalid_day.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(sentinel.read_text(), 'SAFE')
        self.assertEqual(deleted, 1)
        self.assertEqual(log.retention_deleted, 1)

    async def test_symlink_day_file_is_not_followed(self):
        log = self._log(batch_size=1)
        directory = self.root / 'days'
        directory.mkdir()
        sentinel = self.root / 'sentinel'
        sentinel.write_text('SAFE')
        (directory / '2026-11-11.jsonl').symlink_to(sentinel)
        task = asyncio.create_task(log.run())
        try:
            emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
                 text='no-siga', ts=TS)
            await _until(lambda: log.errors['symlink'] > 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sentinel.read_text(), 'SAFE')
        self.assertTrue((directory / '2026-11-11.jsonl').is_symlink())

    def test_torn_tail_is_split_before_the_next_line(self):
        log = self._log()
        directory = self.root / 'days'
        directory.mkdir(mode=0o700)
        path = directory / '2026-11-11.jsonl'
        path.write_bytes(b'{"v":1,"ok":true}\n{"torn":')
        os.chmod(path, 0o600)
        emit(room_id=110, speaker='rayo', kind='reply', target_kind='player', source='local',
             text='completa', ts=TS)
        log._drain()
        log._flush()
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        parts = raw.splitlines()
        self.assertEqual(json.loads(parts[0])['ok'], True)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(parts[1])
        self.assertEqual(json.loads(parts[2])['text'], 'completa')

    def test_hash_changes_with_salt_and_id_and_never_returns_the_id(self):
        first = hash_player(42, 'abc')
        self.assertEqual(first, hash_player(42, 'abc'))
        self.assertNotEqual(first, hash_player(43, 'abc'))
        self.assertNotEqual(first, hash_player(42, 'abd'))
        self.assertIsNone(hash_player(42, None))
        self.assertIsNone(hash_player(None, 'abc'))
        self.assertEqual(len(first), 64)
        self.assertTrue(first.isalnum())
        with self.assertRaises(ValueError):
            hash_player(True, 'abc')
        with self.assertRaises(ValueError):
            hash_player('42', 'abc')

    def test_invalid_events_are_counted_and_emit_never_raises(self):
        install(None)
        emit()
        emit(1, 'rayo')
        log = self._log()
        emit()  # enabled, but not a valid call
        self.assertEqual(log.errors['invalid'], 1)
        emit(room_id=110, speaker='rayo', kind='decline', target_kind='bot', target='lolaritmo',
             source='local', reason='because i said so', ts=TS)
        self.assertEqual(log.errors['invalid'], 2)
        self.assertEqual(log.depth(), 0)
        (self.root / 'enabled').write_text('nope\n')
        log.reload_switch()
        self.assertFalse(log.enabled)
        emit(room_id=110, speaker='rayo', kind='decline', target_kind='bot', target='lolaritmo',
             source='local', reason='not-a-code', ts=TS)
        self.assertEqual(log.errors['invalid'], 2)
        self.assertEqual(log.dropped_full, 0)
        self.assertNotIn('test-salt', repr(log))

    def test_new_reason_codes_are_accepted(self):
        log = self._log()
        for reason in ('restart', 'p2_cutoff', 'seeded_roll'):
            emit(room_id=110, speaker='rayo', kind='travel', target_kind='room', source='local',
                 reason=reason, dest_room_id=100, ts=TS)
        self.assertEqual(log.errors['invalid'], 0)
        self.assertEqual(log.depth(), 3)
        self.assertEqual(REASONS[-3:], ('restart', 'p2_cutoff', 'seeded_roll'))

    def test_metrics_labels_carry_no_chat_text(self):
        log = self._log(salt='sal-secreto-xyz')
        registry = Registry()
        log.register(registry)
        emit(room_id=110, speaker='zqxbot', kind='reply', target_kind='player', source='model',
             player_id=PLAYER, text=PHRASE, human_text=PHRASE, priority='P0', ts=TS)
        log._drain()
        log._flush()
        rendered = registry.render()
        digest = hash_player(PLAYER, 'sal-secreto-xyz')
        self.assertNotIn(PHRASE, rendered)
        self.assertNotIn(str(PLAYER), rendered)
        self.assertNotIn(digest, rendered)
        self.assertNotIn('zqxbot', rendered)
        self.assertNotIn('sal-secreto-xyz', rendered)
        self.assertIn('bots_dialog_log_events_total{kind="reply"} 1', rendered)
        self.assertIn('bots_dialog_log_enabled 1', rendered)
        self.assertIn('bots_dialog_log_salt_configured 1', rendered)
        self.assertRegex(rendered, r'(?m)^bots_dialog_log_bytes_total [1-9]')
        for line in rendered.splitlines():
            if not line.startswith('bots_dialog_log_') or '{' not in line:
                continue
            body = line.split('{', 1)[1].split('}', 1)[0]
            for part in body.split(','):
                value = part.split('=', 1)[1].strip().strip('"')
                self.assertRegex(value, r'^[a-z0-9_]+$')
        kinds_in_help = [kind for kind in KINDS if f'kind="{kind}"' in rendered]
        self.assertEqual(kinds_in_help, list(KINDS))

    def test_service_exposes_no_read_path(self):
        banned = {
            'read', 'read_events', 'iter_events', 'load', 'load_events', 'query',
            'query_log', 'search', 'tail', 'export', 'fetch', 'cat',
        }
        source_path = Path(dialog_log.__file__)
        tree = ast.parse(source_path.read_text())
        names = {node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertFalse(names & banned)
        self.assertFalse(any('read' in name or 'query' in name for name in dialog_log.__all__))
        server = MetricsServer(Registry(), -1)
        for path in ('/dialog', '/dialog-log', '/var/lib/bots-dialog', '/bitacora'):
            _body, status = server._body(path)
            self.assertIn('404', status)
        self.assertNotIn('start_server', source_path.read_text())

    def test_salt_comes_from_the_environment_only(self):
        document = config_document_world(1)
        document['dialog_salt'] = 'from-json'
        document['provider']['salt'] = 'also-json'
        env = {
            'BOTS_DIALOG_LOG_SALT': 'from-env\n',
            'BOTS_DIALOG_LOG_DIR': '/var/lib/bots-dialog',
            'BOTS_DIALOG_LOG_SWITCH': '/etc/bots-dialog/enabled',
        }
        config = build_config(document, env)
        self.assertEqual(config.dialog_log.salt, 'from-env')
        self.assertNotIn('from-env', repr(config.dialog_log))
        self.assertIsNone(build_config(document, {}).dialog_log.salt)
        with self.assertRaises(ConfigError):
            build_config(document, {'BOTS_DIALOG_LOG_SALT': 'x' * 257})

    async def test_manager_wires_the_writer_task(self):
        switch = self.root / 'enabled'
        switch.write_text('true\n')
        logs = self.root / 'days'
        document = config_document_world(1)
        document['bots'] = document['bots'][:1]
        document['service'] = {'startup_stagger_min': 0, 'startup_stagger_max': 0}
        env = {
            'BOTS_DIALOG_LOG_DIR': str(logs),
            'BOTS_DIALOG_LOG_SWITCH': str(switch),
            'BOTS_DIALOG_LOG_SALT': 'manager-salt',
        }
        manager = BotManager(build_config(document, env), Registry())
        self.assertIs(dialog_log._active, manager.dialog_log)
        manager.dialog_log.flush_seconds = 0.05
        manager.dialog_log.switch_seconds = 0.05
        manager.dialog_log.batch_size = 1

        async def fake_session(_session, stop_event):
            await stop_event.wait()

        manager._run_session = fake_session
        stop = asyncio.Event()
        task = asyncio.create_task(manager.run(stop))
        try:
            emit(room_id=100, speaker='guiaia', kind='greeting', target_kind='player',
                 source='local', player_id=7, text='hola', ts=TS)
            await _until(lambda: any(logs.glob('*.jsonl')) and 'hola' in next(logs.glob('*.jsonl')).read_text())
            body = next(logs.glob('*.jsonl')).read_text().replace(hash_player(7, 'manager-salt'), '')
            self.assertNotIn('"7"', body)
            self.assertIn('bots_dialog_log_enabled', manager.registry.render())
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


if __name__ == '__main__':
    unittest.main()

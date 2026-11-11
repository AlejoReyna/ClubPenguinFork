"""Append-only dialog log (docs/bots.md, event contract v1).

``emit`` is synchronous, does no I/O, and never raises. It hashes ``player_id``
with HMAC-SHA256 before enqueueing and drops the raw id. A background task
batches lines into one JSONL file per UTC day (``YYYY-MM-DD.jsonl``).

The kill switch is a file (a ConfigMap key in the cluster), re-read about
every 30 seconds. Off is the default: nothing is written and no day file is
created. The switch being off is not a dropped event. A full queue is.

This module has no reader. Analysis lives in ``scripts/bitacora.py``, which is
run on a copy of the files, not inside the service.
"""
import asyncio
import datetime
import errno
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import time
from pathlib import Path

from . import metrics as metrics_mod
from . import text as text_mod

logger = logging.getLogger('bots.dialog_log')

# Closed vocabulary. Extending ``reason`` requires a docs/bots.md change.
KINDS = ('reply', 'greeting', 'scene', 'invite', 'accept', 'decline', 'travel')
TARGET_KINDS = ('player', 'bot', 'room')
SOURCES = ('model', 'local', 'fallback')
PRIORITIES = ('P0', 'P1', 'P2')
REASONS = (
    'relation_friend',
    'busy_with_human',
    'recent_decline',
    'low_energy',
    'off_schedule',
    'lease_expired',
    'room_min_guard',
    'restart',
    'p2_cutoff',
    'seeded_roll',
)
REASON_KINDS = ('accept', 'decline', 'travel')
FIELDS = (
    'v', 'ts', 'room_id', 'speaker', 'kind', 'target_kind', 'target', 'player',
    'text', 'human_text', 'source', 'priority', 'scene_id', 'invite_id',
    'reason', 'dest_room_id',
)
ERROR_REASONS = ('io', 'invalid', 'symlink')

QUEUE_MAX = 2048
BATCH_MAX = 32
FLUSH_SECONDS = 30.0
SWITCH_SECONDS = 30.0
RETENTION_DAYS = 30
RETENTION_INTERVAL = 6 * 3600.0
_TEXT_LIMIT = 54
_TS_MIN = 946684800000    # 2000-01-01 UTC
_TS_MAX = 4102444800000   # 2100-01-01 UTC

_USER = re.compile(r'^[A-Za-z0-9]{1,12}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_DAY_FILE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.jsonl$')

__all__ = (
    'BATCH_MAX', 'ERROR_REASONS', 'FIELDS', 'FLUSH_SECONDS', 'KINDS',
    'PRIORITIES', 'QUEUE_MAX', 'REASONS', 'RETENTION_DAYS', 'SOURCES',
    'SWITCH_SECONDS', 'TARGET_KINDS', 'DialogLog', 'emit', 'hash_player',
    'install',
)

_active = None


class _StoreError(OSError):
    """A disk failure with a bounded metric reason (``io`` or ``symlink``)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason if reason in ('io', 'symlink') else 'io'


def install(log):
    """Register the process log ``emit`` writes to. ``None`` disables it."""
    global _active
    _active = log


def hash_player(player_id, salt):
    """Hex HMAC-SHA256 of a positive int id. ``None`` when there is no salt.

    The raw id is not part of the return value. An unusable id raises
    ``ValueError`` so the caller can drop the event instead of storing it.
    """
    if player_id is None:
        return None
    if type(player_id) is not int or player_id <= 0:
        raise ValueError('player_id')
    if not salt:
        return None
    digest = hmac.new(str(salt).encode('utf-8'), str(player_id).encode('utf-8'), hashlib.sha256)
    return digest.hexdigest()


def emit(*args, **kwargs):
    """Queue one contract-v1 event. Never raises and never touches the disk.

    Required keywords: ``room_id``, ``speaker``, ``kind``, ``target_kind``,
    ``source``. Optional: ``target``, ``player_id``, ``text``, ``human_text``,
    ``priority``, ``scene_id``, ``invite_id``, ``reason``, ``dest_room_id``,
    ``ts`` (UTC epoch milliseconds).

    ``player_id`` is hashed before it is queued and is not retained. When the
    switch is off this returns immediately and does not count a drop.
    """
    try:
        log = _active
        if log is None or not log.enabled:
            return
        if args:
            log.errors['invalid'] += 1
            return
        log.enqueue(**kwargs)
    except Exception:
        log = _active
        if log is not None and log.enabled:
            try:
                log.errors['invalid'] += 1
            except Exception:
                return


def _day(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')


def _username(value):
    if isinstance(value, str) and _USER.match(value):
        return value
    return None


def _token(value):
    if value is None:
        return None
    if isinstance(value, str) and _TOKEN.match(value):
        return value
    return False


def _room(value):
    if value is None:
        return None
    if type(value) is int and value > 0:
        return value
    return False


def _line(value):
    """Chat-safe line, or ``None`` when empty. ``False`` when the value is unusable."""
    if value is None:
        return None
    if not isinstance(value, str):
        return False
    cleaned = text_mod.sanitize(value, _TEXT_LIMIT)
    return cleaned or None


class DialogLog:
    """In-memory queue plus a single writer task. Safe to construct while off."""

    def __init__(self, directory, switch_path, salt, *,
                 queue_size=QUEUE_MAX, batch_size=BATCH_MAX,
                 flush_seconds=FLUSH_SECONDS, switch_seconds=SWITCH_SECONDS,
                 retention_days=RETENTION_DAYS, retention_interval=RETENTION_INTERVAL,
                 clock=None, monotonic=None, write_batch=None,
                 error_backoff=0.5, error_backoff_max=30.0):
        if queue_size < 1 or batch_size < 1:
            raise ValueError('dialog log queue bounds must be positive')
        self.directory = str(directory)
        self.switch_path = str(switch_path) if switch_path else ''
        self.salt = str(salt) if salt else None
        self.queue_size = int(queue_size)
        self.batch_size = int(batch_size)
        self.flush_seconds = float(flush_seconds)
        self.switch_seconds = float(switch_seconds)
        self.retention_days = int(retention_days)
        self.retention_interval = float(retention_interval)
        self.clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._write_batch = write_batch
        self._backoff_initial = float(error_backoff)
        self._backoff_max = float(error_backoff_max)
        self._backoff = self._backoff_initial
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._pending = []
        self._pending_since = None
        self._stopping = False
        self._registered = False
        self._last_retain = None
        self.enabled = False
        self.dropped_full = 0
        self.bytes_written = 0
        self.retention_deleted = 0
        self.written = {kind: 0 for kind in KINDS}
        self.errors = {reason: 0 for reason in ERROR_REASONS}
        self.reload_switch()

    def __repr__(self):
        salt = 'set' if self.salt else 'missing'
        return (f'DialogLog(directory={self.directory!r}, enabled={self.enabled}, salt={salt})')

    # ---------------------------------------------------------------- switch
    def reload_switch(self):
        """Re-read the kill switch. Only the exact word ``true`` turns it on.

        A missing or unreadable file is off. The cluster mount is a symlink
        farm (ConfigMap ``..data``), so this read follows links. Day files
        never follow symlinks.
        """
        enabled = False
        path = self.switch_path
        if path:
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                fd = None
            if fd is not None:
                try:
                    raw = os.read(fd, 64)
                except OSError:
                    raw = b''
                finally:
                    os.close(fd)
                try:
                    enabled = raw.decode('utf-8').strip().lower() == 'true'
                except UnicodeError:
                    enabled = False
        self.enabled = enabled
        return enabled

    # ------------------------------------------------------------------ emit
    def enqueue(self, **kwargs):
        """Validate, hash, and ``put_nowait``. Counts invalid events and a full queue."""
        item = self._item(**kwargs)
        if item is None:
            self.errors['invalid'] += 1
            return
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped_full += 1

    def _item(self, *, room_id, speaker, kind, target_kind, source,
              target=None, player_id=None, text=None, human_text=None,
              priority=None, scene_id=None, invite_id=None, reason=None,
              dest_room_id=None, ts=None):
        """One ``(day, kind, line)`` tuple, or ``None``. No raw player id."""
        room_id = _room(room_id)
        speaker = _username(speaker)
        if room_id is False or not speaker or kind not in KINDS or target_kind not in TARGET_KINDS:
            return None
        if source not in SOURCES:
            return None
        if target_kind == 'bot':
            target = _username(target)
            if not target:
                return None
        else:
            target = None
        try:
            player = hash_player(player_id, self.salt)
        except ValueError:
            return None
        text = _line(text)
        if text is False:
            return None
        if kind == 'reply':
            human_text = _line(human_text)
            if human_text is False:
                return None
        else:
            human_text = None
        if priority is not None and priority not in PRIORITIES:
            return None
        scene_id = _token(scene_id)
        invite_id = _token(invite_id)
        if scene_id is False or invite_id is False:
            return None
        if kind in REASON_KINDS:
            if reason is not None and reason not in REASONS:
                return None
        else:
            reason = None
        dest_room_id = _room(dest_room_id)
        if dest_room_id is False:
            return None
        if ts is None:
            ts = int(self.clock() * 1000)
        if type(ts) is not int or not _TS_MIN <= ts <= _TS_MAX:
            return None
        event = {
            'v': 1,
            'ts': ts,
            'room_id': room_id,
            'speaker': speaker,
            'kind': kind,
            'target_kind': target_kind,
            'target': target,
            'player': player,
            'text': text,
            'human_text': human_text,
            'source': source,
            'priority': priority,
            'scene_id': scene_id,
            'invite_id': invite_id,
            'reason': reason,
            'dest_room_id': dest_room_id,
        }
        line = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        return (_day(ts), kind, line)

    def depth(self):
        """Events accepted but not yet durable (queue plus the open batch)."""
        return self._queue.qsize() + len(self._pending)

    # --------------------------------------------------------------- metrics
    def register(self, registry):
        """Export bounded series. Labels are kinds and error reasons, never text."""
        if self._registered or not hasattr(registry, 'collect'):
            return
        self._registered = True
        registry.collect(
            metrics_mod.DIALOG_EVENTS,
            lambda: [({'kind': kind}, self.written[kind]) for kind in KINDS],
            kind='counter')
        registry.collect(
            metrics_mod.DIALOG_DROPPED,
            lambda: [({'reason': 'queue_full'}, self.dropped_full)],
            kind='counter')
        registry.collect(
            metrics_mod.DIALOG_ERRORS,
            lambda: [({'reason': reason}, self.errors[reason]) for reason in ERROR_REASONS],
            kind='counter')
        registry.collect(
            metrics_mod.DIALOG_QUEUE,
            lambda: [({}, self.depth())],
            kind='gauge')
        registry.collect(
            metrics_mod.DIALOG_BYTES,
            lambda: [({}, self.bytes_written)],
            kind='counter')
        registry.collect(
            metrics_mod.DIALOG_RETENTION,
            lambda: [({}, self.retention_deleted)],
            kind='counter')
        registry.collect(
            metrics_mod.DIALOG_ENABLED,
            lambda: [({}, 1.0 if self.enabled else 0.0)],
            kind='gauge')
        registry.collect(
            metrics_mod.DIALOG_SALT,
            lambda: [({}, 1.0 if self.salt else 0.0)],
            kind='gauge')

    # ---------------------------------------------------------------- writer
    def _take(self, item):
        if self._pending_since is None:
            self._pending_since = self._monotonic()
        self._pending.append(item)

    def _drain(self):
        while True:
            try:
                self._take(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return

    def _should_flush(self):
        if not self._pending:
            return False
        if not self.enabled:
            return True
        if len(self._pending) >= self.batch_size:
            return True
        return self._monotonic() - self._pending_since >= self.flush_seconds

    def _wait_timeout(self):
        if not self._pending:
            return self.switch_seconds
        remaining = self.flush_seconds - (self._monotonic() - self._pending_since)
        return max(0.01, remaining)

    async def run(self):
        """Batch writer. Cancel the task to flush (when enabled) and stop."""
        try:
            while not self._stopping:
                await self._iteration()
        finally:
            self._drain()
            try:
                self._flush()
            except Exception as error:
                self._count_error(error)

    async def _iteration(self):
        self.reload_switch()
        self._retain_if_due()
        self._drain()
        if not self._should_flush():
            try:
                self._take(await asyncio.wait_for(self._queue.get(), timeout=self._wait_timeout()))
            except asyncio.TimeoutError:
                pass
            else:
                self._drain()
        if not self._should_flush():
            return
        try:
            self._flush()
        except Exception as error:
            self._count_error(error)
            logger.error('dialog log write failed (%s)', getattr(error, 'reason', 'io'))
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self._backoff_max)

    def _count_error(self, error):
        reason = getattr(error, 'reason', 'io')
        if reason not in self.errors:
            reason = 'io'
        self.errors[reason] += 1

    def _flush(self):
        """Write the open batch, or discard it when the switch is off."""
        if not self._pending:
            return
        if not self.enabled:
            self._pending.clear()
            self._pending_since = None
            return
        order = []
        groups = {}
        for item in self._pending:
            day = item[0]
            if day not in groups:
                order.append(day)
                groups[day] = []
            groups[day].append(item)
        for day in order:
            group = groups[day]
            payload = b''.join(item[2] for item in group)
            self._write(day, payload)
            for item in group:
                self.written[item[1]] += 1
                self.bytes_written += len(item[2])
            self._pending = [item for item in self._pending if item[0] != day]
        self._pending_since = None
        self._backoff = self._backoff_initial

    def _write(self, day, payload):
        if self._write_batch is not None:
            self._write_batch(day, payload)
            return
        self._append(day, payload)

    def _append(self, day, payload):
        """O_APPEND one batch. A torn tail from a crash is split onto its own line."""
        if not _DAY_FILE.match(day + '.jsonl'):
            raise _StoreError('io')
        root = self._prepare_dir()
        path = root / (day + '.jsonl')
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise _StoreError('symlink')
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as error:
            if error.errno in (getattr(errno, 'ELOOP', -1),):
                raise _StoreError('symlink') from error
            raise _StoreError('io') from error
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise _StoreError('io')
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o600)
            if st.st_size:
                os.lseek(fd, st.st_size - 1, os.SEEK_SET)
                if os.read(fd, 1) != b'\n':
                    os.write(fd, b'\n')
            view = payload
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise _StoreError('io')
                view = view[written:]
        except _StoreError:
            raise
        except OSError as error:
            raise _StoreError('io') from error
        finally:
            os.close(fd)

    def _prepare_dir(self):
        root = Path(self.directory)
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                info = os.lstat(root)
            except OSError as error:
                raise _StoreError('io') from error
            else:
                info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode):
            raise _StoreError('symlink')
        if not stat.S_ISDIR(info.st_mode):
            raise _StoreError('io')
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        return root

    # -------------------------------------------------------------- retention
    def _retain_if_due(self):
        now = self._monotonic()
        if self._last_retain is not None and now - self._last_retain < self.retention_interval:
            return
        self._last_retain = now
        try:
            self.retain()
        except Exception as error:
            self._count_error(error)

    def retain(self, today=None):
        """Delete day files whose UTC date is more than ``retention_days`` old.

        Names that are not ``YYYY-MM-DD.jsonl``, and any symlink, are left
        untouched. A missing directory is not created.
        """
        if today is None:
            today = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).date()
        cutoff = today - datetime.timedelta(days=self.retention_days)
        root = Path(self.directory)
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return 0
        deleted = 0
        for entry in os.scandir(root):
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            match = _DAY_FILE.match(entry.name)
            if not match:
                continue
            try:
                day = datetime.date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if day < cutoff:
                os.unlink(entry.path)
                deleted += 1
        self.retention_deleted += deleted
        return deleted

"""Phase 4: bounded relationship notes per bot and numeric player id on the PVC.

Stores only a short summary and timestamps — never chat logs or player names.
"""
import logging
import os
import re
import sqlite3
import stat
import time
from pathlib import Path

from . import text

logger = logging.getLogger('bots.recall')

_FORGET = re.compile(
    r'\b(?:olvida(?:me)?|borra|elimina|quita)\b(?:\s+\w+){0,4}\s*(?:mi\s+)?(?:memoria|recuerdo|nota)\b',
    re.IGNORECASE)


def wants_forget(message):
    return bool(_FORGET.search(text.fold(str(message))))


class RecallStore:
    """SQLite-backed notes with TTL expiry and fail-closed writes."""

    def __init__(self, path, *, ttl_seconds=30 * 86400, max_note_chars=120,
                 max_pairs=8192, clock=time.time):
        if ttl_seconds <= 0:
            raise ValueError('recall ttl must be positive')
        self.ttl_seconds = float(ttl_seconds)
        self.max_note_chars = int(max_note_chars)
        self.max_pairs = int(max_pairs)
        self.clock = clock
        self.path = Path(path) if path else None
        self.state_ok = True
        self._conn = None
        if self.path:
            self._open()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create or open without following symlinks. The mode is explicit because
        # SQLite would otherwise honour the container umask (0644 in a pod), and
        # a file that is ours but left open by an earlier start is made private
        # again instead of crash-looping the service.
        try:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise ValueError('recall database must be private and not a symlink') from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('recall database must be private and not a symlink')
            if info.st_mode & 0o077:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA synchronous=NORMAL')
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS notes ('
            'bot_index INTEGER NOT NULL, player_id INTEGER NOT NULL, '
            'note TEXT NOT NULL, updated REAL NOT NULL, '
            'PRIMARY KEY (bot_index, player_id))')

    def _fresh(self, updated):
        return self.clock() - float(updated) <= self.ttl_seconds

    def get(self, bot_index, player_id):
        if self._conn is None:
            return None
        row = self._conn.execute(
            'SELECT note, updated FROM notes WHERE bot_index=? AND player_id=?',
            (int(bot_index), int(player_id))).fetchone()
        if not row:
            return None
        note, updated = row
        if not self._fresh(updated):
            self.delete(bot_index, player_id)
            return None
        return str(note)

    def upsert(self, bot_index, player_id, note):
        note = str(note).strip()[: self.max_note_chars]
        if not note:
            return False
        if self._conn is None:
            self.state_ok = False
            return False
        now = self.clock()
        try:
            count = self._conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
            exists = self._conn.execute(
                'SELECT 1 FROM notes WHERE bot_index=? AND player_id=?',
                (int(bot_index), int(player_id))).fetchone()
            if not exists and count >= self.max_pairs:
                self.state_ok = False
                return False
            self._conn.execute(
                'INSERT INTO notes (bot_index, player_id, note, updated) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(bot_index, player_id) DO UPDATE SET note=excluded.note, updated=excluded.updated',
                (int(bot_index), int(player_id), note, now))
            self._conn.commit()
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self.state_ok = True
            return True
        except sqlite3.Error as error:
            logger.warning('recall upsert failed: %s', type(error).__name__)
            self.state_ok = False
            return False

    def delete(self, bot_index, player_id):
        if self._conn is None:
            return False
        try:
            self._conn.execute(
                'DELETE FROM notes WHERE bot_index=? AND player_id=?',
                (int(bot_index), int(player_id)))
            self._conn.commit()
            self.state_ok = True
            return True
        except sqlite3.Error as error:
            logger.warning('recall delete failed: %s', type(error).__name__)
            self.state_ok = False
            return False

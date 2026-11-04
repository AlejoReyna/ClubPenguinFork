"""Global API attempt budget, including retries; no chat or credentials stored."""
import datetime
import json
import os
from pathlib import Path
import time


class RequestBudget:
    def __init__(self, per_minute=6, per_day=100, path=None, clock=time.time):
        self.per_minute = int(per_minute)
        self.per_day = int(per_day)
        if self.per_minute < 1 or self.per_day < 1:
            raise ValueError('request budgets must be positive')
        self.path = Path(path) if path else None
        self.clock = clock
        self.data = {'day': '', 'used': 0, 'attempts': []}
        if self.path and self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise ValueError('budget file must be private and not a symlink')
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data.get('attempts'), list) or not isinstance(self.data.get('used'), int):
                raise ValueError('invalid budget ledger')

    def reserve(self):
        now = self.clock()
        day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%d')
        if day > self.data['day']:
            self.data.update(day=day, used=0)
        recent = [stamp for stamp in self.data['attempts'] if stamp > now - 60]
        if self.data['used'] >= self.per_day or len(recent) >= self.per_minute:
            return False
        self.data.update(used=self.data['used'] + 1, attempts=recent + [now])
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self.path.with_suffix('.tmp')
            if self.path.is_symlink() or temp.is_symlink():
                raise ValueError('budget path must not be a symlink')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(self.data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temp.chmod(0o600)
            os.replace(temp, self.path)
        return True

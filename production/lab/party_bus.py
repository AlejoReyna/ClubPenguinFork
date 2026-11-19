"""Shared party scene signal between the world server and the bots service."""
import json
import os
from pathlib import Path


class PartyBus:
    """Atomic read/write of a single scene document."""

    def __init__(self, path, clock):
        self.path = Path(path)
        self.clock = clock

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, document):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        payload = json.dumps(document, separators=(',', ':'), sort_keys=True)
        tmp.write_text(payload + '\n', encoding='utf-8')
        os.replace(tmp, self.path)

    def clear(self):
        try:
            self.path.unlink()
        except OSError:
            pass


def default_bus(clock):
    path = os.environ.get('LAB_PARTY_BUS_PATH', '/var/lib/lab-party/scene.json')
    return PartyBus(path, clock)
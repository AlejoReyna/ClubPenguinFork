"""Public, filtered feed of the AI penguins' dialog log for the game page's «Diálogos» panel.

Runs as a pod-local sidecar (MODE=dialog) next to nginx, which proxies the same-origin path
/dialog/feed.json to 127.0.0.1:8767. It is the only reader of the dialog log outside the
owner's Mac (docs/dialogos-publicos.md):

* The PVC `bots-dialog` and the switch ConfigMap are mounted read-only. Nothing here writes.
* Each event is rebuilt from an allow-list of fields, each one re-validated. The player hash
  (`player`) and the human message that triggered a reply (`human_text`) are never copied,
  whatever the file contains.
* Only today's and yesterday's UTC day files are read, from a bounded tail; symlinks and
  non-regular files are refused. A bad line is skipped and counted, never a 500.
* The ConfigMap switch is also the public kill switch: when it is off the feed is empty.
* Responses are capped in event count and bytes, and cached for a few seconds so polling
  viewers do not multiply disk reads.
"""
import datetime
import json
import os
import re
import stat
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse

DIALOG_DIR = '/var/lib/bots-dialog'
SWITCH_PATH = '/etc/bots-dialog/enabled'
PORT = 8767

MAX_EVENTS = 200
READ_MAX = 256 * 1024     # bytes read from the end of each day file
BODY_MAX = 96 * 1024      # serialized response cap
CACHE_SECONDS = 5.0
TEXT_MAX = 120

# Mirrors the writer's closed vocabularies (containers/bots/bots/dialog_log.py, contract v1).
KINDS = ('reply', 'greeting', 'scene', 'invite', 'accept', 'decline', 'travel')
TARGET_KINDS = ('player', 'bot', 'room')
SOURCES = ('model', 'local', 'fallback')
REASONS = (
    'relation_friend', 'busy_with_human', 'recent_decline', 'low_energy', 'off_schedule',
    'lease_expired', 'room_min_guard', 'restart', 'p2_cutoff', 'seeded_roll',
)

# The public contract: these keys and no others ever leave this process.
PUBLIC_FIELDS = ('ts', 'room_id', 'speaker', 'kind', 'target_kind', 'target', 'text', 'source',
                 'scene_id', 'invite_id', 'reason', 'dest_room_id')
FORBIDDEN_FIELDS = ('human_text', 'player', 'player_id')

_USER = re.compile(r'^[A-Za-z0-9]{1,12}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_TS_MIN = 946684800000    # 2000-01-01 UTC
_TS_MAX = 4102444800000   # 2100-01-01 UTC


def _int_id(value):
    return value if type(value) is int and 0 < value < 2**31 else None


def _text(value):
    """Printable text only (no control/format characters), bounded. ``None`` when unusable."""
    if not isinstance(value, str):
        return None
    cleaned = ''.join(ch for ch in value if unicodedata.category(ch)[0] != 'C')
    cleaned = ' '.join(cleaned.split())[:TEXT_MAX]
    return cleaned or None


def public_event(raw):
    """Rebuild one event from the allow-list. ``None`` when the line is not a usable event."""
    if not isinstance(raw, dict):
        return None
    ts, room_id, speaker = raw.get('ts'), _int_id(raw.get('room_id')), raw.get('speaker')
    kind, target_kind, source = raw.get('kind'), raw.get('target_kind'), raw.get('source')
    if type(ts) is not int or not _TS_MIN <= ts <= _TS_MAX:
        return None
    if room_id is None or not isinstance(speaker, str) or not _USER.match(speaker):
        return None
    if kind not in KINDS or target_kind not in TARGET_KINDS or source not in SOURCES:
        return None
    target = raw.get('target')
    if target_kind != 'bot' or not isinstance(target, str) or not _USER.match(target):
        target = None   # a player target is never named, not even by hash
    reason = raw.get('reason') if raw.get('reason') in REASONS else None
    scene_id = raw.get('scene_id') if isinstance(raw.get('scene_id'), str) and _TOKEN.match(raw['scene_id']) else None
    invite_id = raw.get('invite_id') if isinstance(raw.get('invite_id'), str) and _TOKEN.match(raw['invite_id']) else None
    event = {
        'ts': ts, 'room_id': room_id, 'speaker': speaker, 'kind': kind, 'target_kind': target_kind,
        'target': target, 'text': _text(raw.get('text')), 'source': source, 'scene_id': scene_id,
        'invite_id': invite_id, 'reason': reason, 'dest_room_id': _int_id(raw.get('dest_room_id')),
    }
    return event


def switch_on(path=SWITCH_PATH):
    """Same rule as the writer: only the exact word ``true`` is on; missing or unreadable is off."""
    try:
        with open(path, 'rb') as handle:   # ConfigMap mounts are symlinks: follow them here only
            return handle.read(64).decode('utf-8').strip().lower() == 'true'
    except (OSError, UnicodeError):
        return False


def read_tail(path, limit=READ_MAX):
    """Complete lines from the last ``limit`` bytes of a regular, non-symlink file. ``[]`` if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return []
        start = max(0, info.st_size - limit)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, remaining = [], info.st_size - start
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return []
    finally:
        os.close(fd)
    data = b''.join(chunks)
    lines = data.split(b'\n')
    if start > 0:
        lines = lines[1:]          # the first line was cut by the tail window
    if data and not data.endswith(b'\n'):
        lines = lines[:-1]         # torn tail of an in-progress write: not an event yet
    return [line for line in lines if line.strip()]


def _days(now):
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    return [(today - datetime.timedelta(days=1)).isoformat(), today.isoformat()]


def collect(directory, now, max_events=MAX_EVENTS):
    """``(events, skipped)``: the newest public events from yesterday and today, oldest first."""
    try:
        info = os.lstat(directory)
    except OSError:
        return [], 0
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [], 0
    events, skipped = [], 0
    for day in _days(now):
        for line in read_tail(os.path.join(directory, day + '.jsonl')):
            try:
                event = public_event(json.loads(line))
            except (ValueError, UnicodeError, RecursionError):
                event = None
            if event is None:
                skipped += 1
            else:
                events.append(event)
    events.sort(key=lambda e: e['ts'])
    return events[-max_events:] if max_events else [], skipped


def encode(payload, body_max=BODY_MAX):
    """Serialize, dropping the oldest events until the body fits the cap."""
    events = payload['events']
    while True:
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(body) <= body_max or not events:
            return body
        drop = max(1, len(events) // 10)
        del events[:drop]
        payload['count'] = len(events)


class Feed:
    def __init__(self, env=None, now=time.time):
        env = os.environ if env is None else env
        self.directory = str(env.get('DIALOG_DIR', DIALOG_DIR))
        self.switch_path = str(env.get('DIALOG_SWITCH', SWITCH_PATH))
        self.now = now
        self._lock = threading.Lock()
        self._cached = None
        self._cached_until = 0.0

    def build(self):
        with self._lock:
            now = self.now()
            if self._cached is not None and now < self._cached_until:
                return self._cached
            enabled = switch_on(self.switch_path)
            events, skipped = collect(self.directory, now) if enabled else ([], 0)
            self._cached = {'enabled': enabled, 'events': events, 'skipped': skipped, 'generatedAt': int(now)}
            self._cached_until = now + CACHE_SECONDS
            return self._cached

    def response(self, limit=MAX_EVENTS):
        snapshot = self.build()
        limit = max(0, min(int(limit), MAX_EVENTS))
        events = snapshot['events'][-limit:] if limit else []
        payload = dict(snapshot, events=list(events), count=len(events))
        return encode(payload)


def parse_limit(query):
    values = parse.parse_qs(query).get('limit')
    if not values:
        return MAX_EVENTS
    try:
        return max(0, min(int(values[0]), MAX_EVENTS))
    except ValueError:
        return MAX_EVENTS


class Handler(BaseHTTPRequestHandler):
    feed = None
    server_version = 'dialog-feed'

    def do_GET(self):  # noqa: N802 - http.server API
        path, _, query = self.path.partition('?')
        if path == '/healthz':
            self._send(200, b'ok', 'text/plain')
            return
        if path != '/feed':
            self.send_error(404)
            return
        try:
            body = self.feed.response(parse_limit(query))
        except Exception:  # the page must keep working whatever happens here
            self.send_error(503)
            return
        self._send(200, body, 'application/json')

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # no request logging
        pass


def main():
    Handler.feed = Feed()
    # Pod-local only: nginx in the same pod proxies here. Not reachable from the network.
    ThreadingHTTPServer((os.environ.get('DIALOG_FEED_BIND', '127.0.0.1').strip(), PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()

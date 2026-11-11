#!/usr/bin/env python3
"""Read a directory of dialog-log JSONL files. Standard library only.

The bots service does not serve this log. Copy the files off the pod first
(docs/operations.md), then run this on the Mac or the Pi:

  python3 scripts/bitacora.py --dir ./bots-dialog filter --room 110 --penguin rayo \\
      --since 2026-11-01 --until 2026-11-11
  python3 scripts/bitacora.py --dir ./bots-dialog conversations
  python3 scripts/bitacora.py --dir ./bots-dialog matrix
  python3 scripts/bitacora.py --dir ./bots-dialog invites
  python3 scripts/bitacora.py --dir ./bots-dialog sources
  python3 scripts/bitacora.py --dir ./bots-dialog phrases --top 20
  python3 scripts/bitacora.py --dir ./bots-dialog export --format csv --output dialog.csv
  python3 scripts/bitacora.py --dir ./bots-dialog export --format jsonl --output dialog.jsonl

A last line with no trailing newline is a torn write and is ignored. Other
lines that are not JSON are ignored too. Player hashes are not reversed.
"""
import argparse
import csv
import datetime
import json
import re
import sys
from pathlib import Path

# Keep these in step with containers/bots/bots/dialog_log.py and docs/bots.md.
FIELDS = (
    'v', 'ts', 'room_id', 'speaker', 'kind', 'target_kind', 'target', 'player',
    'text', 'human_text', 'source', 'priority', 'scene_id', 'invite_id',
    'reason', 'dest_room_id',
)
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
SOURCES = ('model', 'local', 'fallback')
SPOKEN = ('reply', 'greeting', 'scene')
CONVERSATION_GAP_MS = 600000
_DAY = re.compile(r'^(\d{4}-\d{2}-\d{2})\.jsonl$')


def parse_date(value):
    """``YYYY-MM-DD`` or ``None``. Returns ``None`` for an unusable value too; callers check."""
    if value is None:
        return None
    text = str(value)
    try:
        parsed = datetime.date.fromisoformat(text)
    except ValueError:
        return False
    if parsed.isoformat() != text:
        return False
    return parsed


def _event_day(event):
    try:
        ts = int(event.get('ts'))
    except (TypeError, ValueError):
        return None
    if ts < 0:
        return None
    return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).date()


def iter_file(path):
    """Yield v1 objects. The trailing line is dropped when it has no newline."""
    data = Path(path).read_bytes()
    if not data:
        return
    if not data.endswith(b'\n'):
        head, sep, _tail = data.rpartition(b'\n')
        if not sep:
            return
        data = head + sep
    for raw in data.decode('utf-8', errors='replace').splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get('v') == 1:
            yield event


def day_files(directory, since=None, until=None):
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(directory)
    if not root.is_dir() or root.is_symlink():
        raise NotADirectoryError(directory)
    found = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            continue
        match = _DAY.match(path.name)
        if not match:
            continue
        try:
            day = datetime.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if since is not None and day < since:
            continue
        if until is not None and day > until:
            continue
        found.append(path)
    return found


def load_events(directory, *, room=None, penguin=None, since=None, until=None):
    """Events from every day file, filtered and ordered by time."""
    penguin_key = penguin.casefold() if penguin else None
    events = []
    for path in day_files(directory, since, until):
        for event in iter_file(path):
            day = _event_day(event)
            if day is None:
                continue
            if since is not None and day < since:
                continue
            if until is not None and day > until:
                continue
            if room is not None and event.get('room_id') != room:
                continue
            speaker = event.get('speaker')
            if penguin_key is not None and (not isinstance(speaker, str) or speaker.casefold() != penguin_key):
                continue
            events.append(event)
    events.sort(key=lambda event: (event.get('ts') or 0, event.get('speaker') or ''))
    return events


def conversations(events, gap_ms=CONVERSATION_GAP_MS):
    """Scene threads, invitation threads, and player/room chats split by a time gap.

    An ``invite_id`` keeps the invite, the reply to it, the travel and the
    return together. Otherwise a ``scene_id`` groups one bot-to-bot scene.
    Remaining lines in the same room, for the same player hash (or the same
    speaker when there is no player), form a chat. A gap longer than
    ``gap_ms`` starts a new chat.
    """
    invite_threads = {}
    scene_threads = {}
    loose = []
    for event in events:
        if event.get('invite_id'):
            invite_threads.setdefault(event['invite_id'], []).append(event)
        elif event.get('scene_id'):
            scene_threads.setdefault((event.get('room_id'), event['scene_id']), []).append(event)
        else:
            loose.append(event)

    def pack(kind, ident, rows):
        rows = sorted(rows, key=lambda item: item.get('ts') or 0)
        return {'kind': kind, 'id': ident, 'room_id': rows[0].get('room_id'), 'events': rows}

    result = [pack('invite', ident, rows) for ident, rows in invite_threads.items()]
    result.extend(pack('scene', ident[1], rows) for ident, rows in scene_threads.items())

    loose.sort(key=lambda event: (
        event.get('room_id') is None, event.get('room_id') or 0, event.get('ts') or 0))
    current = None
    current_key = None
    for event in loose:
        player = event.get('player')
        who = player if player else 'speaker:' + str(event.get('speaker') or '')
        key = (event.get('room_id'), who)
        ts = event.get('ts') or 0
        if (current is None or key != current_key
                or ts - (current['events'][-1].get('ts') or 0) > gap_ms):
            current = pack('chat', who, [event])
            current_key = key
            result.append(current)
        else:
            current['events'].append(event)
    result.sort(key=lambda item: (item['events'][0].get('ts') or 0, str(item['id'])))
    return result


def matrix(events):
    """Directed counts. Players are ``player:`` plus the salted hash, never a name."""
    counts = {}
    for event in events:
        source = event.get('speaker')
        if not isinstance(source, str) or not source:
            continue
        target_kind = event.get('target_kind')
        if target_kind == 'bot' and event.get('target'):
            dest = event['target']
        elif target_kind == 'player' and event.get('player'):
            dest = 'player:' + str(event['player'])
        elif target_kind == 'room':
            room = event.get('dest_room_id')
            if room is None:
                room = event.get('room_id')
            dest = 'room:' + str(room)
        else:
            continue
        key = (source, dest)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def invitations(events):
    """One row per ``invite_id``: made, accepted, declined, travels, and reasons."""
    rows = {}
    order = []
    for event in events:
        ident = event.get('invite_id')
        if not ident:
            continue
        if ident not in rows:
            order.append(ident)
            rows[ident] = {
                'invite_id': ident, 'made': 0, 'accepted': 0, 'declined': 0, 'travels': 0,
                'speaker': None, 'target': None, 'dest_room_id': None, 'reasons': [],
            }
        row = rows[ident]
        kind = event.get('kind')
        if kind == 'invite':
            row['made'] += 1
            row['speaker'] = event.get('speaker')
            row['target'] = event.get('target')
            row['dest_room_id'] = event.get('dest_room_id')
        elif kind == 'accept':
            row['accepted'] += 1
        elif kind == 'decline':
            row['declined'] += 1
        elif kind == 'travel':
            row['travels'] += 1
        if kind in ('accept', 'decline', 'travel') and event.get('reason'):
            row['reasons'].append((kind, event['reason']))
    return [rows[ident] for ident in order]


def reason_breakdown(events):
    """Counts of the closed reason codes on accept, decline and travel."""
    counts = {reason: 0 for reason in REASONS}
    for event in events:
        if event.get('kind') not in ('accept', 'decline', 'travel'):
            continue
        reason = event.get('reason')
        if reason in counts:
            counts[reason] += 1
        elif reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def source_ratio(events, kinds=None):
    counts = {name: 0 for name in SOURCES}
    for event in events:
        if kinds is not None and event.get('kind') not in kinds:
            continue
        source = event.get('source')
        if source in counts:
            counts[source] += 1
    total = sum(counts.values())
    ratio = {name: (counts[name] / total if total else 0.0) for name in SOURCES}
    return {'counts': counts, 'total': total, 'ratio': ratio}


def repeated_phrases(events, top=20):
    """Most common bot lines (``text`` only, not the human message)."""
    counts = {}
    for event in events:
        text = event.get('text')
        if not isinstance(text, str):
            continue
        phrase = ' '.join(text.split())
        if not phrase:
            continue
        counts[phrase] = counts.get(phrase, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if top is None:
        return ranked
    return ranked[:max(0, int(top))]


def event_json(event):
    return json.dumps({key: event.get(key) for key in FIELDS},
                      ensure_ascii=False, separators=(',', ':'))


def render_event(event):
    when = event.get('ts')
    try:
        stamp = datetime.datetime.fromtimestamp(int(when) / 1000, datetime.timezone.utc)
        clock = stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError, OSError, OverflowError):
        clock = '?'
    text = event.get('text') or ''
    bits = [
        clock,
        f"room={event.get('room_id')}",
        str(event.get('speaker') or ''),
        str(event.get('kind') or ''),
        str(event.get('source') or ''),
    ]
    if event.get('priority'):
        bits.append(str(event['priority']))
    if event.get('target'):
        bits.append('->' + str(event['target']))
    if event.get('reason'):
        bits.append('reason=' + str(event['reason']))
    if event.get('invite_id'):
        bits.append('invite=' + str(event['invite_id']))
    if event.get('scene_id'):
        bits.append('scene=' + str(event['scene_id']))
    if event.get('dest_room_id') is not None:
        bits.append('dest=' + str(event['dest_room_id']))
    line = ' '.join(bits)
    if text:
        line += ': ' + text
    human = event.get('human_text')
    if human:
        line += ' | human: ' + human
    return line


def render_conversations(groups):
    if not groups:
        return 'no conversations\n'
    lines = []
    for group in groups:
        lines.append(
            f"conversation kind={group['kind']} id={group['id']} "
            f"room={group['room_id']} events={len(group['events'])}")
        for event in group['events']:
            lines.append('  ' + render_event(event))
    return '\n'.join(lines) + '\n'


def render_matrix(counts):
    if not counts:
        return 'from\tto\tcount\n'
    lines = ['from\tto\tcount']
    for (source, dest), count in counts.items():
        lines.append(f'{source}\t{dest}\t{count}')
    return '\n'.join(lines) + '\n'


def render_invitations(rows, breakdown):
    lines = ['invite_id\tfrom\tto\tdest\tmade\taccepted\tdeclined\ttravels\treasons']
    for row in rows:
        reasons = ','.join(f'{kind}:{reason}' for kind, reason in row['reasons'])
        lines.append(
            f"{row['invite_id']}\t{row['speaker'] or ''}\t{row['target'] or ''}\t"
            f"{'' if row['dest_room_id'] is None else row['dest_room_id']}\t"
            f"{row['made']}\t{row['accepted']}\t{row['declined']}\t{row['travels']}\t{reasons}")
    lines.append('reason\tcount')
    for reason, count in breakdown.items():
        if count:
            lines.append(f'{reason}\t{count}')
    return '\n'.join(lines) + '\n'


def render_sources(spoken, everything):
    def block(title, report):
        lines = [title, 'source\tcount\tratio']
        for name in SOURCES:
            lines.append(f"{name}\t{report['counts'][name]}\t{report['ratio'][name]:.3f}")
        return lines
    lines = block('spoken (reply, greeting, scene)', spoken)
    lines.append('')
    lines.extend(block('all events', everything))
    return '\n'.join(lines) + '\n'


def render_phrases(ranked):
    if not ranked:
        return 'count\tphrase\n'
    lines = ['count\tphrase']
    for phrase, count in ranked:
        lines.append(f'{count}\t{phrase}')
    return '\n'.join(lines) + '\n'


def write_jsonl(events, output):
    path = Path(output)
    with path.open('w', encoding='utf-8') as handle:
        for event in events:
            handle.write(event_json(event) + '\n')


def write_csv(events, output):
    path = Path(output)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS), extrasaction='ignore')
        writer.writeheader()
        for event in events:
            writer.writerow({
                key: '' if event.get(key) is None else event.get(key) for key in FIELDS})


def _filters(parser):
    parser.add_argument('--room', type=int)
    parser.add_argument('--penguin', help='bot username (speaker)')
    parser.add_argument('--since', help='UTC day YYYY-MM-DD, inclusive')
    parser.add_argument('--until', help='UTC day YYYY-MM-DD, inclusive')


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Analyse a copy of the bot dialog log (JSONL day files).',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dir', required=True, help='directory of YYYY-MM-DD.jsonl files')
    commands = parser.add_subparsers(dest='command', required=True)

    for name in ('filter', 'conversations', 'matrix', 'invites', 'sources', 'phrases'):
        sub = commands.add_parser(name)
        _filters(sub)
        if name == 'phrases':
            sub.add_argument('--top', type=int, default=20)

    export = commands.add_parser('export')
    _filters(export)
    export.add_argument('--format', choices=('csv', 'jsonl'), required=True)
    export.add_argument('--output', required=True)

    args = parser.parse_args(argv)
    since = parse_date(args.since)
    until = parse_date(args.until)
    if since is False or until is False:
        print('since and until must be YYYY-MM-DD', file=sys.stderr)
        return 2
    try:
        events = load_events(args.dir, room=args.room, penguin=args.penguin, since=since, until=until)
    except FileNotFoundError:
        print(f'no such directory: {args.dir}', file=sys.stderr)
        return 2
    except NotADirectoryError:
        print(f'not a directory: {args.dir}', file=sys.stderr)
        return 2
    except OSError as error:
        print(f'cannot read {args.dir}: {error.strerror or type(error).__name__}', file=sys.stderr)
        return 2

    if args.command == 'filter':
        sys.stdout.write(''.join(render_event(event) + '\n' for event in events) or 'no events\n')
    elif args.command == 'conversations':
        sys.stdout.write(render_conversations(conversations(events)))
    elif args.command == 'matrix':
        sys.stdout.write(render_matrix(matrix(events)))
    elif args.command == 'invites':
        sys.stdout.write(render_invitations(invitations(events), reason_breakdown(events)))
    elif args.command == 'sources':
        sys.stdout.write(render_sources(source_ratio(events, SPOKEN), source_ratio(events)))
    elif args.command == 'phrases':
        sys.stdout.write(render_phrases(repeated_phrases(events, args.top)))
    elif args.command == 'export':
        try:
            if args.format == 'csv':
                write_csv(events, args.output)
            else:
                write_jsonl(events, args.output)
        except OSError as error:
            print(f'cannot write {args.output}: {error.strerror or type(error).__name__}',
                  file=sys.stderr)
            return 2
        print(f'wrote {len(events)} events to {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

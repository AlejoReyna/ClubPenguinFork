"""Prometheus metrics without third-party dependencies.

A small registry plus a scrape server that also answers ``/healthz``. Label
values are fixed identifiers (metric names, result kinds, positional bot
index); chat text, player names and credentials are never used as labels,
mirroring the lab exporter's rule.
"""
import asyncio

# Well-known series. Since labels must not carry names or chat text, bots are
# distinguished by the positional label bot="0","1",...
BOTS_UP = 'bots_up'
BOTS_CONNECTED = 'bots_connected'
BOTS_SESSIONS = 'bots_sessions_total'
BOTS_HEARTBEATS = 'bots_heartbeats_total'
BOTS_RECONNECTS = 'bots_reconnects_total'
BOTS_AUTH_FAILURES = 'bots_auth_failures_total'
BOTS_CHATS_RECEIVED = 'bots_chats_received_total'
BOTS_CHATS_SENT = 'bots_chats_sent_total'
BOTS_SKIPPED = 'bots_replies_skipped_total'
BOTS_FALLBACK = 'bots_provider_fallback_total'
PROVIDER_REQUESTS = 'bots_provider_requests_total'
PROVIDER_ERRORS = 'bots_provider_errors_total'
PROVIDER_LATENCY = 'bots_provider_latency_seconds'
PROVIDER_PROMPT_TOKENS = 'bots_provider_prompt_tokens_total'
PROVIDER_COMPLETION_TOKENS = 'bots_provider_completion_tokens_total'

# Bounded skip reasons (never free text).
SKIP_SELF = 'self'
SKIP_BOT = 'bot'
SKIP_NOT_MENTIONED = 'not_mentioned'
SKIP_BOT_GAP = 'bot_gap'
SKIP_RATE_LIMIT = 'rate_limit'
SKIP_PLAYER_COOLDOWN = 'player_cooldown'
SKIP_QUEUE_FULL = 'queue_full'
SKIP_EMPTY_REPLY = 'empty_reply'

HELP_TEXTS = {
    BOTS_UP: 'Bots service process up',
    BOTS_CONNECTED: 'Whether each configured bot holds a world session',
    BOTS_SESSIONS: 'Login+world connections established per bot',
    BOTS_HEARTBEATS: 'Heartbeat acknowledgements received from the world server',
    BOTS_RECONNECTS: 'Automatic reconnects performed per bot',
    BOTS_AUTH_FAILURES: 'Authentication failures that require credential review',
    BOTS_CHATS_RECEIVED: 'Chat messages observed in the room per bot',
    BOTS_CHATS_SENT: 'Chat messages actually sent per bot',
    BOTS_SKIPPED: 'Messages ignored, with the bounded reason as label',
    BOTS_FALLBACK: 'Local fallback replies because the provider failed',
    PROVIDER_REQUESTS: 'Provider chat-completion calls by outcome',
    PROVIDER_ERRORS: 'Provider failures by bounded error kind',
    PROVIDER_LATENCY: 'Successful provider round-trip duration seconds',
    PROVIDER_PROMPT_TOKENS: 'Prompt tokens reported by the provider',
    PROVIDER_COMPLETION_TOKENS: 'Completion tokens reported by the provider',
}

HISTOGRAM_BUCKETS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)

_STANDARD_SERIES = (
    (BOTS_UP, 'gauge'), (BOTS_CONNECTED, 'gauge'),
    (BOTS_SESSIONS, 'counter'), (BOTS_HEARTBEATS, 'counter'), (BOTS_RECONNECTS, 'counter'),
    (BOTS_AUTH_FAILURES, 'counter'), (BOTS_CHATS_RECEIVED, 'counter'),
    (BOTS_CHATS_SENT, 'counter'), (BOTS_SKIPPED, 'counter'),
    (BOTS_FALLBACK, 'counter'), (PROVIDER_REQUESTS, 'counter'),
    (PROVIDER_ERRORS, 'counter'), (PROVIDER_LATENCY, 'histogram'),
    (PROVIDER_PROMPT_TOKENS, 'counter'), (PROVIDER_COMPLETION_TOKENS, 'counter'),
)


def _escape_label(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def _render_labels(labels):
    if not labels:
        return ''
    rendered = ','.join(f'{key}="{_escape_label(value)}"'
                        for key, value in sorted(labels.items()))
    return '{' + rendered + '}'


def _format_float(value):
    return f'{float(value):.6g}'


class Registry:
    """Counters, gauges and simple histograms rendered in text format.

    Updates run inside the service's single event loop and each operation is
    a straight-line block with no await between read and write, so plain
    cooperative scheduling serializes them; there is no cross-thread access
    (render runs on the same loop from the metrics server).
    """

    def __init__(self, buckets=HISTOGRAM_BUCKETS):
        self._buckets = buckets
        self._help = HELP_TEXTS
        self._type = dict(_STANDARD_SERIES)
        self._values = {}      # (name, labels_key) -> [labels, value]
        self._histograms = {}  # (name, labels_key) -> [labels, buckets, sum]

    async def add(self, name, by=1.0, labels=None):
        key = (name, _labels_key(labels))
        entry = self._values.get(key)
        if entry is None:
            entry = [dict(labels or {}), 0.0]
            self._values[key] = entry
        entry[1] = entry[1] + by

    async def set(self, name, value, labels=None):
        key = (name, _labels_key(labels))
        entry = self._values.get(key)
        if entry is None:
            entry = [dict(labels or {}), float(value)]
            self._values[key] = entry
        entry[1] = float(value)

    async def observe(self, name, value, labels=None):
        value = float(value)
        key = (name, _labels_key(labels))
        entry = self._histograms.get(key)
        if entry is None:
            entry = [dict(labels or {}), [0.0] * (len(self._buckets) + 1), 0.0]
            self._histograms[key] = entry
        index = next((i for i, bucket in enumerate(self._buckets)
                      if value <= bucket), len(self._buckets))
        entry[1][index] += 1
        entry[2] += value

    def render(self):
        """Prometheus text format; sorted for stable and diffable output."""
        lines = []
        grouped = {}
        for (name, _key), (labels, value) in sorted(self._values.items()):
            grouped.setdefault(name, []).append((labels, value))
        for (name, _key), (labels, buckets, total) in sorted(self._histograms.items()):
            grouped.setdefault(name, []).append((labels, (buckets, total)))
        for name, samples in sorted(grouped.items()):
            lines.append(f'# HELP {name} {self._help.get(name, "")}')
            lines.append(f'# TYPE {name} {self._type.get(name, "gauge")}')
            for labels, value in samples:
                rendered = _render_labels(labels)
                if isinstance(value, tuple):
                    bucket_counts, total = value
                    cumulative = 0.0
                    for bucket, count in zip(self._buckets, bucket_counts):
                        cumulative += count
                        bucket_labels = dict(labels, le=_format_float(bucket))
                        lines.append(
                            f'{name}_bucket{_render_labels(bucket_labels)}'
                            f' {_format_float(cumulative)}')
                    lines.append(f'{name}_bucket{_render_labels(dict(labels, le="+Inf"))}'
                                 f' {_format_float(sum(bucket_counts))}')
                    lines.append(f'{name}_sum{rendered} {_format_float(total)}')
                    lines.append(f'{name}_count{rendered}'
                                 f' {_format_float(sum(bucket_counts))}')
                else:
                    lines.append(f'{name}{rendered} {_format_float(value)}')
        if not lines:
            lines.append('# no metrics recorded')
        return '\n'.join(lines) + '\n'


def _labels_key(labels):
    return tuple(sorted((labels or {}).items()))


class BotMetrics:
    """Semantic helpers shared by sessions, behavior and the provider."""

    def __init__(self, registry):
        self.registry = registry

    async def mark_up(self):
        await self.registry.set(BOTS_UP, 1.0)

    async def connected(self, bot_index, connected):
        await self.registry.set(BOTS_CONNECTED, 1.0 if connected else 0.0,
                                labels={'bot': str(bot_index)})

    async def session_established(self, bot_index):
        await self.registry.add(BOTS_SESSIONS, labels={'bot': str(bot_index)})

    async def heartbeat(self, bot_index):
        await self.registry.add(BOTS_HEARTBEATS, labels={'bot': str(bot_index)})

    async def reconnected(self, bot_index):
        await self.registry.add(BOTS_RECONNECTS, labels={'bot': str(bot_index)})

    async def auth_failed(self, bot_index):
        await self.registry.add(BOTS_AUTH_FAILURES, labels={'bot': str(bot_index)})

    async def chat_received(self, bot_index):
        await self.registry.add(BOTS_CHATS_RECEIVED, labels={'bot': str(bot_index)})

    async def chat_sent(self, bot_index):
        await self.registry.add(BOTS_CHATS_SENT, labels={'bot': str(bot_index)})

    async def skipped(self, bot_index, reason):
        await self.registry.add(BOTS_SKIPPED, labels={
            'bot': str(bot_index), 'reason': reason})

    async def fallback_used(self, bot_index):
        await self.registry.add(BOTS_FALLBACK, labels={'bot': str(bot_index)})

    async def provider_ok(self, seconds):
        await self.registry.add(PROVIDER_REQUESTS, labels={'result': 'ok'})
        await self.registry.observe(PROVIDER_LATENCY, seconds)

    async def provider_error(self, kind):
        await self.registry.add(PROVIDER_REQUESTS, labels={'result': 'error'})
        await self.registry.add(PROVIDER_ERRORS, labels={'kind': kind})

    async def add(self, name, by=1.0, labels=None):
        await self.registry.add(name, by, labels)


class MetricsServer:
    """Minimal HTTP responder: GET /metrics and /healthz, nothing else."""

    def __init__(self, registry, port, ready=None):
        self.registry = registry
        self.port = int(port)
        self.server = None
        self.ready = ready or (lambda: True)

    async def start(self):
        if self.port <= 0:
            return
        self.server = await asyncio.start_server(self._serve, '0.0.0.0', self.port)
        await self.registry.set(BOTS_UP, 1.0)

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _serve(self, reader, writer):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode('latin-1').split()
            path = parts[1] if len(parts) >= 2 else '/'
            # Drain headers; requests are tiny and have no body.
            header_bytes = len(request_line)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                header_bytes += len(line)
                if header_bytes > 8192:
                    return
                if line in (b'', b'\r\n', b'\n'):
                    break
            body, status = self._body(path)
            content_type = (b'text/plain; version=0.0.4; charset=utf-8'
                            if path == '/metrics'
                            else b'text/plain; charset=utf-8')
            writer.write(
                f'HTTP/1.1 {status}\r\n'.encode()
                + b'Content-Type: ' + content_type + b'\r\n'
                + f'Content-Length: {len(body)}\r\n'.encode()
                + b'Connection: close\r\n\r\n' + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass

    def _body(self, path):
        if path == '/healthz':
            return b'ok', '200 OK'
        if path == '/readyz':
            return (b'ready', '200 OK') if self.ready() else (b'connecting', '503 Service Unavailable')
        if path == '/metrics':
            return self.registry.render().encode(), '200 OK'
        return b'not found\n', '404 Not Found'

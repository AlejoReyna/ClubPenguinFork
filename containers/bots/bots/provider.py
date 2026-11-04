"""Dialogue providers: Moonshot/Kimi API or a local simulated fallback.

The model only writes short chat text. It receives a plain list of messages,
gets no tool definitions and can never execute anything in the game, the
cluster or the database: the only move from a reply is publishing it after
sanitization. HTTP uses a bounded async transport: cancellation closes its socket
without blocking the packet reader or leaving worker threads in flight.

The request shape was checked against the published Kimi OpenAPI:
- endpoint: POST {base_url}/chat/completions with Bearer API key;
- model ``kimi-k2.6`` accepts ``thinking: {"type": "disabled"}`` for short
  answers; ``"auto"`` omits the field for model families without it;
- no sampling parameters are overridden;
- ``max_completion_tokens`` bounds the reply length.
"""
import asyncio
from collections import deque
import json
import random
from .http_client import post_json

from . import metrics as metrics_mod

BASE_PATH = '/chat/completions'
RETRIABLE_KINDS = frozenset({'http_429', 'http_5xx', 'network'})


class ProviderError(Exception):
    """Provider failure classification; ``kind`` is a metric label."""

    KINDS = ('timeout', 'network', 'http_429', 'http_5xx', 'http_4xx', 'empty', 'budget', 'budget_state')

    def __init__(self, kind, detail=''):
        super().__init__(f'{kind}: {detail}')
        if kind not in self.KINDS:
            raise ValueError(f'unknown provider error kind {kind}')
        self.kind = kind


class MoonshotClient:
    """OpenAI-compatible chat.completions client for the Kimi API.

    Retries are bounded (429/5xx/network only), concurrency is gated by a
    semaphore, and every request is timed out. Token usage is exposed when the
    provider reports it.
    """

    def __init__(self, base_url, api_key, model='kimi-k2.6',
                 max_completion_tokens=120, timeout=30.0, retries=2,
                 retry_wait=2.0, thinking='disabled', max_concurrent=2,
                 registry=None, metrics=None, budget=None):
        if not api_key:
            raise ValueError('Moonshot API key is required in api mode')
        self.base_url = base_url.rstrip('/')
        self.api_key = str(api_key)
        self.model = model
        self.max_completion_tokens = int(max_completion_tokens)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.retry_wait = float(retry_wait)
        self.thinking = thinking
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        # Metrics facade for this client: never None in the service.
        self.metrics = metrics
        self.budget = budget

    def _payload(self, messages):
        payload = {
            'model': self.model,
            'messages': messages,
            'max_completion_tokens': self.max_completion_tokens,
        }
        if self.thinking in ('enabled', 'disabled'):
            payload['thinking'] = {'type': self.thinking}
        return payload

    async def _request(self, messages):
        attempt = 0
        while True:
            attempt += 1
            started = _loop_time()
            try:
                if self.budget is not None:
                    try:
                        permitted = self.budget.reserve()
                    except (OSError, ValueError, TypeError):
                        raise ProviderError('budget_state', 'budget ledger unavailable') from None
                    if not permitted:
                        raise ProviderError('budget', 'global request quota reached')
                status, body = await asyncio.wait_for(
                    post_json(self.base_url + BASE_PATH, 'Bearer ' + self.api_key,
                              json.dumps(self._payload(messages), ensure_ascii=True).encode()),
                    timeout=self.timeout)
                if status != 200:
                    raise ProviderError(_classify_http(status), 'provider rejected request')
                parsed = self._parse(body)
                if self.metrics:
                    await self.metrics.provider_ok(_loop_time() - started)
                return parsed
            except asyncio.TimeoutError:
                error = ProviderError('timeout', 'request exceeded timeout')
            except (OSError, asyncio.IncompleteReadError) as cause:
                error = ProviderError('network', 'connection failed')
            except (ValueError, UnicodeError, asyncio.LimitOverrunError):
                error = ProviderError('empty', 'invalid or oversized response')
            except ProviderError as cause:
                error = cause
            if self.metrics:
                await self.metrics.provider_error(error.kind)
            if error.kind in RETRIABLE_KINDS and attempt <= self.retries:
                await asyncio.sleep(_jittered(self.retry_wait, attempt))
                continue
            raise error

    def _parse(self, body):
        document = json.loads(body.decode('utf-8'))
        if not isinstance(document, dict):
            raise ProviderError('empty', 'unexpected response shape')
        choices = document.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError('empty', 'missing choices')
        message = choices[0].get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), str) or not message['content'].strip():
            raise ProviderError('empty', 'missing content')
        usage = document.get('usage') or {}
        if not isinstance(usage, dict):
            usage = {}
        return message['content'], usage

    async def complete(self, messages):
        """Return (reply_text, usage) or raise ProviderError."""
        async with self.semaphore:
            reply, usage = await self._request(messages)
        if self.metrics and usage:
            def count(key):
                try:
                    return max(0, int(usage.get(key) or 0))
                except (ValueError, TypeError, OverflowError):
                    return 0
            prompt = count('prompt_tokens')
            completion = count('completion_tokens')
            if prompt:
                await self.metrics.add(metrics_mod.PROVIDER_PROMPT_TOKENS, prompt)
            if completion:
                await self.metrics.add(metrics_mod.PROVIDER_COMPLETION_TOKENS, completion)
        return reply, usage


class SimulatedProvider:
    """Deterministic stand-in used by tests: no network, no balance spent.

    Replies never echo player input. ``fail_kinds`` replays a failure scene:
    call 1 (index 0) raises fail_kinds[0], call 2 fail_kinds[1], and so on.
    """

    DEFAULT_REPLIES = (
        'Bienvenido a la isla del laboratorio. Pasea por el Muelle y saluda.',
        'Por el pueblo hay un salon de actividades; entre todos llegamos lejos.',
        'Si buscas aventuras, la agencia de espies guarda misiones secretas.',
    )

    def __init__(self, replies=None, latency=0.0, fail_kinds=None,
                 model='kimi-k2.6', max_completion_tokens=120, record_messages=True):
        self.replies = tuple(replies or self.DEFAULT_REPLIES)
        self.latency = float(latency)
        self.fail_kinds = list(fail_kinds or [])
        self.calls = 0
        self.messages_received = deque(maxlen=16)
        self.record_messages = record_messages
        self.model = model

    def next_reply(self):
        """Deterministic: fail_kinds replay first, then replies cycle in order."""
        if self.calls <= len(self.fail_kinds):
            # calls was already advanced past this failure index.
            raise ProviderError(self.fail_kinds[self.calls - 1], 'simulated failure')
        index = (self.calls - 1 - len(self.fail_kinds)) % len(self.replies)
        return self.replies[index]

    async def complete(self, messages):
        self.calls += 1
        if self.record_messages:
            self.messages_received.append([dict(item) for item in messages])
        if self.latency:
            await asyncio.sleep(self.latency)
        return (self.next_reply(),
                {'prompt_tokens': 64, 'completion_tokens': 12})


def _loop_time():
    return asyncio.get_running_loop().time()


def _jittered(base, attempt):
    """Backoff jitter so sibling failures do not retry in lockstep."""
    cap = base * min(4.0, 2 ** max(0, attempt - 1))
    return random.uniform(0.3, 1.0) * cap


def _classify_http(status):
    if status == 429:
        return 'http_429'
    if 500 <= status <= 599:
        return 'http_5xx'
    return 'http_4xx'

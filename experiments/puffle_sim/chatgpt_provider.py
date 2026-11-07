"""Real provider using the owner's ChatGPT plan; same interface as SimulatedProcess.

evaluate(context) -> (decision, text). No tools, store=false, stream=true and
no sampling parameters. Chat text, tokens and raw provider errors are never
logged. Output is cut with engine.safe_text before it leaves this module.
"""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request

from .chatgpt_auth import AuthError, ChatGPTAuth, RESOURCE
from .engine import QuotaExhausted, safe_text

RESPONSES_URL = RESOURCE + '/responses'
MODELS_URL = RESOURCE + '/models'
INSTRUCTIONS = ('Eres un puffle azul de Club Penguin que vive en el iglú de tu dueño. '
                'Responde en español con una sola frase amable de máximo 48 caracteres, '
                'sin código, enlaces ni HTML. Si el mensaje no requiere respuesta, '
                'responde exactamente (silencio).')
SILENCE = '(silencio)'
TEXT_LIMIT = 120


class ProviderError(Exception):
    pass


class Retryable(ProviderError):
    pass


def stream_http(url, headers, body, timeout=30):
    """Yields (status, iterator_of_lines). Default transport, no logging."""
    req = urllib.request.Request(url, data=body, method='POST', headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, iter(e.read().splitlines())
    return resp.status, resp


def build_request(model, context, allow_actions=False):
    items = []
    for role, text in context[-12:]:
        clean = safe_text(text, 400)
        if clean:
            items.append({'role': 'user' if role == 'player' else 'assistant', 'content': clean})
    request = {'model': model, 'instructions': INSTRUCTIONS, 'input': items,
               'store': False, 'stream': True}
    if allow_actions:
        from .computer_tools import TOOL
        request['tools'] = [TOOL]
        request['instructions'] += (' Si el dueño pide abrir una aplicación disponible, solicita '
                                    'la función. Nunca afirmes que ya se abrió antes de confirmarlo.')
    return request


def _error_code(lines):
    raw = b''.join(line if isinstance(line, bytes) else line.encode() for line in lines)[:4096]
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    err = data.get('error') if isinstance(data, dict) else None
    return err.get('code') if isinstance(err, dict) else None


def parse_stream(lines, allow_actions=False):
    """Returns final text only if response.completed arrives."""
    parts, completed, actions = [], False, []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode('utf-8', 'replace')
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            break
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            parts.append(str(event.get('delta', '')))
            if sum(len(p) for p in parts) > 4000:
                raise ProviderError('salida demasiado larga')
        elif allow_actions and kind == 'response.output_item.done':
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') == 'function_call':
                actions.append(item)
                if len(actions) > 1:
                    raise ProviderError('demasiadas acciones')
        elif kind == 'response.completed':
            completed = True
            break
        elif kind in ('response.failed', 'error', 'response.incomplete'):
            err = (event.get('response') or {}).get('error') or event.get('error') or {}
            code = err.get('code') if isinstance(err, dict) else None
            if code == 'subscription_sharing_usage_limit_exceeded':
                raise QuotaExhausted()
            if code in ('subscription_sharing_usage_unavailable', 'subscription_sharing_user_unavailable'):
                raise Retryable('uso no disponible')
            raise ProviderError('respuesta fallida')
    if not completed:
        raise ProviderError('stream sin response.completed')
    return (''.join(parts), actions) if allow_actions else ''.join(parts)


class ChatGPTProvider:
    def __init__(self, auth=None, transport=None, http=None, retries=2, backoff=1.0, sleep=time.sleep,
                 allow_actions=False):
        self.auth = auth or ChatGPTAuth()
        self.transport = transport or stream_http
        self.http = http or self.auth.http
        self.retries, self.backoff, self.sleep = retries, backoff, sleep
        self.model = None
        self.allow_actions = allow_actions
        # Cancelling an asyncio wrapper cannot stop a blocking urllib request.
        # Keep the underlying call exclusive until its worker really finishes.
        self._inflight = threading.Lock()

    async def evaluate(self, context):
        return await asyncio.to_thread(self._evaluate_exclusive, tuple(context))

    def _evaluate_exclusive(self, context):
        if not self._inflight.acquire(blocking=False):
            raise ProviderError('proveedor ocupado')
        try:
            return self._evaluate(context)
        finally:
            self._inflight.release()

    def _headers(self, token):
        return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                'Accept': 'text/event-stream'}

    def _pick_model(self, token):
        if self.model:
            return self.model
        status, raw = self.http('GET', MODELS_URL, {'Authorization': f'Bearer {token}'}, None)
        if status != 200:
            raise ProviderError('no se pudo listar modelos')
        models = json.loads(raw).get('models', [])
        slug = next((m.get('slug') for m in models if m.get('visibility') == 'list' and m.get('slug')), None)
        if not slug:
            raise ProviderError('no hay modelos disponibles')
        self.model = slug
        return slug

    def _evaluate(self, context):
        if self.auth.paused():
            raise QuotaExhausted()
        try:
            token = self.auth.access_token()
        except AuthError:
            raise ProviderError('no conectado') from None
        model = self._pick_model(token)
        body = json.dumps(build_request(model, context, self.allow_actions), ensure_ascii=False).encode()
        for attempt in range(self.retries + 1):
            try:
                status, lines = self.transport(RESPONSES_URL, self._headers(token), body)
                if status == 200:
                    parsed = parse_stream(lines, self.allow_actions)
                    break
                code = _error_code(lines)
                if status == 429 and code == 'subscription_sharing_usage_limit_exceeded':
                    raise QuotaExhausted()
                if status == 503:
                    raise Retryable('uso no disponible')
                if status == 401 and attempt == 0:
                    token = self.auth.access_token(force_refresh=True)
                    continue
                raise ProviderError(f'HTTP {status}')
            except QuotaExhausted:
                self.auth.set_paused(True)
                raise
            except Retryable:
                if attempt >= self.retries:
                    raise ProviderError('uso no disponible, reintentos agotados') from None
                self.sleep(self.backoff * (2 ** attempt))
        else:
            raise ProviderError('sin respuesta')
        if self.allow_actions:
            from .computer_tools import requested_action
            text, actions = parsed
            action = requested_action(actions[0], context) if actions else None
            if actions:
                if not action:
                    return {'reply': True, 'text': 'No puedo hacer esa acción.'}
                return {'reply': True, 'text': '', 'action': action}
        else:
            text = parsed
        text = safe_text(text, TEXT_LIMIT)
        if not text or text == SILENCE:
            return False, ''
        return True, text

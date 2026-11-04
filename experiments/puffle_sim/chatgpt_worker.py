"""One inference in a short-lived host process; errors never reveal content."""
import asyncio
import json
import sys

from .chatgpt_process import MAX_INPUT
from .chatgpt_provider import ChatGPTProvider
from .engine import QuotaExhausted


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT or not raw.endswith(b'\n'):
            raise ValueError('invalid request')
        data = json.loads(raw)
        # The relay makes at most one inference attempt per player message.
        actions = sys.argv[1:] == ['--local-actions']
        answer = asyncio.run(ChatGPTProvider(retries=0, allow_actions=actions).evaluate(
            tuple(tuple(x) for x in data['context'])))
        if isinstance(answer, dict):
            result = answer
        else:
            decision, text = answer
            result = {'reply': decision is True, 'text': text}
    except QuotaExhausted:
        result = {'error': 'quota'}
    except Exception:
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

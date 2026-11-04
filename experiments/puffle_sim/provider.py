"""Launch a fresh, text-only simulated worker for each evaluation."""
import asyncio
import json
from pathlib import Path
import sys


class SimulatedProcess:
    async def evaluate(self, context):
        data = json.dumps({'context': context}, ensure_ascii=True).encode() + b'\n'
        if len(data) > 16384:
            raise ValueError('context too large')
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-I', str(Path(__file__).with_name('worker.py')),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={})
        try:
            out, _ = await proc.communicate(data)
            if proc.returncode != 0 or len(out) > 2048:
                raise ValueError('simulator failed')
            result = json.loads(out)
            if type(result.get('reply')) is not bool or not isinstance(result.get('text'), str):
                raise ValueError('invalid simulator result')
            return result['reply'], result['text']
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


def make_provider(kind='simulator', **kwargs):
    """Explicit provider choice; the default stays the offline simulator."""
    if kind == 'simulator':
        return SimulatedProcess()
    if kind == 'chatgpt':
        from .chatgpt_provider import ChatGPTProvider
        return ChatGPTProvider(**kwargs)
    raise ValueError('unknown provider')

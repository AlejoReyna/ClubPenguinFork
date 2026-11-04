"""Run one host-side inference in a process that can be killed on exit.

This is still a single-account local relay, not a multi-user credential store.
Context goes over stdin, never arguments or logs.
"""
import asyncio
import contextlib
import json
import os
import signal
import sys

from .engine import QuotaExhausted

MAX_INPUT = 16384
MAX_OUTPUT = 2048


class ProcessChatGPTProvider:
    def __init__(self, command=None, allow_actions=False):
        self.command = tuple(command or (sys.executable, '-m', 'experiments.puffle_sim.chatgpt_worker',
                                          *(['--local-actions'] if allow_actions else [])))
        self.allow_actions = allow_actions

    async def evaluate(self, context):
        data = json.dumps({'context': context}, ensure_ascii=True).encode() + b'\n'
        if len(data) > MAX_INPUT:
            raise ValueError('context too large')
        proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = await proc.communicate(data)
            if proc.returncode != 0 or len(out) > MAX_OUTPUT:
                raise ValueError('inference worker failed')
            result = json.loads(out)
            if not isinstance(result, dict):
                raise ValueError('invalid worker response')
            if result == {'error': 'quota'}:
                raise QuotaExhausted()
            if (self.allow_actions and set(result) == {'reply', 'text', 'action'} and
                    result['reply'] is True and result['text'] == '' and isinstance(result['action'], dict)):
                return result
            if (set(result) != {'reply', 'text'} or type(result['reply']) is not bool
                    or not isinstance(result['text'], str)):
                raise ValueError('invalid worker response')
            return result['reply'], result['text']
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()

"""One cancellable device job; terminating its process group also stops local tools."""
import argparse
import asyncio
import json
import sys

from experiments.puffle_sim.host_relay import Relay, MAX_REQUEST
from experiments.puffle_sim.computer_tools import confirm_and_open
from .companion import provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--music-mac', action='store_true')
    group.add_argument('--music-safari-window-id', type=int)
    group.add_argument('--spotify', action='store_true')
    parser.add_argument('--simulated', action='store_true')
    parser.add_argument('--allow-local-actions', action='store_true')
    args = parser.parse_args()
    try:
        data = json.loads(sys.stdin.buffer.readline(MAX_REQUEST + 1))
        relay = Relay(provider(args), 'device-worker', log=lambda _: None,
                      action_handler=confirm_and_open if args.allow_local_actions else None)
        raw = json.dumps({'secret': 'device-worker', 'context': data['context']}).encode()
        answer = asyncio.run(relay.answer(raw)) or {'reply': False, 'text': ''}
        sys.stdout.write(json.dumps(answer, ensure_ascii=True) + '\n')
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

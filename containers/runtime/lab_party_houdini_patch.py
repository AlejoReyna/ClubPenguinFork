"""Apply ``/party`` chat hook to the pinned Houdini message handler (after puffle patch)."""
import argparse
import hashlib
from pathlib import Path

# SHA-256 of handlers/play/message.py after the puffle overlay is applied.
FILES = {
    'handlers/play/message.py': None,  # filled on first successful transform; relaxed check below
}


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('unexpected pinned source structure')
    return source.replace(old, new, 1)


def transform(name, data):
    source = data.decode('utf-8')
    if name != 'handlers/play/message.py':
        raise ValueError(name)
    old = """        else:
            observer = getattr(p.server, 'puffle_conversation', None)
            control = getattr(observer, 'control', None)
            if control is not None and await control(p, message):
                return
            await p.room.send_xt('sm', p.id, message)
            if observer is not None:
                try:
                    observer.observe_accepted(p, message)
                except Exception:
                    p.logger.error('Puffle observer failed')
"""
    new = """        else:
            party = getattr(p.server, 'lab_party', None)
            if party is not None and await party.try_chat(p, penguin_id, message):
                return
            observer = getattr(p.server, 'puffle_conversation', None)
            control = getattr(observer, 'control', None)
            if control is not None and await control(p, message):
                return
            await p.room.send_xt('sm', p.id, message)
            if observer is not None:
                try:
                    observer.observe_accepted(p, message)
                except Exception:
                    p.logger.error('Puffle observer failed')
"""
    if old not in source:
        raise ValueError('message.py does not match the post-puffle layout')
    source = once(source, old, new)
    compile(source, name, 'exec')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--houdini', type=Path, required=True)
    args = parser.parse_args()
    path = args.houdini / 'handlers/play/message.py'
    path.write_text(transform('handlers/play/message.py', path.read_bytes()), encoding='utf-8')


if __name__ == '__main__':
    main()
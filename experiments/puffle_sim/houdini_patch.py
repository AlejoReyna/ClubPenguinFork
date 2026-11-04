"""Create opt-in Houdini test overlays for the pinned upstream revision.

No running process, source checkout or image is modified by this module.
Installing a bridge and arranging trusted player opt-in remain separate work.
"""
import argparse
import hashlib
from pathlib import Path


FILES = {
    'handlers/play/message.py': '454c96aede4b106332699203f5a1f5db9bd2c2e77390a2c52e39c372e0b6544a',
    'data/room.py': '845ef338e62e364cc2f664248034f266bb496d70444d3691b74461fac024744e',
    'spheniscidae.py': 'ea7714adfbfddb982604717205d665ec08033a8ffbf8bd23343a5cde9c51a601',
}


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('unexpected pinned source structure')
    return source.replace(old, new, 1)


def transform(name, data):
    if name not in FILES or hashlib.sha256(data).hexdigest() != FILES[name]:
        raise ValueError('unrecognized upstream source')
    source = data.decode('utf-8')
    if name == 'handlers/play/message.py':
        old = """        else:
            await p.room.send_xt('sm', p.id, message)

        p.logger.info(f'{p.username} said \\'{message}\\' in room \\'{p.room.name}\\'')
"""
        new = """        else:
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
        source = once(source, old, new)
        old_log = "        p.logger.warn(f'{p.username} tried to use a command that does not exist \\'{message}\\'')\n"
        source = once(source, old_log, '')
    elif name == 'data/room.py':
        old = "    async def remove_penguin(self, p):\n        if not p.stealth_moderator:"
        new = """    async def remove_penguin(self, p):
        observer = getattr(p.server, 'puffle_conversation', None)
        if observer is not None:
            observer.leave(p)
        if not p.stealth_moderator:"""
        source = once(source, old, new)
    else:
        # Network packets contain chat, login material and puffle text. Never
        # write their contents to the debug log, even with debug enabled.
        for line in ("            self.logger.debug(f'Outgoing data: {data}')\n",
                     "        self.logger.debug(f'Received XT data: {data}')\n",
                     "        self.logger.debug(f'Received XML data: {data}')\n"):
            source = once(source, line, '')
        old = "    async def _client_disconnected(self):\n        if self.peer_name in self.server.peers_by_ip:"
        new = """    async def _client_disconnected(self):
        observer = getattr(self.server, 'puffle_conversation', None)
        if observer is not None and self.joined_world:
            observer.disconnect(self)
        if self.peer_name in self.server.peers_by_ip:"""
        source = once(source, old, new)
    compile(source, name, 'exec')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--houdini', type=Path, required=True, help='upstream houdini package directory')
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    args = parser.parse_args()
    # Validate everything before creating any output.
    overlays = {name: transform(name, (args.houdini / name).read_bytes()) for name in FILES}
    args.output.mkdir(parents=True, exist_ok=False)
    for name, source in overlays.items():
        path = args.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(source)
    print('Created isolated Houdini overlays; no running service was modified.')


if __name__ == '__main__':
    main()

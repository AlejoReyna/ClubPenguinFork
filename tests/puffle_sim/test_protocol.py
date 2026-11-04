import os
"""Exercise packet framing with the real Houdini serializer in the image."""
import unittest
from pathlib import Path
from types import SimpleNamespace

from houdini.spheniscidae import Spheniscidae
from experiments.puffle_sim.client.protocol import puffle_talk_fields
from experiments.puffle_sim.houdini_patch import transform


class Sink:
    def __init__(self):
        self.lines = []

    async def send_line(self, line):
        self.lines.append(line)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_cannot_add_fields_or_packets(self):
        sink = Sink()
        fields = puffle_talk_fields(2101, 3, 'hola%xt%sm%|\x00<script>')
        await Spheniscidae.send_xt(sink, *fields)
        self.assertEqual(sink.lines, ['%xt%puffletalk%-1%2101%3%hola xt sm script%'])
        self.assertEqual(len(sink.lines[0].split('%')), 8)

    async def test_invalid_values_do_not_serialize(self):
        for room, pet, message in [(0, 1, 'hola'), (2, True, 'hola'), (2, 3, '%|\x00')]:
            with self.assertRaises(ValueError):
                puffle_talk_fields(room, pet, message)

    async def test_network_debug_does_not_log_packet_contents(self):
        source = (Path(os.environ.get('PUFFLE_UPSTREAM', '/app/houdini/houdini')) / 'spheniscidae.py').read_bytes()
        namespace = {'__name__': 'puffle_private_network'}
        exec(compile(transform('spheniscidae.py', source), 'spheniscidae.py', 'exec'), namespace)
        messages = []
        logger = SimpleNamespace(debug=lambda *a: messages.append(a),
                                 warn=lambda *a: messages.append(a),
                                 info=lambda *a: messages.append(a))
        class Writer:
            def get_extra_info(self, _): return 'synthetic-peer'
            def is_closing(self): return False
            def write(self, data): pass
        server = SimpleNamespace(logger=logger, peers_by_ip={}, xt_listeners={})
        conn = namespace['Spheniscidae'](server, None, Writer())
        await conn.send_line('%xt%puffletalk%-1%private-output%')
        await conn._Spheniscidae__handle_xt_data('%xt%s%unknown%-1%private-input%')
        await conn._Spheniscidae__handle_xml_data('<unexpected secret="private-xml"/>')
        self.assertNotIn('private-output', str(messages))
        self.assertNotIn('private-input', str(messages))
        self.assertNotIn('private-xml', str(messages))

"""Sanitation and mention detection must never allow protocol injection."""
import unittest

from bots.text import mentions, sanitize


class SanitizeTests(unittest.TestCase):

    def test_removes_every_protocol_delimiter(self):
        dirty = 'hola %xt%e%0% canal|id|extra\x00 fin'
        clean = sanitize(dirty)
        for forbidden in ('%', '|', '\x00'):
            self.assertNotIn(forbidden, clean)

    def test_control_characters_become_spaces(self):
        self.assertEqual(sanitize('linea\r\ncontinua\tjunta'), 'linea continua junta'.replace('   ', ' ').strip())
        text = sanitize('uno\x01dos\x02tres')
        self.assertTrue(text.startswith('uno'), text)
        self.assertNotIn('\x01', text)

    def test_drops_leading_command_prefixes(self):
        self.assertEqual(sanitize('!ayuda'), 'ayuda')
        self.assertEqual(sanitize('.comando'), 'comando')
        self.assertEqual(sanitize('!!x'), 'x')
        self.assertEqual(sanitize('? como ando'), 'como ando')

    def test_command_prefix_mid_sentence_stays(self):
        self.assertEqual(sanitize('ve a la .isla?'), 've a la .isla?')

    def test_collapses_whitespace_and_trims(self):
        self.assertEqual(sanitize('   hola    mundo  '), 'hola mundo')

    def test_truncates_to_limit(self):
        text = sanitize('a' * 300, max_length=40)
        self.assertLessEqual(len(text), 40)

    def test_non_string_becomes_string(self):
        self.assertEqual(sanitize(10_000), '10000')

    def test_empty_after_cleaning(self):
        self.assertEqual(sanitize('%%%|||'), '')
        self.assertEqual(sanitize(''), '')
        self.assertEqual(sanitize('   '), '')

    def test_accented_text_survives(self):
        self.assertEqual(sanitize('¡Hola! Vamos al café.'), '¡Hola! Vamos al café.')

    def test_c1_and_format_characters_cannot_hide_a_command_prefix(self):
        self.assertEqual(sanitize('\u200b!comando\x81%|\x00'), 'comando')
        for value in range(0x80, 0xa0):
            self.assertNotIn(chr(value), sanitize('a' + chr(value) + 'b'))


class MentionTests(unittest.TestCase):

    def test_matches_nickname_and_aliases_case_insensitive(self):
        names = ['GuiaIA', 'Guia']
        self.assertTrue(mentions('hola GuiaIA, como ando', names))
        self.assertTrue(mentions('GUÍA de la isla', names))
        self.assertTrue(mentions('una pregunta para GuiA amigos', names))

    def test_no_false_mention(self):
        self.assertFalse(mentions('hola mundo', ['GuiaIA']))
        self.assertFalse(mentions('guitarra para todos', ['Guia']))
        self.assertFalse(mentions('', ['GuiaIA']))

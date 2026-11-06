"""Output filter: links, e-mail, digit runs and the optional Houdini-style blocklist."""
import tempfile
import unittest
from pathlib import Path

from bots import safety
from bots.safety import OutputFilter, check_local_line, load_blocklist


class DataFreeChecksTests(unittest.TestCase):
    """What is refused with no blocklist at all."""

    def setUp(self):
        self.filter = OutputFilter()

    def test_normal_island_talk_passes(self):
        for line in ('Hola crack, a la pista!', 'Tengo 3 bolas de nieve', 'Empieza a las 18:00',
                     'Mi mejor tiempo fueron 1200 puntos', 'Vamos al 2do piso', 'Hoy es 05/11/2026',
                     'Ven a la pizzeria... esta rica', 'Un, dos, tres, cuatro, cinco, seis, siete'):
            self.assertIsNone(self.filter.check(line), line)

    def test_links_and_domains(self):
        for line in ('visita http://ejemplo.com ya', 'HTTPS://EXAMPLE.ORG/x', 'entra a www.ejemplo.com',
                     'www . ejemplo', 'ejemplo.com', 'mi-sitio.gg/abc', 'discord.gg', 'sub.dominio.mx hoy',
                     'ftp://x', 'ejemplo punto com', 'ejemplo dot com', 'ejemplo [.] com', 'ejemplo (punto) com'):
            self.assertEqual(self.filter.check(line), safety.REASON_URL, line)

    def test_email_and_handles(self):
        for line in ('escribe a juan@mail.com', 'sigueme @juan_22', 'juan arroba gmail',
                     'juan [at] gmail', 'juan (at) gmail', 'JUAN.PEREZ+x@Correo.Net'):
            self.assertEqual(self.filter.check(line), safety.REASON_EMAIL, line)

    def test_long_digit_runs_with_separators(self):
        for line in ('llama al 5512345678', 'mi numero es 55 1234 5678', '55-1234-5678',
                     '(555) 123-4567', 'codigo 123456', 'tarjeta 4111 1111 1111 1111', '+52 55 1234 5678'):
            self.assertEqual(self.filter.check(line), safety.REASON_DIGITS, line)
        # five digits and separate short numbers are fine
        for line in ('son 12345 monedas', '10 y 20 y 30', 'del 1 al 5'):
            self.assertIsNone(self.filter.check(line), line)

    def test_digit_threshold_is_configurable(self):
        strict = OutputFilter(max_digits=4)
        self.assertEqual(strict.check('mi clave 1234'), safety.REASON_DIGITS)
        self.assertIsNone(OutputFilter(max_digits=9).check('codigo 123456'))
        with self.assertRaises(ValueError):
            OutputFilter(max_digits=1)

    def test_reasons_are_bounded_labels(self):
        for reason in safety.REASONS:
            self.assertRegex(reason, r'^[a-z]+$')


class BlocklistTests(unittest.TestCase):

    def test_matches_whole_words_like_houdini_but_more_strictly(self):
        rules = OutputFilter(['tonto'])
        # Houdini would match only the bare token "tonto"; these also match here.
        for line in ('eres tonto', 'ERES TONTO', 'Eres tonto!', '¡tonto!', 'tontó', 'eres, tonto, jaja'):
            self.assertEqual(rules.check(line), safety.REASON_BLOCKLIST, line)
        # but not words that merely contain it
        for line in ('tontolaba', 'atontado', 'destonto'):
            self.assertIsNone(rules.check(line), line)

    def test_phrases_match_on_word_boundaries(self):
        rules = OutputFilter(['palabra fea'])
        self.assertEqual(rules.check('dijo una Palabra   FEA hoy'), safety.REASON_BLOCKLIST)
        self.assertEqual(rules.check('palabra, fea'), safety.REASON_BLOCKLIST)
        self.assertIsNone(rules.check('palabra bonita fea'))
        self.assertIsNone(rules.check('una palabrafea'))

    def test_no_blocklist_means_no_blocklist_hits(self):
        self.assertIsNone(OutputFilter().check('eres tonto'))
        self.assertEqual(len(OutputFilter(['a', 'b c', '  ', ''])), 2)

    def test_load_blocklist_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'blocklist.txt'
            path.write_text('# exported from chat_filter_rule\n\nTonto\n  Palabra   Fea  # inline\nÁrbol\ntonto\n',
                            encoding='utf-8')
            self.assertEqual(load_blocklist(path), ('tonto', 'palabra fea', 'arbol'))
            rules = OutputFilter.from_file(path)
            self.assertEqual(rules.check('un arbol'), safety.REASON_BLOCKLIST)
            self.assertEqual(len(rules), 3)

    def test_a_configured_but_missing_blocklist_is_an_error(self):
        with self.assertRaises(OSError):
            OutputFilter.from_file('/nonexistent/blocklist.txt')


class LocalLineTests(unittest.TestCase):
    """Operator-written lines (greetings, fallbacks) are checked when cards load."""

    def test_accepts_a_plain_short_line(self):
        self.assertIsNone(check_local_line('Ahora no, que suena mi cancion!', 54))

    def test_rejects_bad_lines_with_a_reason(self):
        cases = {
            '': 'empty',
            'x' * 60: 'longer',
            'Ahora no, ¡que suena!': 'ASCII',
            'hola | adios': 'delimiter',
            '100% seguro': 'delimiter',
            'visita ejemplo.com': 'filter',
            'llama al 5512345678': 'filter',
        }
        for line, fragment in cases.items():
            problem = check_local_line(line, 54)
            self.assertIsNotNone(problem, line)
            self.assertIn(fragment, problem)

    def test_uses_the_given_filter(self):
        rules = OutputFilter(['feo'])
        self.assertIn('blocklist', check_local_line('que feo dia', 54, rules))
        self.assertIsNone(check_local_line('que feo dia', 54))


if __name__ == '__main__':
    unittest.main()

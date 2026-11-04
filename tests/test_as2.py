"""AS2 helpers: auth formulas (verified against vendor/crypto.py) and framing."""
import unittest

from bots import as2


def vendored_login_password(password, rndk):
    # Golden vectors generated with upstream Crypto.get_login_hash on the
    # uppercase MD5 of the original password, as required by legacy login.
    vectors = {
        ('secreta123', '01abc2def3456789'): '67cf4eafbe8d876432fc8dd000ce7b3a',
        ('otrMasLarga#4', 'A63b82c10d97e5f2'): '73a6d1a6821ec7863a1c542a87f88b6e',
    }
    return vectors[(password, rndk)]


def vendored_world_token(login_key, rndk):
    def encrypt_password(value):
        value = as2.digest(value)
        return value[16:32] + value[0:16]
    return encrypt_password(login_key + rndk) + login_key


class AuthTests(unittest.TestCase):

    def test_login_password_matches_vendor_implementation(self):
        for password, rndk in (('secreta123', '01abc2def3456789'),
                               ('otrMasLarga#4', 'A63b82c10d97e5f2')):
            built = as2.login_server_password(password, rndk)
            self.assertEqual(built, vendored_login_password(password, rndk))
            self.assertEqual(len(built), 32)
            self.assertTrue(all(char in '0123456789abcdef' for char in built))

    def test_world_token_embeds_the_login_key(self):
        token = as2.world_login_password('k1a2b3c4', '7788aabb')
        self.assertEqual(token, vendored_world_token('k1a2b3c4', '7788aabb'))
        self.assertTrue(token.endswith('k1a2b3c4'))
        self.assertEqual(len(token), 32 + len('k1a2b3c4'))

    def test_cdata_escapes_closing_bracket(self):
        self.assertEqual(as2._cdata('a]]>b'), '<![CDATA[a]]]]><![CDATA[>b]]>')


class FrameTests(unittest.TestCase):

    def test_parse_error_frame(self):
        frame = as2.Frame.parse('%xt%e%-1%101%')
        error = frame.as_error()
        self.assertIsNotNone(error)
        self.assertEqual(error.code, 101)
        self.assertTrue(error.auth_related)

    def test_parse_chat_frame(self):
        frame = as2.Frame.parse('%xt%sm%-1%17%hola a todos%')
        self.assertEqual(frame.op, 'sm')
        self.assertEqual(frame.args, ['17', 'hola a todos'])
        self.assertIsNone(frame.as_error())

    def test_parse_room_roster(self):
        frame = as2.Frame.parse('%xt%jr%-1%100%12|Nico|1|1|1|0|0|0|0|0|0|0|90|320|1|0|0|0%')
        self.assertEqual(frame.op, 'jr')
        self.assertEqual(len(frame.args), 2)

    def test_auth_related_codes_follow_the_vendor_list(self):
        auth = {100, 101, 150, 601, 602, 603, 900, 910, 911, 913}
        for code in auth:
            self.assertTrue(as2.ServerError(code).auth_related, code)
        for code in (7, 402, 500, 9000):
            self.assertFalse(as2.ServerError(code).auth_related, code)

    def test_framing_error_on_garbage(self):
        with self.assertRaises(as2.FramingError):
            as2.Frame.parse('nada que ver')


if __name__ == '__main__':
    unittest.main()

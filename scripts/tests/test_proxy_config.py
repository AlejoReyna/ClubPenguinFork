import unittest

import lablib


class ProxyConfigTests(unittest.TestCase):
    def test_clean_checkout_preserves_explicit_extra_hosts(self):
        cfg = lablib.load_config(environ={'CP_PROXY_EXTRA_HOSTS': 'localhost,127.0.0.1,192.168.7.20'},
                                 config_file='/nonexistent')
        self.assertEqual(cfg['CP_PROXY_EXTRA_HOSTS'].split(','), ['localhost', '127.0.0.1', '192.168.7.20'])

    def test_origin_list_rejects_urls_ports_and_header_injection(self):
        for value in ('https://host.test', 'host.test:443', 'host.test\r\nHeader:value'):
            with self.assertRaises(SystemExit):
                lablib.load_config(environ={'CP_PROXY_EXTRA_HOSTS': value}, config_file='/nonexistent')

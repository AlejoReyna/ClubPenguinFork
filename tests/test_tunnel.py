import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tunnel  # noqa: E402


class ControlPathTests(unittest.TestCase):
    def test_short_checkout_keeps_the_socket_next_to_the_repo(self):
        self.assertEqual(tunnel.control_path('/srv/lab/.local'), '/srv/lab/.local/pi-ssh.sock')

    def test_deep_checkout_falls_back_to_a_short_path_that_ssh_can_bind(self):
        deep = '/Users/alexis/.clubpenguin-lab/github-runner/_work/ClubPenguinFork/ClubPenguinFork/.local'
        with tempfile.TemporaryDirectory() as home:
            path = tunnel.control_path(deep, home)
            self.assertTrue(path.startswith(home))
            # ssh adds 17 random characters while binding; the total must stay under macOS's 104.
            self.assertLess(len(path) + 17, 104)
            self.assertEqual(path, tunnel.control_path(deep, home))  # stable across start/stop/status
            self.assertEqual(oct(Path(path).parent.stat().st_mode & 0o777), '0o700')

    def test_different_checkouts_get_different_sockets(self):
        deep = '/x' * 60
        with tempfile.TemporaryDirectory() as home:
            self.assertNotEqual(tunnel.control_path(deep + '/a', home), tunnel.control_path(deep + '/b', home))


if __name__ == '__main__':
    unittest.main()

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tunnel  # noqa: E402


def ssh_result(returncode, stderr=''):
    return subprocess.CompletedProcess(['ssh'], returncode, stdout='', stderr=stderr)


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


class OpenTunnelRetryTests(unittest.TestCase):
    """A Pi that misses SSH for a moment must not fail the whole pipeline."""

    def test_transient_failure_is_retried_until_success(self):
        attempts, sleeps = [], []
        outcomes = [ssh_result(255, 'ssh: connect to host 192.168.1.22 port 22: Operation timed out'),
                    ssh_result(0)]

        def run(command, capture_output, text):
            attempts.append(command)
            return outcomes[min(len(attempts) - 1, len(outcomes) - 1)]

        result = tunnel.open_tunnel(['ssh'], 'pi@192.168.1.22', tries=4, delay=20.0,
                                    sleep=sleeps.append, run=run)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [20.0])

    def test_exhausted_retries_return_the_last_failure(self):
        attempts = []

        def run(command, capture_output, text):
            attempts.append(command)
            return ssh_result(255, 'ssh: connect to host 192.168.1.22 port 22: Operation timed out')

        result = tunnel.open_tunnel(['ssh'], 'pi@192.168.1.22', tries=3, delay=20.0,
                                    sleep=lambda s: None, run=run)
        self.assertEqual(result.returncode, 255)
        self.assertEqual(len(attempts), 3)

    def test_authentication_failure_is_never_retried(self):
        attempts = []

        def run(command, capture_output, text):
            attempts.append(command)
            return ssh_result(255, 'pi@192.168.1.22: Permission denied (publickey).')

        result = tunnel.open_tunnel(['ssh'], 'pi@192.168.1.22', tries=4, delay=20.0,
                                    sleep=lambda s: None, run=run)
        self.assertEqual(result.returncode, 255)
        self.assertEqual(len(attempts), 1)

    def test_a_single_try_makes_exactly_one_attempt(self):
        attempts = []
        tunnel.open_tunnel(['ssh'], 'pi@192.168.1.22', tries=1,
                           sleep=lambda s: None,
                           run=lambda command, capture_output, text: attempts.append(command) or ssh_result(255))
        self.assertEqual(len(attempts), 1)


if __name__ == '__main__':
    unittest.main()

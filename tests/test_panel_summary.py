"""The game page's infrastructure panel summary: states, allow-lists and failure handling."""
import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('panel_summary', ROOT / 'containers/runtime/panel_summary.py')
panel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = panel
_spec.loader.exec_module(panel)

INTERNAL = 'http://prometheus.monitoring-lab.svc'


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def fetcher(available=4, desired=4, players=3, age=10, fail=False):
    calls = []

    def fetch(base, expression, timeout):
        calls.append(expression)
        if fail:
            raise panel.SourceUnavailable('down')
        if expression.startswith('time()'):
            return age
        if 'replicas_available' in expression:
            return available
        if 'spec_replicas' in expression:
            return desired
        if 'clubpenguin_players' in expression:
            return players
        raise AssertionError(expression)

    fetch.calls = calls
    return fetch


def summary(env=None, **kwargs):
    base = {'PROMETHEUS_URL': INTERNAL, 'PANEL_PROVIDER': 'Raspberry Pi', 'RELEASE_INFO_DIR': '/nonexistent'}
    base.update(env or {})
    clock = kwargs.pop('clock', Clock())
    return panel.Summary(env=base, now=clock, fetch=kwargs.pop('fetch', fetcher())), clock


class PanelSummaryTest(unittest.TestCase):
    def test_without_a_source_it_is_not_configured(self):
        result, _ = summary({'PROMETHEUS_URL': ''})
        self.assertEqual(result.prometheus()['state'], 'not_configured')

    def test_a_recent_complete_reading_is_operational(self):
        result, clock = summary()
        reading = result.prometheus()
        self.assertEqual(reading['state'], 'operational')
        self.assertEqual(reading['metrics'], [{'id': 'workloads', 'label': 'Cargas disponibles', 'value': '4 / 4'}])
        self.assertEqual(reading['lastReading'], clock.t)

    def test_players_are_hidden_unless_the_panel_is_authorised_to_show_them(self):
        hidden, _ = summary()
        self.assertNotIn('players', [m['id'] for m in hidden.prometheus()['metrics']])
        shown, _ = summary({'PANEL_SHOW_PLAYERS': 'true'})
        self.assertIn({'id': 'players', 'label': 'Jugadores', 'value': '3'}, shown.prometheus()['metrics'])

    def test_unavailable_workloads_are_never_called_operational(self):
        result, _ = summary(fetch=fetcher(available=3, desired=4))
        self.assertEqual(result.prometheus()['state'], 'degraded')

    def test_an_old_sample_is_stale(self):
        result, _ = summary(fetch=fetcher(age=900))
        self.assertEqual(result.prometheus()['state'], 'stale')

    def test_a_missing_metric_is_stale(self):
        result, _ = summary({'PANEL_SHOW_PLAYERS': 'true'}, fetch=fetcher(players=None))
        self.assertEqual(result.prometheus()['state'], 'stale')

    def test_a_dead_source_is_offline_and_exposes_no_values(self):
        result, clock = summary()
        result.prometheus()  # one good reading first
        good = clock.t
        clock.t += 60
        result.fetch = fetcher(fail=True)
        reading = result.prometheus()
        self.assertEqual(reading['state'], 'offline')
        self.assertEqual(reading['metrics'], [])
        self.assertEqual(reading['lastReading'], good)

    def test_a_real_unreachable_port_is_offline_quickly(self):
        result = panel.Summary(env={'PROMETHEUS_URL': 'http://127.0.0.1:9', 'PANEL_TIMEOUT': '1'})
        started = time.monotonic()
        self.assertEqual(result.prometheus()['state'], 'offline')
        self.assertLess(time.monotonic() - started, 3)

    def test_a_server_that_never_answers_times_out(self):
        class Silent(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(5)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Silent)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = panel.Summary(env={'PROMETHEUS_URL': f'http://127.0.0.1:{server.server_port}', 'PANEL_TIMEOUT': '1'})
            started = time.monotonic()
            self.assertEqual(result.prometheus()['state'], 'offline')
            self.assertLess(time.monotonic() - started, 3.5)
        finally:
            server.shutdown()

    def test_readings_are_cached(self):
        fetch = fetcher()
        result, clock = summary(fetch=fetch)
        result.build()
        first = len(fetch.calls)
        clock.t += 5
        result.build()
        self.assertEqual(len(fetch.calls), first)
        clock.t += 20
        result.build()
        self.assertGreater(len(fetch.calls), first)

    def test_only_approved_release_fields_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'commit').write_text('A1B2C3D4E5F60718293A4B5C6D7E8F9012345678\n')
            Path(directory, 'version').write_text('2026.11.3')
            Path(directory, 'image_tag').write_text('lab-006')
            Path(directory, 'registry').write_text('acr.example.net')
            found = panel.read_release(directory)
        self.assertEqual(found, {'commit': 'a1b2c3d4e5f6', 'version': '2026.11.3'})

    def test_a_malformed_commit_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'commit').write_text('<script>alert(1)</script>')
            self.assertEqual(panel.read_release(directory), {})

    def test_grafana_private_has_no_link(self):
        self.assertEqual(panel.grafana_summary('https://grafana.example.com', True), {'access': 'private', 'url': None})

    def test_grafana_accepts_only_https_without_credentials(self):
        self.assertEqual(panel.grafana_summary('https://grafana.example.com/d/x', False)['access'], 'link')
        for bad in ('http://grafana.example.com', 'https://u:p@grafana.example.com', 'javascript:alert(1)', '', 'grafana'):
            self.assertEqual(panel.grafana_summary(bad, False), {'access': 'not_configured', 'url': None}, bad)

    def test_the_serialised_summary_never_contains_monitoring_addresses(self):
        result, _ = summary({'GRAFANA_PRIVATE': 'true', 'GRAFANA_URL': 'https://grafana-internal.example/secret'})
        text = json.dumps(result.build())
        for leaked in ('monitoring-lab', '.svc', 'secret', 'grafana-internal', 'http'):
            self.assertNotIn(leaked, text)

    def test_deployment_labels_are_sanitised(self):
        result, _ = summary({'PANEL_PLATFORM': 'Kubernetes (k3s)', 'PANEL_ENVIRONMENT': '<b>x</b>',
                             'PANEL_COMPONENTS': 'Web (nginx), Runtime,<img src=x>'})
        deployment = result.deployment()
        self.assertEqual(deployment['platform'], 'Kubernetes (k3s)')
        self.assertIsNone(deployment['environment'])
        self.assertEqual(deployment['components'], ['Web (nginx)', 'Runtime'])


if __name__ == '__main__':
    unittest.main()

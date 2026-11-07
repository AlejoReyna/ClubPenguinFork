"""Aggregated infrastructure summary for the game page's "Infraestructura" panel.

Runs as a pod-local sidecar (MODE=panel) next to nginx, which proxies the same-origin path
/infra/summary.json to 127.0.0.1:8766. The browser never talks to Prometheus or Grafana and never
receives their addresses, credentials or internal names: only the summary built here.

* Prometheus is queried with FIXED expressions (below), a hard timeout and a short cache.
* Release fields come from the `release-info` ConfigMap, restricted to an allow-list.
* The player count is exposed only when PANEL_SHOW_PLAYERS=true (it is a population figure
  that has to be approved for this panel).
"""
import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse, request
from urllib.error import URLError

NAMESPACE = 'clubpenguin'
# Approved release-info keys. Anything else in the ConfigMap (image tags, run ids...) is ignored.
RELEASE_FIELDS = ('commit', 'version')
RELEASE_DIR = os.environ.get('RELEASE_INFO_DIR', '/etc/release-info')

QUERIES = {
    'available': f'sum(kube_deployment_status_replicas_available{{namespace="{NAMESPACE}"}})',
    'desired': f'sum(kube_deployment_spec_replicas{{namespace="{NAMESPACE}"}})',
    'players': 'sum(clubpenguin_players{world="Glaciar"})',
    'age': f'time() - max(timestamp(kube_deployment_spec_replicas{{namespace="{NAMESPACE}"}}))',
}

_SAFE_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._/()+-]{0,63}$')
_SHA = re.compile(r'^[0-9a-fA-F]{7,40}$')
_TAG = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}$')


class SourceUnavailable(Exception):
    """The metrics source did not answer usefully in time."""


def _env(name, default=''):
    return os.environ.get(name, default).strip()


def _label(value):
    return value if value and _SAFE_LABEL.match(value) else None


def read_release(directory=None):
    """Approved fields only; a commit is shortened, anything odd is dropped."""
    directory = directory or RELEASE_DIR
    found = {}
    for field in RELEASE_FIELDS:
        try:
            with open(os.path.join(directory, field), encoding='utf-8') as handle:
                value = handle.read(80).strip()
        except OSError:
            continue
        if field == 'commit' and _SHA.match(value):
            found['commit'] = value[:12].lower()
        elif field == 'version' and _TAG.match(value):
            found['version'] = value
    return found


def grafana_summary(url, private):
    if private:
        return {'access': 'private', 'url': None}
    url = (url or '').strip()
    if url:
        parts = parse.urlsplit(url)
        # Only an https page address without embedded credentials is ever handed to the browser.
        if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
            return {'access': 'link', 'url': url}
    return {'access': 'not_configured', 'url': None}


def query_scalar(base_url, expression, timeout):
    """Run one instant query. Returns a float, or None when no series matched."""
    endpoint = base_url.rstrip('/') + '/api/v1/query?' + parse.urlencode({'query': expression})
    opener = request.build_opener(request.ProxyHandler({}))  # cluster-internal: never via a proxy
    try:
        with opener.open(endpoint, timeout=timeout) as response:  # nosec - fixed expressions
            payload = json.loads(response.read(65536))
    except (URLError, OSError, ValueError) as error:
        raise SourceUnavailable(type(error).__name__) from None
    data = payload.get('data') if isinstance(payload, dict) else None
    result = data.get('result') if isinstance(data, dict) else None
    if not isinstance(payload, dict) or payload.get('status') != 'success' or not isinstance(result, list):
        raise SourceUnavailable('bad response')
    if not result:
        return None
    try:
        value = float(result[0]['value'][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


class Summary:
    def __init__(self, env=None, now=time.time, fetch=query_scalar):
        self.env = env if env is not None else os.environ
        self.now = now
        self.fetch = fetch
        self._lock = threading.Lock()
        self._cached = None
        self._cached_until = 0.0
        self._last_good = None

    def _get(self, name, default=''):
        return str(self.env.get(name, default)).strip()

    def deployment(self):
        components = [c.strip() for c in self._get('PANEL_COMPONENTS').split(',')]
        release = read_release(self._get('RELEASE_INFO_DIR', RELEASE_DIR))
        return {
            'project': _label(self._get('PANEL_PROJECT')) or 'ClubPenguin Lab',
            'provider': _label(self._get('PANEL_PROVIDER')),
            'platform': _label(self._get('PANEL_PLATFORM')),
            'environment': _label(self._get('PANEL_ENVIRONMENT')),
            'components': [c for c in components if _label(c)],
            'release': release.get('commit') or release.get('version'),
            'version': release.get('version'),
        }

    def prometheus(self):
        base = self._get('PROMETHEUS_URL')
        parts = parse.urlsplit(base)
        if not base or parts.scheme not in ('http', 'https') or not parts.hostname or parts.username:
            return {'state': 'not_configured', 'metrics': [], 'lastReading': None}

        timeout = max(1, min(int(self._get('PANEL_TIMEOUT', '2') or 2), 10))
        stale_after = max(15, min(int(self._get('PANEL_STALE_AFTER', '120') or 120), 3600))
        show_players = self._get('PANEL_SHOW_PLAYERS').lower() == 'true'
        wanted = ['available', 'desired', 'age'] + (['players'] if show_players else [])
        deadline = self.now() + timeout
        values = {}
        try:
            for name in wanted:
                remaining = deadline - self.now()
                if remaining <= 0:
                    raise SourceUnavailable('timeout')
                values[name] = self.fetch(base, QUERIES[name], remaining)
        except SourceUnavailable:
            # Never show an old value as current; keep only when the last good read happened.
            return {'state': 'offline', 'metrics': [], 'lastReading': self._last_good}

        read_at = self.now()
        metrics = []
        complete = True
        if values['available'] is None or values['desired'] is None:
            complete = False
        else:
            metrics.append({'id': 'workloads', 'label': 'Cargas disponibles',
                            'value': f"{round(values['available'])} / {round(values['desired'])}"})
        if show_players:
            if values.get('players') is None:
                complete = False
            else:
                metrics.append({'id': 'players', 'label': 'Jugadores', 'value': str(round(values['players']))})
        age = values['age']
        fresh = age is not None and 0 <= age <= stale_after
        # Operational means every shown workload is available AND the reading is recent.
        healthy = (values['available'] is not None and values['desired'] is not None
                   and values['available'] >= values['desired'] > 0)
        self._last_good = read_at
        if not complete or not fresh:
            state = 'stale'
        else:
            state = 'operational' if healthy else 'degraded'
        return {'state': state, 'metrics': metrics, 'lastReading': read_at}

    def build(self):
        with self._lock:
            if self._cached is not None and self.now() < self._cached_until:
                return self._cached
            summary = {
                'deployment': self.deployment(),
                'prometheus': self.prometheus(),
                'grafana': grafana_summary(self._get('GRAFANA_URL'), self._get('GRAFANA_PRIVATE').lower() == 'true'),
            }
            self._cached = summary
            self._cached_until = self.now() + max(1, min(int(self._get('PANEL_CACHE', '15') or 15), 300))
            return summary


class Handler(BaseHTTPRequestHandler):
    summary = None
    server_version = 'panel'

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.split('?')[0] != '/summary':
            self.send_error(404)
            return
        try:
            body = json.dumps(self.summary.build()).encode()
        except Exception:  # the page must keep working whatever happens here
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # no request logging; nothing sensitive in it anyway
        pass


def main():
    Handler.summary = Summary()
    # Pod-local only: nginx in the same pod proxies here. Not reachable from the network.
    ThreadingHTTPServer((_env('PANEL_BIND', '127.0.0.1'), 8766), Handler).serve_forever()


if __name__ == '__main__':
    main()

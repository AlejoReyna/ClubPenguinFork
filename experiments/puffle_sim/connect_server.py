"""Puffle Lab local page with the «Conectar mi ChatGPT» button.

Runs only on 127.0.0.1. The browser never receives tokens or the account
email: it only sees a status word. Run from the repo root:

    python3 -m experiments.puffle_sim.connect_server --port 1455
"""
import sys
import argparse
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import secrets
import urllib.parse
import webbrowser
from pathlib import Path

from .chatgpt_auth import AuthError, ChatGPTAuth, PlanNotGranted
from .spotify_auth import SpotifyAuth, SpotifyAuthError

MESSAGES = {
    'ok': '¡Listo! Tu puffle ya usa tu cuenta.',
    'plan': 'No concedió uso del plan: el puffle no puede hablar con tu cuenta. No se guardó nada.',
    'denied': 'Conexión cancelada.',
    'state': 'La solicitud expiró o no es válida. Vuelve a picarle al botón.',
    'error': 'No se pudo conectar. Intenta otra vez.',
    'bye': 'Desconectado. Se revocó el acceso y se borró el archivo local.',
    'bye_local': 'Se borró el archivo local, pero OpenAI no confirmó la revocación.',
    'resumed': 'El puffle vuelve a hablar.',
}
SPOTIFY_MESSAGES = {
    'sp_ok': '¡Listo! Tu puffle ya puede poner tu música.',
    'sp_denied': 'Conexión con Spotify cancelada.',
    'sp_error': 'No se pudo conectar Spotify. Intenta otra vez.',
    'sp_bye': 'Spotify desconectado. Quita también la app en spotify.com/account/apps.',
}
SPOTIFY_DEFAULT = {
    'sin_app': 'Para la música falta el Client ID de tu app de Spotify.',
    'desconectado': 'Conecta Spotify para pedirle canciones desde tu iglú.',
    'conectado': 'Spotify conectado. Solo tú, en tu iglú, puedes pedir música.',
}
DEFAULT_MESSAGES = {
    'desconectado': 'Conecta tu cuenta para que tu puffle conteste en el iglú.',
    'conectado': '¡Listo! Tu puffle ya usa tu cuenta.',
    'pausado': 'Tu puffle está descansando porque se acabó tu cuota. Pícale a Reanudar cuando vuelva.',
}
LABELS = {'desconectado': ('Desconectado', '#9aa0a6'),
          'conectado': ('Conectado', '#1f9d55'),
          'pausado': ('Pausado por cuota', '#d29b1f')}

PUFFLE_PNG = Path(__file__).resolve().parent / 'assets' / 'puffle.png'
PUFFLE_IMG = '<img class="puffle" src="/puffle.png" width="99" alt="Puffle">'

PAGE = """<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Puffle Lab · conexión</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px 20px;
background:#fff;color:#111;font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
main{{width:100%;max-width:380px;text-align:center}}
.puffle{{display:block;width:99px;height:auto;margin:0 auto 24px}}
h1{{margin:0 0 12px;font-size:26px;font-weight:600;letter-spacing:-.01em}}
.msg{{margin:0 0 28px;color:#555}}
.actions{{display:flex;flex-wrap:wrap;justify-content:center;gap:10px}}
button{{font:inherit;font-size:17px;font-weight:500;border-radius:8px;padding:11px 20px;cursor:pointer;
border:1px solid #2f7fd8;background:#2f7fd8;color:#fff}}
button:hover{{background:#2569b5;border-color:#2569b5}}
button:focus-visible{{outline:2px solid #2f7fd8;outline-offset:2px}}
.alt{{background:transparent;color:#111;border-color:#d4d4d4}}
.alt:hover{{background:#f0f0f0;border-color:#c4c4c4}}
.spotify{{margin-top:36px;padding-top:28px;border-top:1px solid #eee}}
.spotify h2{{margin:0 0 8px;font-size:19px;font-weight:600}}
.sp{{background:#1db954;border-color:#1db954}}.sp:hover{{background:#179443;border-color:#179443}}
.sr{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}}
@media (max-width:420px){{.actions form,.actions button{{width:100%}}}}
</style>
<main>{puffle}<h1>Puffle conversador</h1><span class="sr" role="status">{label}</span>
<p class="msg">{msg}</p><div class="actions">{actions}</div>{spotify}</main></html>"""


def form(action, label, cls, csrf):
    return (f'<form method="post" action="{action}" style="display:inline">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<button class="{cls}">{label}</button></form>')


def cors_origin(header_value):
    """Allow read-only status from the local game tab (127.0.0.1 / localhost only)."""
    if not header_value:
        return None
    parsed = urllib.parse.urlsplit(header_value)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        return None
    return header_value


def make_handler(auth, port, csrf, spotify=None):
    spotify = spotify or SpotifyAuth()
    allowed_hosts = {f'127.0.0.1:{port}'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PuffleLab'
        sys_version = ''

        def log_message(self, *args):  # callback URLs carry codes; log nothing
            pass

        def _send(self, status, body=b'', ctype='text/html; charset=utf-8', location=None, cors=None):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Frame-Options', 'DENY')
            if cors:
                self.send_header('Access-Control-Allow-Origin', cors)
            self.send_header('Content-Security-Policy',
                             "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self' https://auth.openai.com https://accounts.spotify.com")
            if location:
                self.send_header('Location', location)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            if self.headers.get('Host') not in allowed_hosts:
                self._send(421, b'host no permitido', 'text/plain')
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            url = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path == '/auth/callback':
                try:
                    auth.complete(query)
                    code = 'ok'
                except PlanNotGranted:
                    code = 'plan'
                    sys.stderr.write('[puffle-lab] callback: plan no concedido\n')
                except AuthError as e:
                    # AuthError messages are fixed strings without secrets.
                    sys.stderr.write(f'[puffle-lab] callback: {e}\n')
                    code = 'denied' if 'cancelaste' in str(e) else ('state' if 'state' in str(e) else 'error')
                except Exception as e:
                    sys.stderr.write(f'[puffle-lab] callback: excepción {type(e).__name__}\n')
                    code = 'error'
                return self._send(303, location=f'/?m={code}')
            if url.path == '/spotify/callback':
                try:
                    spotify.complete(query)
                    code = 'sp_ok'
                except SpotifyAuthError as e:
                    sys.stderr.write(f'[puffle-lab] spotify: {e}\n')
                    code = 'sp_denied' if 'cancelaste' in str(e) else 'sp_error'
                except Exception as e:
                    sys.stderr.write(f'[puffle-lab] spotify: excepción {type(e).__name__}\n')
                    code = 'sp_error'
                return self._send(303, location=f'/?m={code}')
            if url.path == '/puffle.png':
                self._send(200, PUFFLE_PNG.read_bytes(), 'image/png')
                return
            if url.path == '/api/local-status':
                origin = cors_origin(self.headers.get('Origin'))
                if not origin:
                    return self._send(403, b'forbidden', 'text/plain')
                payload = json.dumps({'chatgpt': auth.status(), 'spotify': spotify.status()}).encode()
                return self._send(200, payload, 'application/json; charset=utf-8', cors=origin)
            if url.path != '/':
                return self._send(404, b'no encontrado', 'text/plain')
            status = auth.status()
            label, color = LABELS[status]
            if status == 'desconectado':
                actions = form('/connect', 'Conectar mi ChatGPT', 'go', csrf)
            else:
                actions = form('/disconnect', 'Desconectar', 'alt', csrf)
                if status == 'pausado':
                    actions = form('/resume', 'Reanudar', 'go', csrf) + actions
            msg = MESSAGES.get(query.get('m'), DEFAULT_MESSAGES[status])
            sp_status = spotify.status()
            sp_msg = SPOTIFY_MESSAGES.get(query.get('m'), SPOTIFY_DEFAULT[sp_status])
            if sp_status == 'conectado':
                sp_actions = form('/spotify/disconnect', 'Desconectar Spotify', 'alt', csrf)
            elif sp_status == 'desconectado':
                sp_actions = form('/spotify/connect', 'Conectar mi Spotify', 'sp', csrf)
            else:
                sp_actions = ''
            sp_block = (f'<section class="spotify"><h2>Música</h2><p class="msg">{html.escape(sp_msg)}</p>'
                        f'<div class="actions">{sp_actions}</div></section>')
            body = PAGE.format(puffle=PUFFLE_IMG, color=color, label=label, msg=html.escape(msg), actions=actions,
                               spotify=sp_block)
            self._send(200, body.encode())

        def do_POST(self):
            if not self._host_ok():
                return
            length = min(int(self.headers.get('Content-Length') or 0), 4096)
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
            origin = self.headers.get('Origin')
            if (not hmac.compare_digest(fields.get('csrf', [''])[0], csrf)
                    or (origin and origin != f'http://127.0.0.1:{port}')):
                return self._send(403, b'csrf', 'text/plain')
            if self.path == '/connect':
                return self._send(303, location=auth.start(port))
            if self.path == '/disconnect':
                return self._send(303, location='/?m=' + ('bye' if auth.disconnect() else 'bye_local'))
            if self.path == '/spotify/connect':
                try:
                    return self._send(303, location=spotify.start(port))
                except SpotifyAuthError:
                    return self._send(303, location='/?m=sp_error')
            if self.path == '/spotify/disconnect':
                spotify.disconnect()
                return self._send(303, location='/?m=sp_bye')
            if self.path == '/resume':
                auth.set_paused(False)
                return self._send(303, location='/?m=resumed')
            self._send(404, b'no encontrado', 'text/plain')

    return Handler


def serve(port=1455, auth=None, open_browser=True):
    auth = auth or ChatGPTAuth()
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(auth, port, secrets.token_urlsafe(32)))
    url = f'http://127.0.0.1:{port}/'
    print(f'Puffle Lab: abre {url}  (Ctrl+C para cerrar)', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='Página local «Conectar mi ChatGPT» de Puffle Lab')
    parser.add_argument('--port', type=int, default=1455)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    serve(args.port, open_browser=not args.no_browser)


if __name__ == '__main__':
    main()

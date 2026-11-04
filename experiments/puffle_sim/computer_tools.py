"""Explicitly approved, narrowly scoped actions on the local Mac."""
import asyncio
import contextlib
import json
import re
import unicodedata
from pathlib import Path

APPLICATIONS = {
    'calculadora': '/System/Applications/Calculator.app',
    'notas': '/System/Applications/Notes.app',
    'safari': '/Applications/Safari.app',
    'textedit': '/System/Applications/TextEdit.app',
    'musica': '/System/Applications/Music.app',
    'brave': '/Applications/Brave Browser.app',
}

TOOL = {'type': 'namespace', 'name': 'mac',
        'description': 'Acciones locales en la Mac del dueño, sujetas a confirmación.',
        'tools': [{'type': 'function', 'name': 'abrir_aplicacion',
                   'description': 'Solicita abrir una aplicación que el dueño pidió explícitamente.',
                   'parameters': {'type': 'object', 'properties': {
                       'aplicacion': {'type': 'string', 'enum': list(APPLICATIONS)}},
                       'required': ['aplicacion'], 'additionalProperties': False},
                   'strict': True}]}


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c))


def requested_action(item, context):
    """Accept one recognized tool call only when the latest player asked for it."""
    if not isinstance(item, dict) or item.get('type') != 'function_call':
        return None
    if item.get('name') not in ('abrir_aplicacion', 'mac.abrir_aplicacion'):
        return None
    if item.get('namespace') not in (None, 'mac'):
        return None
    try:
        args = json.loads(item.get('arguments', ''))
    except (ValueError, TypeError):
        return None
    if not isinstance(args, dict) or set(args) != {'aplicacion'}:
        return None
    app = args['aplicacion']
    if not isinstance(app, str) or app not in APPLICATIONS:
        return None
    last = next((text for role, text in reversed(context) if role == 'player'), '')
    phrase = normalized(last)
    if (not re.search(r'\b(?:abre|abrir|abreme|abras|inicia|lanza)\b', phrase)
            or not re.search(r'(?<!\w)' + re.escape(app) + r'(?!\w)', phrase)
            or re.search(r'\b(?:no|nunca|jamas)\s+(?:me\s+)?(?:abre|abras|abrir|inicies|inicia|lances|lanza)\b', phrase)):
        return None
    return {'application': app}


async def _run(*args, timeout):
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, output
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def confirm_and_open(action):
    app = action.get('application') if isinstance(action, dict) else None
    path = APPLICATIONS.get(app)
    if not path or not Path(path).is_dir():
        return 'No encuentro esa aplicación.'
    script = ('on run argv\n'
              'display dialog ("El puffle quiere abrir " & item 1 of argv & ". ¿Lo permites?") '
              'buttons {"Cancelar", "Abrir"} default button "Cancelar" giving up after 18\n'
              'end run')
    try:
        code, out = await _run('/usr/bin/osascript', '-e', script, app, timeout=20)
        if code != 0 or b'button returned:Abrir' not in out:
            return 'No abrí la aplicación.'
        code, _ = await _run('/usr/bin/open', '-a', path, timeout=5)
    except (OSError, asyncio.TimeoutError):
        return 'No pude abrir la aplicación.'
    return '¡Ya abrí ' + app + '!' if code == 0 else 'No pude abrir la aplicación.'

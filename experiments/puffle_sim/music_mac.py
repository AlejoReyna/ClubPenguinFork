"""Explicit owner chat controls for the local macOS Music application."""
import asyncio
import base64
import contextlib

from .spotify import bubble

_ACTIONS = {'play', 'pause', 'resume', 'next', 'previous'}
_SCRIPTS = {
    'play': '''
set resultsList to search library playlist 1 for wanted only names
if (count of resultsList) is 0 then return "NOT_FOUND"
set selectedTrack to item 1 of resultsList
set selectedID to persistent ID of selectedTrack
play selectedTrack
delay 0.5
if player state is playing and (persistent ID of current track) is selectedID then return "PLAYED:" & (name of current track)
return "UNVERIFIED"
''',
    'pause': '''
pause
delay 0.2
if player state is paused or player state is stopped then return "PAUSED"
return "UNVERIFIED"
''',
    'resume': '''
play
delay 0.5
if player state is playing then return "RESUMED"
return "UNVERIFIED"
''',
    'next': '''
if player state is stopped then return "NO_TRACK"
set oldID to persistent ID of current track
next track
delay 0.5
if (persistent ID of current track) is not oldID then return "NEXT"
return "UNVERIFIED"
''',
    'previous': '''
if player state is stopped then return "NO_TRACK"
previous track
delay 0.5
if player state is playing or player state is paused then return "PREVIOUS_REQUESTED"
return "UNVERIFIED"
''',
}


def make_script(intent):
    if not isinstance(intent, dict) or set(intent) != {'action', 'query'}:
        raise ValueError('invalid music intent')
    action, query = intent['action'], intent['query']
    if action not in _ACTIONS or not isinstance(query, str) or len(query) > 80:
        raise ValueError('invalid music intent')
    if action == 'play' and not query.strip():
        raise ValueError('empty music query')
    # The only interpolated characters are base64; user text cannot become
    # AppleScript syntax. The script itself is passed to osascript on stdin.
    if any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise ValueError('invalid query')
    encoded = base64.b64encode(query.encode('utf-8')).decode('ascii')
    return ('use framework "Foundation"\nuse scripting additions\n'
            'set encoded to "' + encoded + '"\n'
            "set decodedData to current application's NSData's alloc()'s "
            'initWithBase64EncodedString:encoded options:0\n'
            "set wanted to (current application's NSString's alloc()'s "
            "initWithData:decodedData encoding:(current application's NSUTF8StringEncoding)) as text\n"
            'try\n'
            'tell application "Music"\n' + _SCRIPTS[action] +
            '\nend tell\n'
            'on error errorMessage number errorNumber\n'
            'return "ERROR:" & errorNumber\n'
            'end try\n')


async def _run_script(script, timeout=20):
    proc = await asyncio.create_subprocess_exec('/usr/bin/osascript', '-',
                                                stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(script.encode('utf-8')), timeout)
        return proc.returncode, output.decode('utf-8', 'replace').strip()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


class MusicMacBackend:
    """One local owner's Music app; activation is explicit at relay startup."""
    def __init__(self, runner=None, catalog_player=None):
        self.runner = runner or _run_script
        if catalog_player is None:
            from .music_catalog_mac import play_catalog
            catalog_player = play_catalog
        self.catalog_player = catalog_player

    async def run(self, intent):
        try:
            script = make_script(intent)
            if intent['action'] == 'play':
                status, title = await self.catalog_player(intent['query'])
                if status == 'PLAYED':
                    return bubble('Puse «%s».' % title)
                if status == 'NOT_FOUND':
                    return 'No encontré esa canción en Apple Music.'
                if status in ('ERROR:-25211', 'ERROR:-1743', 'ERROR:-1744'):
                    return 'Autoriza Accesibilidad para Música en tu Mac.'
                if status == 'UNVERIFIED':
                    return 'Se lo pedí a Música, sin confirmar.'
                return 'No pude buscar en Apple Music.'
            code, status = await self.runner(script)
        except (ValueError, OSError, asyncio.TimeoutError):
            return 'No pude controlar Música en tu Mac.'
        if code != 0 or status.startswith('ERROR:'):
            if status in ('ERROR:-1743', 'ERROR:-1744'):
                return 'Autoriza el control de Música en tu Mac.'
            return 'No pude controlar Música en tu Mac.'
        replies = {
            'PAUSED': 'Listo, pausé la música.',
            'RESUMED': 'Listo, la música sigue.',
            'NEXT': 'Listo, siguiente canción.',
            'PREVIOUS_REQUESTED': 'Pedí la canción anterior.',
            'NO_TRACK': 'Reproduce algo en Música primero.',
            'NOT_FOUND': 'No la encontré en tu biblioteca de Música.',
            'UNVERIFIED': 'Se lo pedí a Música, sin confirmar.',
        }
        if status.startswith('PLAYED:'):
            return bubble('Puse «%s».' % status[len('PLAYED:'):])
        return replies.get(status, 'No pude controlar Música en tu Mac.')

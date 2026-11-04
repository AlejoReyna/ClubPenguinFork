"""Search and play the Apple Music catalog through the local Music window.

This is an opt-in, single-owner local fixture. No song query is put in argv or
logs. UI accessibility permission and a visible, unlocked desktop are required.
"""
import asyncio
import base64
import contextlib
import re
import unicodedata


def _text_variable(name, value):
    encoded = base64.b64encode(value.encode('utf-8')).decode('ascii')
    return (
        'set encodedText to "' + encoded + '"\n'
        "set decodedText to current application's NSData's alloc()'s "
        'initWithBase64EncodedString:encodedText options:0\n'
        'set ' + name + " to (current application's NSString's alloc()'s "
        "initWithData:decodedText encoding:(current application's NSUTF8StringEncoding)) as text\n"
    )


_PREFIX = 'use framework "Foundation"\nuse scripting additions\n'
_SEARCH = '''
tell application "Music" to activate
tell application "System Events" to tell process "Music"
  if not frontmost then return "ERROR:FOCUS"
  try
    click UI element 1 of row 1 of outline 1 of scroll area 1 of splitter group 1 of window 1
  end try
  delay 0.3
  keystroke "f" using command down
  delay 0.4
  if exists text field 1 of group 1 of toolbar 1 of window 1 then
    set fieldRef to text field 1 of group 1 of toolbar 1 of window 1
  else if exists text field 1 of toolbar 1 of window 1 then
    set fieldRef to text field 1 of toolbar 1 of window 1
  else
    return "ERROR:SEARCH_FIELD"
  end if
  set focused of fieldRef to true
  set value of fieldRef to wanted
  delay 0.6
  key code 36
  delay 0.3
  key code 36
  delay 3
  set topResults to list 1 of list 1 of scroll area 2 of splitter group 1 of window 1
  set resultText to ""
  repeat with e in (every UI element of topResults)
    try
      set titleText to description of e
      repeat with labelItem in (every static text of e)
        set typeText to name of labelItem
        if typeText contains "·" then
          set resultText to resultText & titleText & tab & typeText & linefeed
          exit repeat
        end if
      end repeat
    end try
  end repeat
  return resultText
end tell
'''
_FIND = '''
tell application "Music" to activate
tell application "System Events" to tell process "Music"
  if not frontmost then return "ERROR:FOCUS"
  set topResults to list 1 of list 1 of scroll area 2 of splitter group 1 of window 1
  repeat with e in (every UI element of topResults)
    try
      if description of e is wantedTitle then
        repeat with labelItem in (every static text of e)
          if name of labelItem is wantedType then
            set {x, y} to position of e
            set {w, h} to size of e
            if w < 10 or h < 10 then return "ERROR:POSITION"
            return ((x + w / 2) as integer as text) & "," & ((y + h / 2) as integer as text)
          end if
        end repeat
      end if
    end try
  end repeat
  return "ERROR:RESULT"
end tell
'''
_CLICK = '''
tell application "System Events" to tell process "Music"
  if not frontmost then return "ERROR:FOCUS"
  set topResults to list 1 of list 1 of scroll area 2 of splitter group 1 of window 1
  repeat with e in (every UI element of topResults)
    try
      if description of e is wantedTitle then
        repeat with labelItem in (every static text of e)
          if name of labelItem is wantedType then
            if not (exists checkbox 1 of e) then return "ERROR:BUTTON"
            click checkbox 1 of e
            return "CLICKED"
          end if
        end repeat
      end if
    end try
  end repeat
  return "ERROR:RESULT"
end tell
'''
_VERIFY = '''
tell application "Music"
  if player state is playing then
    if name of current track contains wantedTitle and artist of current track is wantedArtist then return "PLAYED"
  end if
  return "UNVERIFIED"
end tell
'''


async def _osascript(script, language='AppleScript', timeout=10):
    proc = await asyncio.create_subprocess_exec(
        '/usr/bin/osascript', '-l', language, '-', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(script.encode('utf-8')), timeout)
        if proc.returncode:
            return 'ERROR:SCRIPT'
        return output.decode('utf-8', 'replace').strip()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def _tokens(value):
    folded = unicodedata.normalize('NFKD', value.casefold())
    folded = ''.join(c for c in folded if not unicodedata.combining(c))
    return {v for v in re.findall(r'[^\W_]+', folded) if len(v) > 1 and v not in {'de', 'la', 'el', 'los', 'las', 'the', 'and', 'con'}}


def select_song(query, results):
    """Select a matching song only, never an album, playlist, or unrelated hit."""
    terms = _tokens(query)
    if not terms:
        return None
    best, best_score = None, 0
    for line in results.splitlines():
        parts = line.split('\t')
        if len(parts) != 2 or len(parts[0]) > 256 or len(parts[1]) > 256:
            continue
        title, kind = (s.strip() for s in parts)
        if '·' not in kind:
            continue
        label, artist = (s.strip() for s in kind.split('·', 1))
        if label.casefold() not in {'song', 'canción', 'cancion'}:
            continue
        title_words = _tokens(title)
        overlap = terms & (title_words | _tokens(artist))
        # At least one query word must identify the title and a majority of the
        # meaningful query words must match title or artist.
        if not (terms & title_words) or len(overlap) * 2 < len(terms):
            continue
        score = len(overlap) / len(terms)
        if score > best_score:
            best, best_score = (title, kind), score
    return best


def _script_with_values(body, **values):
    return _PREFIX + ''.join(_text_variable(k, v) for k, v in values.items()) + 'try\n' + body + '\non error number errNum\nreturn "ERROR:" & errNum\nend try\n'


async def play_catalog(query, runner=_osascript):
    if not isinstance(query, str) or not query.strip() or len(query) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in query):
        return 'ERROR:QUERY', ''
    results = await runner(_script_with_values(_SEARCH, wanted=query))
    if results.startswith('ERROR:'):
        return results, ''
    song = select_song(query, results)
    if song is None:
        return 'NOT_FOUND', ''
    title, kind = song
    values = dict(wantedTitle=title, wantedType=kind)
    artist = kind.split('·', 1)[1].strip()
    verify_values = dict(wantedTitle=title, wantedArtist=artist)
    # A request for the already-playing track need not toggle its play button.
    if await runner(_script_with_values(_VERIFY, **verify_values)) == 'PLAYED':
        return 'PLAYED', title
    coordinates = await runner(_script_with_values(_FIND, **values))
    if not re.fullmatch(r'-?\d{1,5},-?\d{1,5}', coordinates):
        return 'ERROR:POSITION', ''
    x, y = (int(v) for v in coordinates.split(','))
    # Hovering reveals Music's play button. Coordinates come from the current
    # matching result, never from chat. osascript holds Accessibility permission.
    js = ("ObjC.import('CoreGraphics');\n" +
          f"for (const x of [{x - 2},{x}]) {{const p={{x:x,y:{y}}}; " +
          "$.CGWarpMouseCursorPosition(p); const e=$.CGEventCreateMouseEvent(null,5,p,0); " +
          "$.CGEventPost(0,e); delay(0.2);}\n")
    hover_result = await runner(js, language='JavaScript')
    if hover_result.startswith('ERROR:'):
        return hover_result, ''
    clicked = await runner(_script_with_values(_CLICK, **values))
    if clicked != 'CLICKED':
        return 'ERROR:BUTTON', ''
    for _ in range(4):
        await asyncio.sleep(0.5)
        if await runner(_script_with_values(_VERIFY, **verify_values)) == 'PLAYED':
            return 'PLAYED', title
    return 'UNVERIFIED', ''

"""Local owner's Apple Music web player, controlled without moving the cursor.

The owner signs in in Safari. Only the dedicated window and its first tab are
addressed. No cookies, tokens, account details, or chat are read or logged.
"""
import asyncio
import json

from .music_catalog_mac import _osascript, _script_with_values, select_song
from .spotify import bubble


_APPLESCRIPT = '''
tell application "Safari"
  return do JavaScript javascriptSource in tab 1 of window id WINDOW_ID
end tell
'''


def _js_script(window_id, source):
    if type(window_id) is not int or not 0 < window_id < 2**31:
        raise ValueError('invalid Safari window')
    # Base64 is inserted by _script_with_values: no chat becomes AppleScript.
    return _script_with_values(_APPLESCRIPT.replace('WINDOW_ID', str(window_id)),
                               javascriptSource=source)


def _js(body):
    return ('(()=>{if(location.hostname!=="music.apple.com"||location.protocol!=="https:")'
            'return JSON.stringify({status:"wrong-page"});try{' + body +
            '}catch(e){return JSON.stringify({status:"error"})}})()')


async def _command(window_id, source, runner):
    try:
        result = await runner(_js_script(window_id, _js(source)))
        parsed = json.loads(result)
        return parsed if isinstance(parsed, dict) else {'status': 'error'}
    except (ValueError, TypeError, OSError, asyncio.TimeoutError):
        return {'status': 'error'}


def _status_js():
    return '''const a=document.querySelector('audio,video'),m=navigator.mediaSession;
const d=m&&m.metadata;
return JSON.stringify({status:'ok',playing:!!a&&!a.paused,ready:a?a.readyState:0,
  duration:a&&Number.isFinite(a.duration)?a.duration:null,
  preview:!!a&&(()=>{try{return new URL(a.currentSrc).hostname==='audio-ssl.itunes.apple.com'}catch(e){return false}})(),
  title:d?d.title:'',artist:d?d.artist:''});'''


async def _safari_runner(script):
    # Navigation can temporarily make Safari stop answering Apple Events.
    return await _osascript(script, timeout=4)


class SafariMusicBackend:
    """Only enabled for the fixture owner and an explicitly selected window."""
    def __init__(self, window_id, runner=None):
        if type(window_id) is not int or not 0 < window_id < 2**31:
            raise ValueError('invalid Safari window')
        self.window_id = window_id
        self.runner = runner or _safari_runner

    async def _call(self, js):
        return await _command(self.window_id, js, self.runner)

    async def _play(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 80 or any(ord(c) < 32 for c in query):
            return 'No pude buscar esa canción.'
        q = json.dumps(query, ensure_ascii=True)
        navigation = '''const m=location.pathname.match(/^\\/([a-z]{2})(?:\\/|$)/i);
const country=m?m[1]:'us';
location.assign('/'+country+'/search?term='+encodeURIComponent(QUERY));
return JSON.stringify({status:'searching'});'''.replace('QUERY', q)
        if (await self._call(navigation)).get('status') != 'searching':
            return 'Abre Apple Music en la ventana conectada.'
        search = '''const q=QUERY;
if(!location.pathname.endsWith('/search')||new URLSearchParams(location.search).get('term')!==q)
 return JSON.stringify({status:'loading'});
const rows=[...document.querySelectorAll('[data-testid="track-lockup"]')].slice(0,40);
const tracks=rows.map(e=>({title:(e.querySelector('[data-testid="track-lockup-title"]')?.innerText||'').trim(),
 artist:(e.querySelector('[data-testid="track-lockup-subtitle"]')?.innerText||'').trim()}))
 .filter(x=>x.title&&x.artist);
return JSON.stringify({status:tracks.length?'ok':'loading',tracks});'''.replace('QUERY', q)
        tracks = []
        transient_errors = 0
        for _ in range(20):
            await asyncio.sleep(0.4)
            result = await self._call(search)
            if result.get('status') == 'ok' and isinstance(result.get('tracks'), list):
                tracks = result['tracks']
                break
            if result.get('status') == 'error' and transient_errors < 2:
                transient_errors += 1
                continue
            if result.get('status') not in ('loading', 'ok'):
                return 'No pude buscar en Apple Music.'
        if not tracks:
            return 'No encontré resultados en Apple Music.'
        rows = []
        for t in tracks[:40]:
            if not isinstance(t, dict):
                continue
            title, artist = t.get('title'), t.get('artist')
            if (isinstance(title, str) and isinstance(artist, str) and
                    not any(c in title + artist for c in '\t\n\r')):
                rows.append(title + '\tSong · ' + artist)
        selected = select_song(query, '\n'.join(rows))
        if not selected:
            return 'No encontré esa canción en Apple Music.'
        title, kind = selected
        artist = kind.split('·', 1)[1].strip()
        # The result markup can appear before the web player's event handlers
        # are ready, particularly when Safari is in the background.
        await asyncio.sleep(3)
        title_js, artist_js = json.dumps(title, ensure_ascii=True), json.dumps(artist, ensure_ascii=True)
        click = '''const title=TITLE, artist=ARTIST;
const rows=[...document.querySelectorAll('[data-testid="track-lockup"]')];
const row=rows.find(e=>(e.querySelector('[data-testid="track-lockup-title"]')?.innerText||'').trim()===title&&
 (e.querySelector('[data-testid="track-lockup-subtitle"]')?.innerText||'').trim()===artist);
const button=row&&row.querySelector('[data-testid="play-button"]');
if(!button) return JSON.stringify({status:'missing'});
button.click();return JSON.stringify({status:'clicked'});'''.replace('TITLE', title_js).replace('ARTIST', artist_js)
        if (await self._call(click)).get('status') != 'clicked':
            return 'No pude iniciar esa canción.'
        for _ in range(12):
            await asyncio.sleep(0.5)
            state = await self._call(_status_js())
            if (state.get('status') == 'ok' and state.get('playing') and state.get('ready', 0) >= 2
                    and str(state.get('title', '')).casefold() == title.casefold()
                    and str(state.get('artist', '')).casefold() == artist.casefold()):
                duration = state.get('duration')
                if state.get('preview') or not isinstance(duration, (int, float)) or duration <= 35:
                    return 'Parece que solo suena un fragmento.'
                return bubble('Puse «%s».' % title)
        return 'No pude confirmar la reproducción.'

    async def _control(self, action):
        state = await self._call(_status_js())
        if state.get('status') != 'ok':
            return 'Abre Apple Music en la ventana conectada.'
        if action == 'pause' and not state.get('playing'):
            return 'La música ya está pausada.'
        if action == 'resume' and state.get('playing'):
            return 'La música ya está sonando.'
        if action in ('pause', 'resume'):
            selector = '.playback-play__pause' if action == 'pause' else '.playback-play__play'
            js = '''const b=document.querySelector(SELECTOR);if(!b)return JSON.stringify({status:'missing'});
b.click();return JSON.stringify({status:'clicked'});'''.replace('SELECTOR', json.dumps(selector))
        else:
            direction = 'next' if action == 'next' else 'previous'
            js = '''const host=document.querySelector('amp-playback-controls-item-skip[direction="DIRECTION"]');
const b=host&&host.shadowRoot&&host.shadowRoot.querySelector('button');
if(!b||b.disabled)return JSON.stringify({status:'missing'});b.click();return JSON.stringify({status:'clicked'});'''.replace('DIRECTION', direction)
        if (await self._call(js)).get('status') != 'clicked':
            return 'No pude controlar Apple Music.'
        await asyncio.sleep(0.5)
        after = await self._call(_status_js())
        if action == 'pause' and after.get('status') == 'ok' and not after.get('playing'):
            return 'Listo, pausé la música.'
        if action == 'resume' and after.get('playing'):
            return 'Listo, la música sigue.'
        if action == 'next' and after.get('playing') and (after.get('title'), after.get('artist')) != (state.get('title'), state.get('artist')):
            return 'Listo, siguiente canción.'
        return 'Se lo pedí a Apple Music, sin confirmar.'

    async def run(self, intent):
        if not isinstance(intent, dict) or set(intent) != {'action', 'query'}:
            return 'No pude controlar Apple Music.'
        action = intent.get('action')
        if action == 'play':
            return await self._play(intent.get('query'))
        if action in ('pause', 'resume', 'next', 'previous'):
            return await self._control(action)
        return 'No pude controlar Apple Music.'

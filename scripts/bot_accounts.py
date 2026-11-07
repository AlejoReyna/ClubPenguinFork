"""Account/bootstrap helpers. The running bots never register or access SQL."""
import http.cookiejar
import secrets
import urllib.parse
import urllib.request

PERSONAS = (
    ('GuiaIA', ['Guia'], 'Guia amable: explica lugares y ayuda a visitantes.'),
    ('PescadorIA', ['Pescador'], 'Pescador paciente: conversa sobre el muelle y la pesca.'),
    ('ExploradorIA', ['Explorador'], 'Explorador curioso: habla de la isla y sus aventuras.'),
)

# Dedicated accounts are needed for the visible names: Houdini sends the
# registered penguin name in the roster, not the client's configured nickname.
WORK_ROLES = (
    ('BaristaIA', ['Barista'], 110,
     'Barista amable: recomienda cafe y bebidas, conversa y representa pedidos solo mediante dialogo.'),
    ('DJIA', ['DJ'], 120,
     'DJ animado: conversa sobre musica, baile y ambiente. No afirmes haber cambiado la musica.'),
    ('PizzeroIA', ['Pizzero'], 330,
     'Pizzero amable: conversa sobre pizzas e ingredientes y representa pedidos y entregas solo mediante dialogo.'),
)

# Walk rectangles in raw AS2 room coordinates (stage 760x480, the values sent
# with j#jr/u#sp). Source: block_mc walkability masks exported with FFDec from
# vendor/legacy-media/.../rooms/{coffee,dance,pizza}.swf (block_mc filled =
# blocked, engine.swf isPositionValid), then checked in Ruffle with a penguin
# at every corner and the centre (2026-11-04). Each post is the centre.
#   coffee.swf: floor left of the counter (counter x>=380, y 200-340).
#   dance.swf: in front of the DJ booth (booth y<=220), top of the dance floor.
#   pizza.swf: in front of the right end of the counter (counter x<=350, y<=250),
#   above the tables (y>=300); next to the room start_x/start_y (400,278).
MEASURED_WORK_AREAS = {
    'BaristaIA': {'min_x': 300, 'max_x': 360, 'min_y': 290, 'max_y': 350},
    'DJIA': {'min_x': 280, 'max_x': 380, 'min_y': 230, 'max_y': 265},
    'PizzeroIA': {'min_x': 365, 'max_x': 430, 'min_y': 258, 'max_y': 290},
}

# Fixed posts (centre of each measured zone). DJ Cobra / Cadence and Viterbos stay put.
MEASURED_WORK_POSTS = {
    'BaristaIA': None,
    # Cadence: fixed DJ booth post (tuned in-room 2026-11-05). stationary, no auto-dance.
    'DJIA': {'x': 322, 'y': 168},
    'PizzeroIA': {'x': 397, 'y': 274},
}

# u#sa action id for the client dance button (Houdini forwards as xt sa).
DANCE_ACTION_ID = 11

# Visible names after the work-bot makeover (username keys stay baristaia/djia/pizzeroia).
WORK_VISIBLE = {
    'BaristaIA': ('Juanito', ['Barista']),
    'DJIA': ('Cadence', ['DJ', 'Cobra', 'DJ Cobra', 'Cadence']),
    'PizzeroIA': ('Viterbos', ['Pizzero']),
}

WORK_NICKNAME_ALIASES = {
    'baristaia': {'BaristaIA', 'Juanito', 'Baristaia'},
    'djia': {'DJIA', 'DJ Cobra', 'Cadence', 'Djia'},
    'pizzeroia': {'PizzeroIA', 'Viterbos', 'Pizzeroia'},
}


def work_config_document(areas=None):
    """Build a fresh configuration; never register accounts or change a Secret.

    Defaults to MEASURED_WORK_AREAS; server-side coordinate acceptance does
    not establish that a point is walkable, so overrides must be measured too.
    """
    if areas is None:
        areas = MEASURED_WORK_AREAS
    if set(areas) != {role[0] for role in WORK_ROLES}:
        raise ValueError('Supply one verified movement area for each of the three roles')
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'containers/bots'))
    from bots.config import parse_area

    doc = config_document()
    doc['bots'] = []
    for name, _legacy_aliases, room, personality in WORK_ROLES:
        area = areas[name]
        parse_area(area, name)
        nickname, aliases = WORK_VISIBLE[name]
        entry = {'username': name.lower(), 'password': secrets.token_urlsafe(16),
                 'nickname': nickname, 'aliases': aliases,
                 'personality': personality, 'room': room}
        post = MEASURED_WORK_POSTS.get(name)
        if post is not None:
            entry['stationary'] = True
            entry['anchor'] = dict(post)
        else:
            entry['walk'] = dict(area)
        doc['bots'].append(entry)
    return doc


def config_document():
    return {
        'provider': {'mode': 'simulated', 'model': 'kimi-k2.6', 'thinking': 'disabled',
                     'max_completion_tokens': 120, 'timeout': 15, 'retries': 2,
                     'max_concurrent': 2, 'max_requests_per_day': 100},
        'policy': {'requests_per_minute': 6, 'max_concurrent_requests': 2,
                   'player_cooldown': 6, 'bot_min_gap': 2, 'queue_size': 8,
                   'memory_turns': 8, 'memory_ttl': 600},
        'behavior': {'respond_to_bots': False, 'walk_min_delay': 12,
                     'walk_max_delay': 28, 'max_message_length': 54,
                     'first_move_delay_min': 0.3, 'first_move_delay_max': 0.8},
        'bots': [{'username': name.lower(), 'nickname': name, 'aliases': aliases,
                  'personality': personality, 'room': 100,
                  'password': secrets.token_urlsafe(16)}
                 for name, aliases, personality in PERSONAS],
    }


def cast_config_document(cast_path):
    """Fresh synthetic/local credentials for N cards; no existing identities adopted."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'containers/bots'))
    from bots.cast import load_cards
    from bots.rooms import load_catalog
    cards = load_cards(cast_path)
    catalog = load_catalog()
    doc = config_document()
    doc['bots'] = []
    for card in cards.values():
        if not card.enabled:
            continue
        if card.missing_for_new_character():
            raise ValueError('New cast characters need complete cards')
        if card.appearance_items or card.appearance_color is not None:
            raise ValueError('Phase 1 does not provision appearance items or colors')
        room = catalog.require(card.home_room)
        if room.spawn is None:
            raise ValueError('The home room needs a documented spawn')
        # A safe legacy representation also exists in the credential file. Cast
        # activation still requires BOTS_CAST_DIR at runtime.
        doc['bots'].append({'username': card.username, 'password': secrets.token_urlsafe(16),
                            'nickname': card.nickname, 'aliases': list(card.aliases),
                            'room': card.home_room, 'stationary': True,
                            'anchor': dict(zip(('x', 'y'), room.spawn)),
                            'personality': card.personality_text()})
    if not doc['bots']:
        raise ValueError('No enabled cast characters')
    doc['provider'].update(daily_usd=1.0, monthly_usd=30.0, max_concurrent=4)
    doc['policy']['max_concurrent_requests'] = 4
    return doc


def cast_nickname_sql(bots):
    """Only validated ASCII identities may enter this operator SQL."""
    import re
    pairs = []
    for bot in bots:
        user, nick = bot['username'], bot['nickname']
        if not re.fullmatch(r'[a-z0-9]{4,12}', user) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ]{0,29}', nick):
            raise ValueError('Invalid cast identity for SQL')
        pairs.append((user, nick))
    cases = ' '.join("WHEN '%s' THEN '%s'" % pair for pair in pairs)
    users = ','.join("'%s'" % user for user, _ in pairs)
    return ('UPDATE penguin SET nickname = CASE username ' + cases + ' END '
            'WHERE username IN (' + users + ') AND active AND NOT moderator '
            'AND NOT stealth_moderator RETURNING username;')


def register(base_url, username, password):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(fields):
        request = urllib.request.Request(base_url.rstrip('/') + '/create/legacy/',
                                         data=urllib.parse.urlencode(fields).encode())
        with opener.open(request, timeout=15) as response:
            result = urllib.parse.parse_qs(response.read().decode())
        if result.get('success') != ['1']:
            raise RuntimeError('Account registration rejected; response and credentials omitted')
        return result

    sid = post({'action': 'validate_username', 'username': username, 'colour': '1', 'lang': 'es'})['sid'][0]
    post({'action': 'validate_password_email', 'sid': sid, 'password': password,
          'password_confirm': password, 'email': username.lower() + '@example.com', 'lang': 'es'})


def registration_program():
    """Code for a normal registration from an operator exec into Dash; stdin is private."""
    from pathlib import Path
    source = Path(__file__).read_text()
    return source + '''
import sys
payload = json.load(sys.stdin)
for bot in payload['bots']:
    register('http://web:8080', bot['username'], bot['password'])
print(json.dumps({'created': len(payload['bots'])}))
'''


def authentication_program():
    """Authenticate provisioned credentials through normal login; never echo them."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'containers/bots/bots/as2.py').read_text()
    return source + '''
import sys, json
async def verify():
    payload = json.load(sys.stdin)
    for bot in payload['bots']:
        conn = await AS2Connection.connect('login', 6112)
        try:
            key = await conn.handshake(10)
            await conn.login_legacy(bot['username'].lower(), login_server_password(bot['password'], key), 10)
        finally:
            await conn.close()
    print(json.dumps({'authenticated': len(payload['bots'])}))
asyncio.run(verify())
'''


# Houdini Language.Es. Dash stores this in approval_es; the roster field is the bitmask.
SPANISH_APPROVAL_BIT = 8


def deployment_is_stopped(deployment):
    """True only when the bots Deployment is declared at 0 and no pod is up.

    A scale-down that has not finished still reports status.replicas > 0.
    Missing status.replicas counts as zero, which is what the API emits once
    the last pod is gone.
    """
    if not isinstance(deployment, dict):
        return False
    spec = deployment.get('spec') if isinstance(deployment.get('spec'), dict) else {}
    status = deployment.get('status') if isinstance(deployment.get('status'), dict) else {}
    try:
        spec_replicas = int(spec.get('replicas', 1))
        raw_status = status.get('replicas', 0)
        status_replicas = int(0 if raw_status is None else raw_status)
    except (TypeError, ValueError):
        return False
    return spec_replicas == 0 and status_replicas == 0


def legacy_work_nicknames_unchanged(accounts):
    """Refuse to overwrite later renames when using the legacy provisioner."""
    return all(name in WORK_NICKNAME_ALIASES and isinstance(row, dict) and
               row.get('nickname') in WORK_NICKNAME_ALIASES[name]
               for name, row in accounts.items())


def nickname_correction_sql():
    """Point the three work accounts at their exact visible names.

    Dash title-cases every registration (USERNAME_FORCE_CASE), which would
    show Baristaia, Djia and Pizzeroia. This updates only nickname, and only
    on active non-moderator rows. Usernames are the fixed role constants.
    """
    pairs = []
    for name, _aliases, _room, _personality in WORK_ROLES:
        if not name.isalnum():
            raise ValueError('role name must be alphanumeric')
        pairs.append((name.lower(), WORK_VISIBLE[name][0]))
    cases = ' '.join("WHEN '%s' THEN '%s'" % pair for pair in pairs)
    users = ','.join("'%s'" % user for user, _nick in pairs)
    return (
        'UPDATE penguin SET nickname = CASE username ' + cases + ' END '
        'WHERE username IN (' + users + ') '
        'AND active AND NOT moderator AND NOT stealth_moderator '
        'RETURNING username;'
    )


def roster_approval_program():
    """Join each role's room and require the exact nickname plus Spanish bit 8.

    Runs inside Dash, like the login check. Prints a short JSON result and
    never prints credentials. A mismatch is a reason code, not the nickname.
    """
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'containers/bots/bots/as2.py').read_text()
    return source + '''
import sys, json
BIT = 8
async def one(bot):
    login = await AS2Connection.connect('login', 6112)
    try:
        key = await login.handshake(10)
        fields = await login.login_legacy(bot['username'].lower(), login_server_password(bot['password'], key), 10)
    finally:
        await login.close()
    pid, login_key = fields[0], fields[1]
    world = await AS2Connection.connect('glaciar', 9876)
    try:
        rndk = await world.handshake(10)
        await world.login_legacy(bot['username'].lower(), world_login_password(login_key, rndk), 10)
        await world.join_world(int(pid), login_key, 10)
        anchor = bot.get('anchor')
        if anchor:
            x, y = int(anchor['x']), int(anchor['y'])
        else:
            walk = bot['walk']
            x = (int(walk['min_x']) + int(walk['max_x'])) // 2
            y = (int(walk['min_y']) + int(walk['max_y'])) // 2
        frame = await world.join_room(int(bot['room']), 10, x, y)
        if not frame.args or frame.args[0] != str(int(bot['room'])):
            return 'room'
        record = next((parts for parts in (v.split('|') for v in frame.args)
                       if len(parts) >= 3 and parts[0] == str(int(pid))), None)
        if record is None or record[1] != bot['nickname']:
            return 'nickname'
        if (int(record[2]) & BIT) == 0:
            return 'approval'
        return None
    finally:
        await world.close()
async def verify():
    payload = json.load(sys.stdin)
    approved = 0
    reason = None
    for bot in payload['bots']:
        reason = await one(bot)
        if reason:
            break
        approved += 1
    print(json.dumps({'approved': approved, 'reason': reason}))
asyncio.run(verify())
'''

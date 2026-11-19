"""Lab overlays on pinned dash/houdini sources (starting coins, welcome puffle)."""
import argparse
import hashlib
from pathlib import Path

STARTING_COINS = 10_000

HOUDINI_FILES = {
    'data/penguin.py': '7198ab515937e880210461798fb9684df857e2578a280a5230ba599784b716da',
    'handlers/play/pet.py': '40cc0e91471a7cc2c28617c3092f8b10051201905790427a709cad3bd6b216bf',
}

DASH_FILES = {
    'routes/create/legacy.py': '35b60ed1240e4942b41f6c1092af8d9f11cba30f93da5906fbafd77211297dcf',
    'routes/create/vanilla.py': 'ae3604187992da7e3c671d4737d2072265510fcc8ecf8c35dfddd7754cb85e71',
}

DASH_IMPORT = """from dash.data import db
from dash.data.item import PenguinItem
from dash.data.mail import PenguinPostcard, send_email
from dash.data.penguin import ActivationKey, Penguin
"""

DASH_IMPORT_WITH_GIFT = (
    DASH_IMPORT
    + 'from lab_welcome_puffle import grant_for_penguin, normalize_welcome_puffle_name, WELCOME_PUFFLE_NAME\n'
)

ACCOUNT_CREATE_TAIL = """                                   approval_en=app.config.APPROVE_USERNAME,
                                   approval_pt=app.config.APPROVE_USERNAME,
                                   approval_fr=app.config.APPROVE_USERNAME,
                                   approval_es=app.config.APPROVE_USERNAME,
                                   approval_de=app.config.APPROVE_USERNAME,
                                   approval_ru=app.config.APPROVE_USERNAME,
                                   active=app.config.ACTIVATE_PLAYER)

    await PenguinItem.create(penguin_id=penguin.id, item_id=int(color))
    await PenguinPostcard.create(penguin_id=penguin.id, sender_id=None, postcard_id=125)
"""


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('unexpected pinned source structure')
    return source.replace(old, new, 1)


def wrap_account_create(source, create_header):
    old = create_header + ACCOUNT_CREATE_TAIL
    new = (
        '    async with db.transaction():\n'
        + '\n'.join('    ' + line if line else line for line in create_header.split('\n'))
        + '\n'.join('    ' + line if line else line for line in ACCOUNT_CREATE_TAIL.split('\n'))
        + '        await grant_for_penguin(db, penguin.id, puffle_name)\n'
    )
    return once(source, old, new)


def transform_houdini(name, data):
    if name not in HOUDINI_FILES or hashlib.sha256(data).hexdigest() != HOUDINI_FILES[name]:
        raise ValueError('unrecognized upstream houdini source')
    source = data.decode('utf-8')
    if name == 'data/penguin.py':
        source = once(
            source,
            'coins = db.Column(db.Integer, nullable=False, server_default=db.text("500"))',
            f'coins = db.Column(db.Integer, nullable=False, server_default=db.text("{STARTING_COINS}"))',
        )
    elif name == 'handlers/play/pet.py':
        source = once(
            source,
            """from houdini.data.room import PenguinBackyardRoom, PenguinIglooRoom
from houdini.handlers import Priority, XMLPacket, XTPacket
""",
            """from houdini.data.room import PenguinBackyardRoom, PenguinIglooRoom
from houdini.data import db
from houdini.handlers import Priority, XMLPacket, XTPacket
from houdini.lab_welcome_puffle import is_protected_puffle_id
""",
        )
        source = once(
            source,
            """                    if is_legacy_puffle and puffle.food == puffle.rest == puffle.clean == 0:
                        await penguin.add_inbox(server.postcards[puffle_crumbs.runaway_postcard], details=puffle.name)
                        await penguin.puffles.delete(puffle.id)
""",
            """                    if is_legacy_puffle and puffle.food == puffle.rest == puffle.clean == 0:
                        if not await is_protected_puffle_id(db, puffle.id):
                            await penguin.add_inbox(server.postcards[puffle_crumbs.runaway_postcard], details=puffle.name)
                            await penguin.puffles.delete(puffle.id)
""",
        )
        source = once(
            source,
            """@handlers.handler(XTPacket('p', 'pcn'))
async def handle_check_puffle_name(p, puffle_name):
    name_ok = check_name(p, puffle_name)
    await p.send_xt('pcn', puffle_name, int(name_ok))


@handlers.handler(XTPacket('p', 'pg'), client=ClientType.Vanilla)
""",
            """@handlers.handler(XTPacket('p', 'pcn'))
async def handle_check_puffle_name(p, puffle_name):
    name_ok = check_name(p, puffle_name)
    await p.send_xt('pcn', puffle_name, int(name_ok))


@handlers.handler(XTPacket('p', 'pufflerename'))
async def handle_rename_puffle(p, puffle: PenguinPuffle, name: str):
    if not check_name(p, name):
        return await p.send_error(441)
    await puffle.update(name=name.title()).apply()
    await p.send_xt('pufflerename', puffle.id, puffle.name)


@handlers.handler(XTPacket('p', 'pg'), client=ClientType.Vanilla)
""",
        )
        source = once(
            source,
            """async def handle_return_puffle(p, puffle: PenguinPuffle):
    if p.walking == puffle.id:
        await p.update(walking=None).apply()

    await p.puffles.delete(puffle.id)
    await p.room.send_xt('prp', puffle.id)
""",
            """async def handle_return_puffle(p, puffle: PenguinPuffle):
    if await is_protected_puffle_id(db, puffle.id):
        return

    if p.walking == puffle.id:
        await p.update(walking=None).apply()

    await p.puffles.delete(puffle.id)
    await p.room.send_xt('prp', puffle.id)
""",
        )
    compile(source, name, 'exec')
    return source


def transform_dash(name, data):
    if name not in DASH_FILES or hashlib.sha256(data).hexdigest() != DASH_FILES[name]:
        raise ValueError('unrecognized upstream dash source')
    source = data.decode('utf-8')
    source = once(source, DASH_IMPORT, DASH_IMPORT_WITH_GIFT)
    if name == 'routes/create/legacy.py':
        old = """    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,
                                   color=int(color),
                                   approval_en=app.config.APPROVE_USERNAME,"""
        new = """    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,
                                   color=int(color), coins=app.config.STARTING_COINS,
                                   approval_en=app.config.APPROVE_USERNAME,"""
        source = once(source, old, new)
        source = once(
            source,
            """    username = username.strip()
    if app.config.USERNAME_FORCE_CASE:
        username = username.title()

    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,
                                   color=int(color), coins=app.config.STARTING_COINS,
                                   approval_en=app.config.APPROVE_USERNAME,""",
            """    username = username.strip()
    if app.config.USERNAME_FORCE_CASE:
        username = username.title()

    display_name = request.form.get('nickname', '').strip() or username
    if len(display_name) < 1 or len(display_name) > 30 or '|' in display_name or '%' in display_name:
        return response.text(urlencode({
            'error': i18n.t('create.name_not_allowed', locale=lang)
        }))

    puffle_name = normalize_welcome_puffle_name(request.form.get('puffle_name', ''))
    if puffle_name is None:
        return response.text(urlencode({
            'error': i18n.t('create.name_not_allowed', locale=lang)
        }))

    penguin = await Penguin.create(username=username.lower(), nickname=display_name, password=password, email=email,
                                   color=int(color), coins=app.config.STARTING_COINS,
                                   approval_en=app.config.APPROVE_USERNAME,""",
        )
        source = wrap_account_create(
            source,
            '    penguin = await Penguin.create(username=username.lower(), nickname=display_name, password=password, email=email,\n'
            '                                   color=int(color), coins=app.config.STARTING_COINS,\n',
        )
    elif name == 'routes/create/vanilla.py':
        source = once(
            source,
            """    if app.config.USERNAME_FORCE_CASE:
        username = username.title()

    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,
                                   color=int(color),
""",
            """    if app.config.USERNAME_FORCE_CASE:
        username = username.title()

    puffle_name = normalize_welcome_puffle_name(request.form.get('puffle_name', ''))
    if puffle_name is None:
        puffle_name = WELCOME_PUFFLE_NAME

    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,
                                   color=int(color),
""",
        )
        source = wrap_account_create(
            source,
            '    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,\n'
            '                                   color=int(color),\n',
        )
    compile(source, name, 'exec')
    return source


def apply_tree(root, relative_paths, transform):
    for relative in relative_paths:
        path = root / relative
        patched = transform(relative, path.read_bytes())
        path.write_text(patched, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--houdini', type=Path, help='houdini package directory')
    parser.add_argument('--dash', type=Path, help='dash package directory')
    args = parser.parse_args()
    if args.houdini:
        apply_tree(args.houdini, HOUDINI_FILES, transform_houdini)
    if args.dash:
        apply_tree(args.dash, DASH_FILES, transform_dash)


if __name__ == '__main__':
    main()

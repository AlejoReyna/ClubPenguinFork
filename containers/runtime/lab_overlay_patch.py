"""Lab overlays on pinned dash/houdini sources (starting coins, etc.)."""
import argparse
import hashlib
from pathlib import Path

STARTING_COINS = 10_000

HOUDINI_FILES = {
    'data/penguin.py': '7198ab515937e880210461798fb9684df857e2578a280a5230ba599784b716da',
}

DASH_FILES = {
    'routes/create/legacy.py': '35b60ed1240e4942b41f6c1092af8d9f11cba30f93da5906fbafd77211297dcf',
}


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('unexpected pinned source structure')
    return source.replace(old, new, 1)


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
    compile(source, name, 'exec')
    return source


def transform_dash(name, data):
    if name not in DASH_FILES or hashlib.sha256(data).hexdigest() != DASH_FILES[name]:
        raise ValueError('unrecognized upstream dash source')
    source = data.decode('utf-8')
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

    penguin = await Penguin.create(username=username.lower(), nickname=display_name, password=password, email=email,
                                   color=int(color), coins=app.config.STARTING_COINS,
                                   approval_en=app.config.APPROVE_USERNAME,""",
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

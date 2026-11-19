"""Bind mounts so glaciar-dev nginx serves the full 2017 client media from vendor.

The published web image may ship an incomplete ``/srv/media/play/v2/client`` (no
``club_penguin.swf``). Vendor has the dump. Overlay only the login/create mobile
patches; ``engine.swf`` gets only the glaciarCamera hook (``build_engine_glaciar.py``),
not the legacy puffle renderer patch that broke the 2017 map.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_MEDIA = ROOT / 'vendor' / 'vanilla-media' / 'media'
PATCH_DIR = ROOT / '.local' / 'glaciar-dev' / 'patched-swfs'
PATCH_STRINGS = ROOT / '.local' / 'glaciar-dev' / 'patched-strings'
PATCHED = ('login.swf', 'create.swf', 'engine.swf')
LANGS = ('de', 'en', 'es', 'fr', 'pt')
STRING_FILES = ('game_strings.json', 'game_strings.jsonp')


def ensure_chat_restrict_strings() -> None:
    """Overlay chat string configs so the in-game chat field accepts ``/``."""
    vendor_root = ROOT / 'vendor' / 'vanilla-media' / 'media'
    if not vendor_root.is_dir():
        return
    needs = []
    for lang in LANGS:
        for name in STRING_FILES:
            src = vendor_root / 'play' / lang / 'web_service' / 'game_configs' / name
            dest = PATCH_STRINGS / 'play' / lang / 'web_service' / 'game_configs' / name
            if not src.is_file():
                continue
            if not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime:
                needs.append((src, dest))
    if not needs and (PATCH_STRINGS / 'play' / 'es' / 'web_service' / 'game_configs' / 'game_strings.json').is_file():
        return
    for src, dest in needs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    subprocess.run([sys.executable, str(ROOT / 'scripts/patch_chat_restrict.py'),
                    str(PATCH_STRINGS)], check=True, cwd=ROOT)


def ensure_glaciar_engine_patch() -> None:
    """2017 engine with glaciarCamera only (navbar + mobile view bridge)."""
    dest = PATCH_DIR / 'engine.swf'
    vendor = VENDOR_MEDIA / 'play' / 'v2' / 'client' / 'engine.swf'
    if not vendor.is_file():
        return
    if dest.is_file() and dest.stat().st_mtime >= vendor.stat().st_mtime:
        return
    PATCH_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    ffdec = ROOT / '.local' / 'tools' / 'ffdec-26.4.0' / 'ffdec.jar'
    if not ffdec.is_file():
        return
    subprocess.run([
        sys.executable, str(ROOT / 'experiments/puffle_sim/client/build_engine_glaciar.py'),
        '--ffdec', str(ffdec), '--engine', str(vendor), '--output', str(dest),
    ], check=True, cwd=ROOT)


def ensure_patched_swfs(web_container: str = 'glaciar-dev-web') -> None:
    """Copy patched SWFs from a running web container if the cache dir is empty."""
    PATCH_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    ensure_glaciar_engine_patch()
    missing = [name for name in PATCHED if name != 'engine.swf' and not (PATCH_DIR / name).is_file()]
    if not missing:
        return
    inspect = subprocess.run(['docker', 'inspect', web_container], capture_output=True)
    if inspect.returncode != 0:
        return
    for name in missing:
        src = f'{web_container}:/srv/media/play/v2/client/{name}'
        dest = PATCH_DIR / name
        if subprocess.run(['docker', 'cp', src, str(dest)], capture_output=True).returncode == 0:
            dest.chmod(0o644)


def dev_shell_mounts() -> list[str]:
    return [
        f'{ROOT}/containers/web/index.html:/srv/play/index.html:ro',
        f'{ROOT}/containers/web/puffle.html:/srv/play/puffle/index.html:ro',
        f'{ROOT}/containers/web/register.html:/srv/play/register.html:ro',
        f'{ROOT}/containers/web/default.conf:/etc/nginx/conf.d/default.conf:ro',
        f'{ROOT}/containers/web/sw.js:/srv/play/sw.js:ro',
        f'{ROOT}/containers/web/manifest.webmanifest:/srv/play/manifest.webmanifest:ro',
        f'{ROOT}/containers/web/dialog-bots-mentions.json:/srv/play/dialog-bots-mentions.json:ro',
    ]


def dev_web_mounts(*, include_media: bool = True) -> list[str]:
    mounts = dev_shell_mounts()
    if not include_media:
        return mounts
    club = VENDOR_MEDIA / 'play' / 'v2' / 'client' / 'club_penguin.swf'
    if not club.is_file():
        return mounts
    ensure_patched_swfs()
    ensure_chat_restrict_strings()
    mounts.append(f'{VENDOR_MEDIA}:/srv/media:ro')
    mounts.append(
        f'{ROOT}/containers/web/environment_data.xml:/srv/media/play/web_service/environment_data.xml:ro')
    clothing = ROOT / 'containers/web/custom-clothing'
    if clothing.is_dir():
        mounts.append(
            f'{clothing}:/srv/media/play/v2/content/global/clothing:ro')
    for name in PATCHED:
        path = PATCH_DIR / name
        if path.is_file():
            mounts.append(f'{path}:/srv/media/play/v2/client/{name}:ro')
    for lang in LANGS:
        for name in STRING_FILES:
            path = PATCH_STRINGS / 'play' / lang / 'web_service' / 'game_configs' / name
            if path.is_file():
                mounts.append(
                    f'{path}:/srv/media/play/{lang}/web_service/game_configs/{name}:ro')
    return mounts


def web_mounts_differ(existing: list[str] | None, desired: list[str]) -> bool:
    return sorted(existing or []) != sorted(desired)
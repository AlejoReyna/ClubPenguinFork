"""Bind mounts so glaciar-dev nginx serves the full 2017 client media from vendor.

The published web image may ship an incomplete ``/srv/media/play/v2/client`` (no
``club_penguin.swf``). Vendor has the dump. Overlay only the login/create mobile
patches; ``engine.swf`` must stay the full 2017 media build (the small puffle
patch breaks walking and the map when booting through ``club_penguin.swf``).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_MEDIA = ROOT / 'vendor' / 'vanilla-media' / 'media'
PATCH_DIR = ROOT / '.local' / 'glaciar-dev' / 'patched-swfs'
PATCHED = ('login.swf', 'create.swf')


def ensure_patched_swfs(web_container: str = 'glaciar-dev-web') -> None:
    """Copy patched SWFs from a running web container if the cache dir is empty."""
    PATCH_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    missing = [name for name in PATCHED if not (PATCH_DIR / name).is_file()]
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
    return mounts


def web_mounts_differ(existing: list[str] | None, desired: list[str]) -> bool:
    return sorted(existing or []) != sorted(desired)
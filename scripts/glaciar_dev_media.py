"""Bind mounts so glaciar-dev nginx serves the full 2017 client media from vendor.

The published web image may ship an incomplete ``/srv/media/play/v2/client`` (no
``club_penguin.swf``). Vendor has the dump. Overlay only the login/create mobile
patches and the 2017 engine with the Glaciar camera and puffle chat renderer.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_MEDIA = ROOT / 'vendor' / 'vanilla-media' / 'media'
PATCH_DIR = ROOT / '.local' / 'glaciar-dev' / 'patched-swfs'
PATCH_STRINGS = ROOT / '.local' / 'glaciar-dev' / 'patched-strings'
PATCHED = ('login.swf', 'create.swf', 'engine.swf')
LANGS = ('de', 'en', 'es', 'fr', 'pt', 'ru')
STRING_FILES = ('game_strings.json', 'game_strings.jsonp')
BUNDLE_FILE = 'game_configs.bin'


def ensure_chat_restrict_strings() -> None:
    """Overlay chat string configs so the in-game chat field accepts ``/``."""
    vendor_root = ROOT / 'vendor' / 'vanilla-media' / 'media'
    if not vendor_root.is_dir():
        return
    needs = []
    service_rel_paths = [Path('play') / lang / 'web_service' for lang in LANGS]
    service_rel_paths.append(Path('play') / 'web_service')
    for service_rel in service_rel_paths:
        for name in STRING_FILES:
            rel = service_rel / 'game_configs' / name
            src = vendor_root / rel
            dest = PATCH_STRINGS / rel
            if not src.is_file():
                continue
            if not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime:
                needs.append((src, dest))
        bundle_rel = service_rel / BUNDLE_FILE
        src_bundle = vendor_root / bundle_rel
        dest_bundle = PATCH_STRINGS / bundle_rel
        if src_bundle.is_file() and (not dest_bundle.is_file() or dest_bundle.stat().st_mtime < src_bundle.stat().st_mtime):
            needs.append((src_bundle, dest_bundle))
    es_json = PATCH_STRINGS / 'play' / 'es' / 'web_service' / 'game_configs' / 'game_strings.json'
    es_bundle = PATCH_STRINGS / 'play' / 'es' / 'web_service' / BUNDLE_FILE
    if not needs and es_json.is_file() and es_bundle.is_file():
        return
    for src, dest in needs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    subprocess.run([sys.executable, str(ROOT / 'scripts/patch_chat_restrict.py'),
                    str(PATCH_STRINGS)], check=True, cwd=ROOT)


def ensure_glaciar_engine_patch() -> None:
    """Refresh the patched 2017 engine when the source or patch inputs change."""
    dest = PATCH_DIR / 'engine.swf'
    vendor = VENDOR_MEDIA / 'play' / 'v2' / 'client' / 'engine.swf'
    if not vendor.is_file():
        return
    patch_dir = ROOT / 'experiments/puffle_sim/client'
    inputs = [vendor, *(patch_dir / name for name in (
        'build_engine_glaciar.py', 'build_engine.py', 'mobile_camera_glaciar.as', 'renderer.as'))]
    if dest.is_file() and dest.stat().st_mtime_ns >= max(path.stat().st_mtime_ns for path in inputs):
        return
    PATCH_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    ffdec = ROOT / '.local' / 'tools' / 'ffdec-26.4.0' / 'ffdec.jar'
    if not ffdec.is_file():
        return
    with tempfile.TemporaryDirectory(dir=PATCH_DIR) as temporary:
        patched = Path(temporary) / dest.name
        subprocess.run([
            sys.executable, str(patch_dir / 'build_engine_glaciar.py'),
            '--ffdec', str(ffdec), '--engine', str(vendor), '--output', str(patched),
        ], check=True, cwd=ROOT)
        patched.replace(dest)


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
    service_rel_paths = [Path('play') / lang / 'web_service' for lang in LANGS]
    service_rel_paths.append(Path('play') / 'web_service')
    for service_rel in service_rel_paths:
        for name in STRING_FILES:
            rel = service_rel / 'game_configs' / name
            path = PATCH_STRINGS / rel
            if path.is_file():
                mounts.append(f'{path}:/srv/media/{rel.as_posix()}:ro')
        bundle_rel = service_rel / BUNDLE_FILE
        bundle_path = PATCH_STRINGS / bundle_rel
        if bundle_path.is_file():
            mounts.append(f'{bundle_path}:/srv/media/{bundle_rel.as_posix()}:ro')
    return mounts


def web_mounts_differ(existing: list[str] | None, desired: list[str]) -> bool:
    return sorted(existing or []) != sorted(desired)

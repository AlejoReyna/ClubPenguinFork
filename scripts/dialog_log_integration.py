"""Dialog-log check for the isolated bots bench. No Houdini and no network.

The running service does not call ``emit`` yet (those call sites belong to
Vida). This drives ``emit`` inside the bots image, once with the switch off
and once with it on, and checks the mounted directory from the host.
"""
import hashlib
import hmac
import stat
from pathlib import Path


_PROBE = (
    'import hashlib, pathlib\n'
    'print(hashlib.sha256(pathlib.Path("/app/bots/dialog_log.py").read_bytes()).hexdigest())\n'
)

_SCRIPT = r'''
import asyncio, os, pathlib
from bots.dialog_log import DialogLog, emit, install

async def main():
    mode = os.environ["CHECK"]
    log = DialogLog(
        "/var/lib/bots-dialog", "/etc/bots-dialog/enabled",
        os.environ["BOTS_DIALOG_LOG_SALT"],
        flush_seconds=0.05, switch_seconds=0.05, batch_size=1,
        retention_interval=10 ** 9, error_backoff=0.05, error_backoff_max=0.05)
    install(log)
    task = asyncio.create_task(log.run())
    try:
        for _ in range(40):
            if log.enabled == (mode == "on"):
                break
            await asyncio.sleep(0.05)
        else:
            raise SystemExit("switch did not settle to " + mode)
        emit(room_id=110, speaker="rayo", kind="reply", target_kind="player",
             source="local", player_id=424242, text="hola bitacora",
             human_text="oye rayo", priority="P0")
        for _ in range(40):
            files = list(pathlib.Path("/var/lib/bots-dialog").glob("*.jsonl"))
            if mode == "on" and files and "hola bitacora" in files[0].read_text():
                break
            if mode == "off" and files:
                raise SystemExit("switch off created a day file")
            await asyncio.sleep(0.05)
        files = list(pathlib.Path("/var/lib/bots-dialog").glob("*.jsonl"))
        if mode == "off":
            if files:
                raise SystemExit("switch off created a day file")
            print("off-ok")
            return
        if len(files) != 1 or "hola bitacora" not in files[0].read_text():
            raise SystemExit("switch on did not write the line")
        print("on-ok")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

asyncio.run(main())
'''


def check_dialog_log(docker, tag, temp, label):
    """Fail when the image does not record a line with the switch on, or records one with it off."""
    image = 'clubpenguin/bots:' + tag
    root = Path(__file__).resolve().parents[1]
    source = (root / 'containers/bots/bots/dialog_log.py').read_bytes()
    probe = docker(['run', '--rm', '--pull=never', '--network=none', '--read-only',
                    '--label', label, '--entrypoint', 'python', image, '-c', _PROBE])
    if probe.stdout.strip() != hashlib.sha256(source).hexdigest():
        raise RuntimeError('Bots image is stale; rebuild before testing the dialog log')
    salt = 'bench-salt'
    digest = hmac.new(salt.encode('utf-8'), b'424242', hashlib.sha256).hexdigest()
    for mode in ('off', 'on'):
        work = Path(temp) / ('dialog-' + mode)
        work.mkdir(mode=0o777)
        work.chmod(0o777)
        switch = Path(temp) / ('switch-' + mode)
        switch.write_text('true\n' if mode == 'on' else 'false\n')
        switch.chmod(0o644)
        result = docker([
            'run', '--rm', '--pull=never', '--network=none', '--read-only', '--label', label,
            '--mount', f'type=bind,source={work},target=/var/lib/bots-dialog',
            '--mount', f'type=bind,source={switch},target=/etc/bots-dialog/enabled,readonly',
            '-e', 'BOTS_DIALOG_LOG_SALT=' + salt, '-e', 'CHECK=' + mode,
            '--entrypoint', 'python', image, '-c', _SCRIPT])
        if result.stdout.strip() != mode + '-ok':
            raise RuntimeError('Dialog log check failed for switch ' + mode)
        files = list(work.glob('*.jsonl'))
        if mode == 'off':
            if files:
                raise RuntimeError('Switch off created ' + files[0].name)
            continue
        if len(files) != 1:
            raise RuntimeError('Switch on should create one day file')
        text = files[0].read_text(encoding='utf-8')
        if 'hola bitacora' not in text or stat.S_IMODE(files[0].stat().st_mode) != 0o600:
            raise RuntimeError('Day file is missing the line or is not mode 0600')
        if '424242' in text.replace(digest, ''):
            raise RuntimeError('Raw player id leaked into the dialog log')

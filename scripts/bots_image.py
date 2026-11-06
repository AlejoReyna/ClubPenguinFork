"""Refuse stale local bot images before each disposable integration stage."""
import hashlib
import json


def verify_bots_image(root, docker, image):
    package = root / 'containers/bots/bots'
    expected = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.rglob('*') if p.suffix in ('.py', '.json')}
    check = ('import pathlib,hashlib,json\nbase=pathlib.Path("/app/bots")\n'
             'print(json.dumps({str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() '
             'for p in base.rglob("*") if p.suffix in (".py",".json")}))')
    actual = docker(['run', '--rm', '--pull=never', '--network=none', '--read-only',
                     '--entrypoint', 'python', image, '-I', '-c', check])
    if json.loads(actual.stdout) != expected:
        raise RuntimeError('Bots image code or room data is stale; rebuild before testing')
    return package

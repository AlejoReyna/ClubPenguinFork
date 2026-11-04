"""Pass Kubernetes environment configuration into the upstream entrypoints."""
import os
import runpy
import sys

mode = os.environ.get('MODE', 'login')
if mode == 'puffle':
    runpy.run_module('production.puffle.broker', run_name='__main__')
elif mode == 'exporter':
    runpy.run_path('/app/exporter.py', run_name='__main__')
elif mode == 'proxy':
    runpy.run_path('/app/socket_proxy.py', run_name='__main__')
elif mode == 'dash':
    os.chdir('/app/dash')
    sys.path.insert(0, '/app/dash')
    sys.argv = ['bootstrap.py', '--config', 'lab_config.py']
    runpy.run_path('bootstrap.py', run_name='__main__')
elif mode in ('login', 'world'):
    os.chdir('/app/houdini')
    sys.path.insert(0, '/app/houdini')
    if mode == 'world':
        from production.puffle.world import install
        install()
    sys.argv = [
        'bootstrap.py', mode,
        '--address', '0.0.0.0',
        '--port', os.environ.get('PORT', '6112' if mode == 'login' else '9876'),
        '--database-address', os.environ.get('POSTGRES_HOST', 'postgres'),
        '--database-name', os.environ.get('POSTGRES_DB', 'houdini'),
        '--database-username', os.environ.get('POSTGRES_USER', 'houdini'),
        '--database-password', os.environ['POSTGRES_PASSWORD'],
        '--redis-address', os.environ.get('REDIS_HOST', 'redis'),
        '--capacity', os.environ.get('WORLD_CAPACITY', '20'),
        '--lang', 'es', '--single-client-mode', '--default-client', 'legacy',
    ]
    if mode == 'world':
        sys.argv += ['-id', '3101', '--name', 'Glaciar']
    runpy.run_path('bootstrap.py', run_name='__main__')
else:
    raise SystemExit(f'Unknown MODE: {mode}')


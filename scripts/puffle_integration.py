"""End-to-end production entrypoint test with disposable accounts and no providers."""
import json


def check_puffle(root, docker, exec_script, dash, postgres):
    smoke = (root / 'scripts/game-smoke.py').read_text()
    registration = "ns={'__name__':'synthetic'}\nexec(%r,ns)\n" % smoke
    registration += ("import json\nprint(json.dumps([dict(zip(('name','password'),ns['register']('http://dash:3000'))) "
                     "for _ in range(3)]))\n")
    accounts = json.loads(exec_script(dash, registration).stdout)
    names = [x['name'] for x in accounts]
    if not all(n.isascii() and n.isalpha() and n.startswith('lab') for n in names):
        raise RuntimeError('Unexpected disposable identities')
    selected = ','.join("'" + n + "'" for n in names)
    rows = docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-At', '-F', '|',
                   '-c', f'SELECT username,id FROM penguin WHERE username IN ({selected})']).stdout.splitlines()
    ids = {name: int(pid) for name, pid in (x.split('|') for x in rows)}
    if len(ids) != 3:
        raise RuntimeError('Missing disposable identities')
    for account in accounts:
        account['id'] = ids[account['name']]
    id_list = ','.join(str(x['id']) for x in accounts)
    sql = ('WITH r AS (INSERT INTO penguin_igloo_room(penguin_id,type,flooring,location,locked) '
           f'SELECT id,1,0,1,false FROM penguin WHERE id IN ({id_list}) RETURNING id,penguin_id) '
           'UPDATE penguin SET igloo=r.id FROM r WHERE penguin.id=r.penguin_id; '
           "INSERT INTO penguin_puffle(penguin_id,puffle_id,name) SELECT id,0,'Puffle' "
           f'FROM penguin WHERE id IN ({id_list});')
    docker(['exec', postgres, 'psql', '-U', 'houdini', '-d', 'houdini', '-v', 'ON_ERROR_STOP=1', '-c', sql])
    source = ('import sys,types,json\n'
              'm=types.ModuleType("bot_as2");sys.modules["bot_as2"]=m\n'
              'exec(%r,m.__dict__)\n' % (root / 'containers/bots/bots/as2.py').read_text())
    source += "ns={'__name__':'fixture'}\nexec(%r,ns)\n" % (root / 'tests/puffle_sim/fixture_clients.py').read_text()
    source += (root / 'tests/production/socket_journey.py').read_text()
    output = exec_script(dash, source, json.dumps(accounts)).stdout
    print(output.strip())

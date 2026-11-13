import logging, os, sys, tempfile
logging.disable(logging.CRITICAL)
import requests
import harness

work = tempfile.mkdtemp(prefix='xms_')
srv, mod, LOG, plug = harness.build(harness.PLUGIN_DIR, work, harness.STUB)
base = f'http://127.0.0.1:{srv.server_port}/api/plugins/xmigrate_ct/api/'
from pegaprox.utils import rbac, audit
H = {'X-Requested-With': 'XMLHttpRequest'}
res = []
def check(name, cond, extra=''):
    res.append(bool(cond)); print(('OK  ' if cond else 'KO  ') + name + (f'   [{extra}]' if extra and not cond else ''))
def run(body, who='good'):
    return requests.post(base + 'run', json=body, cookies={'session_id': who}, headers=H)
good = dict(src='192.0.2.5', dst='pve01.lan', vmid='705', dry_run=True)

# --- validazione: ognuno di questi NON deve far partire un lavoro
njobs = len(mod.JOBS)
hostile = {
  'src con -oProxyCommand': dict(good, src='-oProxyCommand=touch /tmp/x@y'),
  'src con -o senza @': dict(good, src='-oProxyCommand=id'),
  'dst con -': dict(good, dst='-F/etc/passwd'),
  'src con spazio': dict(good, src='10.0.0.1 -v'),
  'src con ;': dict(good, src='10.0.0.1;id'),
  'src con $()': dict(good, src='$(id)'),
  'src con newline': dict(good, src='10.0.0.1\nid'),
  'user@ che inizia con -': dict(good, src='-x@host'),
  'vmid non numerico': dict(good, vmid='705;id'),
  'vmid negativo': dict(good, vmid='-5'),
  'target_vmid non numerico': dict(good, target_vmid='abc'),
  'mode sconosciuta': dict(good, mode='local --foo'),
  'staging con spazio': dict(good, staging='a b'),
  'staging con -': dict(good, staging='-x'),
  'dst_storage con ;': dict(good, dst_storage='x;y'),
  'identity inesistente': dict(good, identity='/non/esiste'),
  'identity relativa': dict(good, identity='chiave'),
  'identity con ..': dict(good, identity='/tmp/../etc/passwd'),
  'identity con -': dict(good, identity='-i'),
  'campo lista': dict(good, src=['a']),
  'campo oggetto': dict(good, vmid={'x': 1}),
  'campo bool': dict(good, src=True),
}
for name, body in hostile.items():
    r = run(body)
    check(f'rifiutato (400): {name}', r.status_code == 400 and r.json().get('ok') is False, f'{r.status_code} {r.text[:80]}')
check('nessun lavoro avviato dalle richieste ostili', len(mod.JOBS) == njobs)

# --- richieste valide
for name, body in {
  'IPv4': good, 'hostname': dict(good, src='pve02.example.com'), 'user@host': dict(good, src='root@192.0.2.5'),
  'IPv6': dict(good, src='fd00::5'), 'vmid numerico (int)': dict(good, vmid=705, target_vmid=9705),
  'storage con . _ -': dict(good, dst_storage='local-lvm', staging='bkp_2.0'),
  'mode pbs': dict(good, mode='pbs'),
}.items():
    r = run(body); check(f'accettato: {name}', r.status_code == 200 and r.json().get('ok') is True, f'{r.status_code} {r.text[:80]}')
keyfile = os.path.join(work, 'chiave'); open(keyfile, 'w').write('x')
r = run(dict(good, identity=keyfile)); check('accettato: identity assoluta esistente', r.status_code == 200, r.text[:80])
last = list(mod.JOBS.values())[-1]
check('la chiave indicata finisce in -i', last['args'][last['args'].index('-i') + 1] == keyfile)

# --- eliminazione dell'origine (opzione pericolosa: solo true JSON, mai incompatibile con no_start)
def args_of(r): return mod.JOBS[r.json()['job_id']]['args']
r = run(good); check('di default NESSUN --destroy-src e nessuna opzione "marca" (il lock e\' automatico nello script)',
                     r.status_code == 200 and '--destroy-src' not in args_of(r) and not any('mark' in a for a in args_of(r)), str(args_of(r)))
r = run(dict(good, destroy_src=False)); check('destroy_src=false -> nessun flag', r.status_code == 200 and '--destroy-src' not in args_of(r))
r = run(dict(good, destroy_src=None)); check('destroy_src=null -> nessun flag', r.status_code == 200 and '--destroy-src' not in args_of(r))
r = run(dict(good, dry_run=False, destroy_src=True)); check('destroy_src=true -> --destroy-src', r.status_code == 200 and '--destroy-src' in args_of(r), str(args_of(r)))
r = run(dict(good, dry_run=True, destroy_src=True)); check('destroy_src=true con dry-run: passato (la simulazione mostra il piano)', r.status_code == 200 and '--destroy-src' in args_of(r) and '-n' in args_of(r))
n = len(mod.JOBS)
for name, v in {'stringa "false"': 'false', 'stringa "true"': 'true', 'numero 1': 1, 'stringa vuota': '', 'lista': [True], 'oggetto': {'a': 1}}.items():
    r = run(dict(good, destroy_src=v)); check(f'destroy_src {name}: rifiutato (400)', r.status_code == 400 and r.json().get('ok') is False, f'{r.status_code} {r.text[:80]}')
r = run(dict(good, dry_run=False, destroy_src=True, no_start=True)); check('destroy_src + no_start: rifiutato (400)', r.status_code == 400 and 'no_start' in r.json().get('error', ''), f'{r.status_code} {r.text[:100]}')
check('nessun lavoro avviato dalle richieste rifiutate', len(mod.JOBS) == n)

# --- audit con l'utente vero
calls = audit.CALLS
check('audit: avvio registrato con l\'utente vero (alice), non "plugin"', any(c[0] == 'alice' and c[1] == 'xmigrate_ct_start' for c in calls) and not any(c[0] == 'plugin' for c in calls))
check('il lavoro ricorda chi l\'ha avviato', last['user'] == 'alice')
r = requests.get(base + 'jobs', cookies={'session_id': 'good'}); check('elenco lavori include l\'utente', r.json()['jobs'][0]['user'] == 'alice')

# --- permessi
def codes(who):
    c = {'session_id': who}
    return {
      'ui': requests.get(base + 'ui', cookies=c).status_code,
      'jobs': requests.get(base + 'jobs', cookies=c).status_code,
      'job': requests.get(base + 'job?id=x', cookies=c).status_code,
      'ssh-key': requests.get(base + 'ssh-key', cookies=c).status_code,
      'run': requests.post(base + 'run', json=good, cookies=c, headers=H).status_code,
      'install': requests.post(base + 'install-ssh-key', json={'host': 'h', 'password': 'p'}, cookies=c, headers=H).status_code,
    }
rbac.PERMS = {'plugins.view'}
c = codes('viewer')
check('senza plugins.manage: run, install, jobs, job, ssh-key -> 403', all(c[k] == 403 for k in ('run', 'install', 'jobs', 'job', 'ssh-key')), str(c))
check('senza plugins.manage: la pagina ui si apre comunque (200)', c['ui'] == 200)
r = requests.post(base + 'run', json=good, cookies={'session_id': 'viewer'}, headers=H)
check('il 403 spiega il permesso mancante', r.json().get('code') == 'MISSING_PERMISSION' and r.json().get('required') == 'plugins.manage')
n = len(mod.JOBS); run(good, 'viewer'); check('403: nessun lavoro avviato', len(mod.JOBS) == n)
rbac.PERMS = {'plugins.view', 'plugins.manage'}
c = codes('viewer'); check('con plugins.manage nessuna route è bloccata dal permesso (job 404 e install 500 sono esiti normali)', all(v not in (401, 403) for v in c.values()) and c['run'] == 200 and c['jobs'] == 200, str(c))
# controllo permessi non disponibile (altra versione di PegaProx): ripiega sul ruolo
rbac.RAISE = True
c_admin = codes('good'); c_viewer = codes('viewer')
check('se il controllo permessi dà errore: admin passa, viewer no', c_admin['run'] == 200 and c_viewer['run'] == 403 and c_viewer['install'] == 403, f'{c_admin} {c_viewer}')
rbac.RAISE = False

# --- install-ssh-key
mod._install_pubkey = lambda h, u, pw, pk: (True, 'installata')
def inst(body): return requests.post(base + 'install-ssh-key', json=body, cookies={'session_id': 'good'}, headers=H)
check('install: host valido + utente root -> 200', inst({'host': '192.0.2.5', 'user': 'root', 'password': 'p'}).status_code == 200)
for name, b in {'host -o': {'host': '-oProxyCommand=x', 'password': 'p'}, 'host con @': {'host': 'a@b', 'password': 'p'},
                'utente -o': {'host': 'h', 'user': '-oProxyCommand=x', 'password': 'p'}, 'utente con spazio': {'host': 'h', 'user': 'a b', 'password': 'p'},
                'password numerica': {'host': 'h', 'password': 123}, 'senza password': {'host': 'h'}}.items():
    r = inst(b); check(f'install rifiutato: {name}', r.status_code == 400, f'{r.status_code} {r.text[:80]}')
check('audit: installazione chiave registrata senza password', any(c[1] == 'xmigrate_ct_install_key' and 'segret' not in str(c) and c[0] == 'alice' for c in audit.CALLS))

print('\n%d prove, %d fallite' % (len(res), res.count(False))); sys.exit(1 if False in res else 0)

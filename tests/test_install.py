#!/usr/bin/env python3
"""Test di install.sh su una finta installazione di PegaProx (nessun accesso alla rete)."""
import os, sys, shutil, subprocess, tempfile, tarfile, time, stat

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
INSTALL = os.path.join(ROOT, 'install.sh')
SRC_PLUGIN = os.path.join(ROOT, 'plugin', 'xmigrate_ct')
FILES = ['manifest.json', '__init__.py', 'ui.html', 'xmigrate-ct.sh']
IS_ROOT = os.geteuid() == 0
res = []

def check(name, cond, extra=''):
    res.append(bool(cond)); print(('OK  ' if cond else 'KO  ') + name + (f'   [{extra}]' if extra and not cond else ''))

class Box:
    """Ambiente isolato: cartella plugin, backup, shim di systemctl, HOME."""
    def __init__(self):
        self.d = tempfile.mkdtemp(prefix='xmi_')
        self.plugins = os.path.join(self.d, 'plugins'); os.makedirs(self.plugins)
        self.backups = os.path.join(self.d, 'backups')
        self.shim = os.path.join(self.d, 'shim'); os.makedirs(self.shim)
        self.calls = os.path.join(self.d, 'calls.log')
        self.sysenv = os.path.join(self.d, 'sysenv'); self.syswd = os.path.join(self.d, 'syswd')
        open(self.sysenv, 'w').write(''); open(self.syswd, 'w').write('')
        self.write_shim('systemctl', f'''#!/bin/bash
echo "systemctl $*" >> "{self.calls}"
case "$*" in
  "show pegaprox -p Environment --value") cat "{self.sysenv}" ;;
  "show pegaprox -p WorkingDirectory --value") cat "{self.syswd}" ;;
  "cat pegaprox") exit 0 ;;
  "restart pegaprox") exit 0 ;;
  *) exit 0 ;;
esac
''')
    def write_shim(self, name, body):
        p = os.path.join(self.shim, name); open(p, 'w').write(body); os.chmod(p, 0o755)
    def env(self, **extra):
        e = dict(os.environ, HOME=self.d, XMIGRATE_BACKUP_DIR=self.backups, PATH=self.shim + os.pathsep + os.environ['PATH'])
        e.pop('PEGAPROX_PLUGINS_DIR', None)
        e.update(extra); return e
    def run(self, *args, env=None, cwd=None, stdin=None, script=None):
        p = subprocess.run(['bash', script or INSTALL, *args], env=env or self.env(), cwd=cwd or self.d, capture_output=True, text=True, timeout=120, stdin=stdin)
        return p.returncode, p.stdout, p.stderr
    @property
    def dest(self): return os.path.join(self.plugins, 'xmigrate_ct')
    def read(self, f): return open(os.path.join(self.dest, f), encoding='utf-8', newline='').read()
    def backups_list(self): return sorted(os.listdir(self.backups)) if os.path.isdir(self.backups) else []

def mode(p): return stat.S_IMODE(os.stat(p).st_mode)
def same_as_repo(box):
    return all(open(os.path.join(SRC_PLUGIN, f), 'rb').read() == open(os.path.join(box.dest, f), 'rb').read() for f in FILES)

def make_tarball(path, mutate=None, version=None):
    """Tarball come quello di codeload.github.com: cartella radice <repo>-<ref>/ con plugin/xmigrate_ct dentro."""
    stage = tempfile.mkdtemp(prefix='xmt_'); top = os.path.join(stage, 'pegaprox-xmigrate-ct-main', 'plugin', 'xmigrate_ct')
    os.makedirs(top)
    for f in FILES: shutil.copy(os.path.join(SRC_PLUGIN, f), top)
    if version:
        m = open(os.path.join(top, 'manifest.json')).read().replace('"version": "1.1.0"', f'"version": "{version}"')
        open(os.path.join(top, 'manifest.json'), 'w').write(m)
    if mutate: mutate(top)
    with tarfile.open(path, 'w:gz') as t: t.add(os.path.join(stage, 'pegaprox-xmigrate-ct-main'), arcname='pegaprox-xmigrate-ct-main')
    return path

# 1) prima installazione da copia locale ------------------------------------------------------
b = Box()
rc, out, err = b.run('--dir', b.plugins)
check('prima installazione: exit 0', rc == 0, out + err)
check('i 4 file sono identici a quelli della repo', same_as_repo(b))
check('xmigrate-ct.sh eseguibile (0755), gli altri 0644', mode(b.dest + '/xmigrate-ct.sh') == 0o755 and all(mode(b.dest + '/' + f) == 0o644 for f in FILES[:3]))
check('nessun file temporaneo lasciato nella cartella del plugin', sorted(os.listdir(b.dest)) == sorted(FILES), str(os.listdir(b.dest)))
check('prima installazione: nessun backup e istruzioni di attivazione (Rescan)', b.backups_list() == [] and 'Rescan' in out, out[-300:])
check('le istruzioni citano chiave SSH, plugins.manage e simulazione', 'authorized_keys' in out and 'plugins.manage' in out and 'simulazione' in out)

# 2) aggiornamento: backup della versione vecchia ---------------------------------------------
os.makedirs(b.dest + '/__pycache__'); open(b.dest + '/__pycache__/x.pyc', 'w').write('x')
m = open(b.dest + '/manifest.json').read().replace('"version": "1.1.0"', '"version": "0.9.0"')
open(b.dest + '/manifest.json', 'w').write(m)
rc, out, err = b.run('--dir', b.plugins)
check('aggiornamento: exit 0 e log "0.9.0 -> 1.1.0"', rc == 0 and '0.9.0 -> 1.1.0' in out, out + err)
bl = b.backups_list()
check('backup creato con la versione vecchia (0.9.0)', len(bl) == 1 and '"0.9.0"' in open(os.path.join(b.backups, bl[0], 'manifest.json')).read(), str(bl))
check('__pycache__ rimossa e file di nuovo identici alla repo', not os.path.exists(b.dest + '/__pycache__') and same_as_repo(b))
check('il backup non e\' leggibile da altri (0700)', mode(os.path.join(b.backups, bl[0])) == 0o700, oct(mode(os.path.join(b.backups, bl[0]))))
check('aggiornamento: istruzioni = disattiva/riattiva il plugin', 'disattiva e riattiva' in out)

# 3) ritenzione dei backup ----------------------------------------------------------------------
for _ in range(7): b.run('--dir', b.plugins)
check('si tengono al massimo 5 backup', len(b.backups_list()) == 5, str(b.backups_list()))
rc, out, err = b.run('--dir', b.plugins, '--no-backup'); n = len(b.backups_list())
check('--no-backup: nessun nuovo backup', rc == 0 and n == 5)

# 4) dry-run ---------------------------------------------------------------------------------
b = Box()
rc, out, err = b.run('--dir', b.plugins, '--dry-run')
check('--dry-run: exit 0, nulla installato, nessun backup', rc == 0 and not os.path.exists(b.dest) and b.backups_list() == [] and 'dry-run' in out, out + err)
b.run('--dir', b.plugins); before = {f: open(b.dest + '/' + f, 'rb').read() for f in FILES}
open(b.dest + '/ui.html', 'a').write('<!-- modificato -->'); mod = open(b.dest + '/ui.html', 'rb').read()
nb = len(b.backups_list()); rc, out, err = b.run('--dir', b.plugins, '--dry-run')
check('--dry-run su un\'installazione esistente: non cambia niente', rc == 0 and open(b.dest + '/ui.html', 'rb').read() == mod and len(b.backups_list()) == nb)

# 5) rilevamento della cartella ------------------------------------------------------------------
b = Box()
rc, out, err = b.run(env=b.env(PEGAPROX_PLUGINS_DIR=b.plugins))
check('PEGAPROX_PLUGINS_DIR usata senza --dir', rc == 0 and same_as_repo(b) and 'ambiente' in out, out + err)
b = Box(); wd = os.path.join(b.d, 'svc'); os.makedirs(wd + '/plugins'); open(b.syswd, 'w').write(wd + '\n')
rc, out, err = b.run()
check('cartella ricavata da systemd (WorkingDirectory/plugins)', rc == 0 and os.path.isdir(wd + '/plugins/xmigrate_ct') and not os.path.exists(b.dest), out + err)
b = Box(); alt = os.path.join(b.d, 'volume'); os.makedirs(alt); open(b.sysenv, 'w').write(f'FOO=1 PEGAPROX_PLUGINS_DIR={alt} BAR=2\n')
rc, out, err = b.run()
check('cartella ricavata da Environment del servizio (Docker/volumi)', rc == 0 and os.path.isdir(alt + '/xmigrate_ct'), out + err)
if not any(os.path.isdir(p) for p in ('/opt/PegaProx/plugins', '/opt/pegaprox/plugins', '/var/lib/pegaprox/plugins', '/usr/lib/pegaprox/plugins')):
    b = Box(); rc, out, err = b.run()
    check('nessuna cartella trovata: errore chiaro con suggerimento --dir', rc == 1 and 'non trovata' in err and '--dir' in err, err)
b = Box(); rc, out, err = b.run('--dir', os.path.join(b.d, 'non-esiste'))
check('--dir inesistente: errore', rc == 1 and 'inesistente' in err, err)

# 6) da tarball (curl | bash) e fine riga -----------------------------------------------------------
b = Box(); tb = make_tarball(os.path.join(b.d, 'src.tar.gz'), version='9.9.9')
lone = os.path.join(b.d, 'solo'); os.makedirs(lone); shutil.copy(INSTALL, lone)
rc, out, err = b.run('--dir', b.plugins, env=b.env(XMIGRATE_ARCHIVE_URL='file://' + tb), cwd=lone, script=lone + '/install.sh')
# nota: bash lone/install.sh -> non ci sono file locali accanto, quindi scarica
rc2 = rc
check('da tarball: installa la versione dell\'archivio (9.9.9)', rc == 0 and '"9.9.9"' in b.read('manifest.json') and 'scarico' in out, out + err)
b = Box(); tb = make_tarball(os.path.join(b.d, 'src.tar.gz'), version='9.9.9')
p = subprocess.run(['bash', '-s', '--', '--dir', b.plugins], stdin=open(INSTALL, 'rb'), cwd=ROOT, env=b.env(XMIGRATE_ARCHIVE_URL='file://' + tb), capture_output=True, text=True)
check('"curl | bash" lanciato DENTRO la repo scarica comunque (non usa i file locali)', p.returncode == 0 and '"9.9.9"' in b.read('manifest.json'), p.stdout + p.stderr)
b = Box()
def crlf(top):
    p = top + '/xmigrate-ct.sh'; data = open(p, 'rb').read().replace(b'\n', b'\r\n'); open(p, 'wb').write(data)
tb = make_tarball(os.path.join(b.d, 'src.tar.gz'), mutate=crlf)
rc, out, err = b.run('--dir', b.plugins, env=b.env(XMIGRATE_ARCHIVE_URL='file://' + tb), cwd=lone, script=lone + '/install.sh')
inst = open(b.dest + '/xmigrate-ct.sh', 'rb').read() if os.path.exists(b.dest + '/xmigrate-ct.sh') else b''
check('sorgente con fine riga CRLF: installato in LF e valido', rc == 0 and len(inst) > 1000 and b'\r' not in inst and subprocess.run(['bash', '-n', b.dest + '/xmigrate-ct.sh']).returncode == 0, out + err)
b = Box(); rc, out, err = b.run('--dir', b.plugins, env=b.env(XMIGRATE_ARCHIVE_URL='file://' + b.d + '/non-esiste.tar.gz'), cwd=lone, script=lone + '/install.sh')
check('download impossibile: errore chiaro, niente installato', rc == 1 and 'download non riuscito' in err and not os.path.exists(b.dest), err)
b = Box(); open(b.d + '/vuoto.tar.gz', 'wb').write(b''); rc, out, err = b.run('--dir', b.plugins, env=b.env(XMIGRATE_ARCHIVE_URL='file://' + b.d + '/vuoto.tar.gz'), cwd=lone, script=lone + '/install.sh')
check('archivio non valido: errore, niente installato', rc == 1 and not os.path.exists(b.dest), err)

# 7) sorgente rotto: l'installazione esistente resta intatta ------------------------------------------
b = Box(); b.run('--dir', b.plugins); keep = {f: open(b.dest + '/' + f, 'rb').read() for f in FILES}
for what, fn in {'xmigrate-ct.sh con errore di sintassi': lambda top: open(top + '/xmigrate-ct.sh', 'a').write('\nfi fi ))\n'),
                 '__init__.py con errore di sintassi': lambda top: open(top + '/__init__.py', 'a').write('\ndef (:\n'),
                 'manifest.json non valido': lambda top: open(top + '/manifest.json', 'w').write('{non json'),
                 'manifest con id diverso': lambda top: open(top + '/manifest.json', 'w').write('{"id": "altro"}'),
                 'file mancante (ui.html)': lambda top: os.remove(top + '/ui.html')}.items():
    tb = make_tarball(os.path.join(b.d, 'bad.tar.gz'), mutate=fn)
    rc, out, err = b.run('--dir', b.plugins, env=b.env(XMIGRATE_ARCHIVE_URL='file://' + tb), cwd=lone, script=lone + '/install.sh')
    check(f'{what}: rifiutato e installazione esistente intatta', rc == 1 and all(open(b.dest + '/' + f, 'rb').read() == keep[f] for f in FILES), (out + err)[-200:])

# 8) migrazione in corso ------------------------------------------------------------------------------
b = Box(); b.run('--dir', b.plugins)
long = os.path.join(b.d, 'job'); os.makedirs(long); sc = long + '/xmigrate-ct.sh'
open(sc, 'w').write('#!/bin/bash\nsleep 20\n'); os.chmod(sc, 0o755)
job = subprocess.Popen(['bash', sc])
time.sleep(0.5)
rc, out, err = b.run('--dir', b.plugins)
check('migrazione in corso: aggiornamento rifiutato', rc == 1 and 'in corso' in err, err)
rc, out, err = b.run('--dir', b.plugins, '--uninstall')
check('migrazione in corso: anche la rimozione e\' rifiutata', rc == 1 and os.path.isdir(b.dest), err)
rc, out, err = b.run('--dir', b.plugins, '--force')
check('--force: prosegue', rc == 0, err)
job.kill(); job.wait()

# 9) sostituzione atomica: uno script in esecuzione non viene riscritto a meta' ----------------------------
b = Box(); os.makedirs(b.dest)
for f in FILES: shutil.copy(os.path.join(SRC_PLUGIN, f), b.dest)
open(b.dest + '/xmigrate-ct.sh', 'w').write('#!/bin/bash\nsleep 2\necho OLD_FINISHED\n')
outf = os.path.join(b.d, 'job.out')
job = subprocess.Popen(['bash', b.dest + '/xmigrate-ct.sh'], stdout=open(outf, 'w'), stderr=subprocess.STDOUT)
time.sleep(0.5)
rc, out, err = b.run('--dir', b.plugins, '--force')
job.wait()
check('script in esecuzione durante l\'aggiornamento: finisce regolarmente con il vecchio codice', rc == 0 and open(outf).read().strip() == 'OLD_FINISHED' and same_as_repo(b), open(outf).read())

# 10) rimozione -----------------------------------------------------------------------------------------
b = Box(); b.run('--dir', b.plugins)
rc, out, err = b.run('--dir', b.plugins, '--uninstall')
check('--uninstall: cartella rimossa, backup fatto, nota sulla chiave SSH', rc == 0 and not os.path.exists(b.dest) and len(b.backups_list()) == 1 and 'xmigrate_ct_ed25519' in out, out + err)
os.makedirs(b.plugins + '/altro_plugin'); open(b.plugins + '/altro_plugin/manifest.json', 'w').write('{}')
b.run('--dir', b.plugins); b.run('--dir', b.plugins, '--uninstall')
check('gli altri plugin restano dove sono', os.path.isfile(b.plugins + '/altro_plugin/manifest.json'))
rc, out, err = b.run('--dir', b.plugins, '--uninstall')
check('--uninstall senza plugin installato: errore chiaro', rc == 1 and 'non e\' installato' in err, err)

# 11) riavvio del servizio ---------------------------------------------------------------------------------
b = Box(); b.run('--dir', b.plugins)
check('senza --restart il servizio non viene toccato', 'restart' not in open(b.calls).read() if os.path.exists(b.calls) else True)
rc, out, err = b.run('--dir', b.plugins, '--restart')
check('--restart: systemctl restart pegaprox eseguito', rc == 0 and 'systemctl restart pegaprox' in open(b.calls).read() and 'F5' in out, out + err)
b.write_shim('systemctl', '#!/bin/bash\n[ "$1" = restart ] && exit 1\nexit 0\n')
rc, out, err = b.run('--dir', b.plugins, '--restart')
check('--restart fallito: avviso, installazione comunque riuscita', rc == 0 and 'riavvio non riuscito' in err)

# 12) dipendenze (jq / ssh) ------------------------------------------------------------------------------------
def minimal_path(box, drop):
    """PATH con solo i comandi che servono all'installer, senza quelli in `drop`."""
    d = os.path.join(box.d, 'minbin'); os.makedirs(d, exist_ok=True)
    for c in ('bash', 'sed', 'cp', 'mv', 'chmod', 'chown', 'mkdir', 'rm', 'find', 'sort', 'date', 'stat', 'readlink', 'dirname', 'mktemp',
              'tar', 'curl', 'python3', 'id', 'pgrep', 'env', 'cat', 'head', 'ls', 'sleep', 'grep'):
        if c in drop: continue
        src = shutil.which(c)
        if src and not os.path.exists(os.path.join(d, c)): os.symlink(src, os.path.join(d, c))
    return d
b = Box(); mp = minimal_path(b, drop=('jq', 'ssh', 'ssh-keygen'))
log = os.path.join(b.d, 'pm.log')
for pm in ('apt-get',):
    open(os.path.join(mp, pm), 'w').write(f'#!/bin/bash\necho "{pm} $*" >> "{log}"\n'); os.chmod(os.path.join(mp, pm), 0o755)
rc, out, err = b.run('--dir', b.plugins, env=dict(b.env(), PATH=mp))
if IS_ROOT:
    check('jq mancante (root): installato con il gestore di pacchetti', rc == 0 and 'apt-get install -y -qq jq' in open(log).read(), out + err)
else:
    check('jq mancante (non root): avviso e installazione del plugin comunque riuscita', rc == 0 and 'installa jq a mano' in err, out + err)
check('ssh/ssh-keygen mancanti: avviso con il pacchetto da installare', 'mancano: ssh' in err and 'openssh-client' in err, err)
b = Box(); mp = minimal_path(b, drop=('jq',)); log = os.path.join(b.d, 'pm.log')
open(os.path.join(mp, 'apt-get'), 'w').write(f'#!/bin/bash\necho "$*" >> "{log}"\n'); os.chmod(os.path.join(mp, 'apt-get'), 0o755)
rc, out, err = b.run('--dir', b.plugins, '--no-deps', env=dict(b.env(), PATH=mp))
check('--no-deps: nessuna installazione e nessun avviso', rc == 0 and not os.path.exists(log) and 'mancano' not in err)
b = Box(); open(os.path.join(b.shim, 'jq'), 'w').write('#!/bin/bash\nexit 0\n'); os.chmod(os.path.join(b.shim, 'jq'), 0o755)
rc, out, err = b.run('--dir', b.plugins)
check('jq presente: nessun tentativo di installarlo', 'jq non trovato' not in out)

# 13) proprietario dei file (solo root) -----------------------------------------------------------------------------
if IS_ROOT:
    b = Box(); os.chown(b.plugins, 1234, 4321)
    rc, out, err = b.run('--dir', b.plugins)
    st = [os.stat(b.dest + '/' + f) for f in FILES] + [os.stat(b.dest)]
    check('i file prendono il proprietario della cartella plugin (utente del servizio)', rc == 0 and all((s.st_uid, s.st_gid) == (1234, 4321) for s in st), str([(s.st_uid, s.st_gid) for s in st]))

# 14) argomenti ---------------------------------------------------------------------------------------------------
b = Box()
rc, out, err = b.run('--help'); check('--help: exit 0 e testo di uso', rc == 0 and 'Uso:' in out)
rc, out, err = b.run('--boh'); check('opzione sconosciuta: errore', rc == 1 and 'sconosciuta' in err)
rc, out, err = b.run('--dir', b.plugins, '--ref', '$(touch /tmp/pwn_xmi)'); check('--ref con caratteri pericolosi rifiutato', rc == 1 and not os.path.exists('/tmp/pwn_xmi'))
rc, out, err = b.run('--dir', b.plugins, env=b.env(XMIGRATE_REPO='a;b')); check('XMIGRATE_REPO non valido rifiutato', rc == 1)

print('\n%d prove, %d fallite' % (len(res), res.count(False))); sys.exit(1 if False in res else 0)

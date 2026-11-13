import json, logging, os, sys, tempfile, time
logging.disable(logging.CRITICAL)
from playwright.sync_api import sync_playwright
import harness

NEW = harness.PLUGIN_DIR
STUB = harness.STUB
work = tempfile.mkdtemp(prefix='xm_')
srv, mod, LOG, plug = harness.build(NEW, work, STUB)
base = f'http://127.0.0.1:{srv.server_port}'
results = []
def check(name, cond, extra=''):
    results.append((name, bool(cond)))
    print(('OK  ' if cond else 'KO  ') + name + (f'   [{extra}]' if extra and not cond else ''))

# 0) route ui servita come pagina HTML dal plugin vero
import requests
r = requests.get(base + '/api/plugins/xmigrate_ct/api/ui', cookies={'session_id': 'good'})
check('GET ui -> 200 text/html con la pagina', r.status_code == 200 and r.headers['Content-Type'].startswith('text/html') and 'XMigrate CT' in r.text)
r = requests.get(base + '/api/plugins/xmigrate_ct/api/ui')
check('senza sessione la route resta protetta (401)', r.status_code == 401)
os.rename(os.path.join(plug, 'ui.html'), os.path.join(plug, 'ui.html.bak'))
r = requests.get(base + '/api/plugins/xmigrate_ct/api/ui', cookies={'session_id': 'good'})
check('ui.html mancante -> 404 JSON leggibile', r.status_code == 404 and 'ui.html' in r.json().get('error', ''))
os.rename(os.path.join(plug, 'ui.html.bak'), os.path.join(plug, 'ui.html'))

with sync_playwright() as p:
    b = harness.launch(p)
    ctx = b.new_context(viewport={'width': 1300, 'height': 900})
    ctx.add_cookies([{'name': 'session_id', 'value': 'good', 'url': base}])
    page = ctx.new_page()
    errors = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    dialogs = []
    def on_dialog(d):
        dialogs.append(d.message)
        d.accept() if ACCEPT[0] else d.dismiss()
    ACCEPT = [True]
    page.on('dialog', on_dialog)

    page.goto(base + '/host?theme=corp-light')
    f = page.frame_locator('#f')
    f.locator('h1').wait_for()
    f.locator('#srcToggle').click(); f.locator('#dstToggle').click()   # modalità manuale: i campi di testo
    check('la pagina si apre nella iframe con sandbox', f.locator('h1').inner_text() == 'XMigrate CT')
    check('tema chiaro applicato da ?theme=corp-light',
          page.frames[1].evaluate("document.documentElement.getAttribute('data-theme')") == 'light')
    check('nessun lavoro all\'inizio', 'Nessun lavoro' in f.locator('#jobs').inner_text())
    check('dry-run spuntato di default e bottone "Simula"', f.locator('#dry_run').is_checked() and f.locator('#go').inner_text() == 'Simula')

    # validazione lato pagina: nessuna richiesta parte
    n0 = len([l for l in LOG if l[1] == 'run'])
    f.locator('#src').fill('10.0.0.1'); f.locator('#dst').fill('10.0.0.2'); f.locator('#vmid').fill('abc')
    f.locator('#go').click()
    check('VMID non numerico -> errore e nessuna richiesta', 'numero' in f.locator('#formMsg').inner_text() and len([l for l in LOG if l[1] == 'run']) == n0)
    f.locator('#vmid').fill('705'); f.locator('#dst').fill('-oProxyCommand=x@y')
    f.locator('#go').click()
    check('host che inizia con "-" rifiutato dalla pagina', 'non validi' in f.locator('#formMsg').inner_text() and len([l for l in LOG if l[1] == 'run']) == n0)
    f.locator('#dst').fill('10.0.0.2')

    # simulazione
    f.locator('#go').click()
    f.locator('#meta .badge.running').wait_for(timeout=5000)
    check('durante il lavoro il bottone e\' disattivato', f.locator('#go').is_disabled())
    f.locator('#meta .badge.done').wait_for(timeout=10000)
    log_html = f.locator('#log').inner_html(); log_txt = f.locator('#log').inner_text()
    check('log in streaming: riga dello stub presente', 'avvio stub args' in log_txt and 'riga finale' in log_txt)
    check('colori ANSI resi come span (non codici grezzi)', 'class="b blu"' in log_html and '\x1b' not in log_html and '[1;34m' not in log_txt)
    check('HTML nel log escapato (nessun <b> reale)', '&lt;b&gt;&amp;amp;' in log_html and '<b>' not in log_html.replace('<span', ''))
    check('badge done e codice 0', 'codice 0' in f.locator('#meta').inner_text())
    run_calls = [l for l in LOG if l[1] == 'run']
    body = json.loads(run_calls[-1][2])
    check('payload: tutte stringhe/bool come richiede il backend', body['vmid'] == '705' and body['src'] == '10.0.0.1' and body['dry_run'] is True and isinstance(body['target_vmid'], str))
    job = [j for j in mod.JOBS.values()][-1]
    args = ' '.join(job['args'])
    check('argomenti passati allo script: -s -d -c -i -n -y', args.startswith('-s 10.0.0.1 -d 10.0.0.2 -c 705 -i ') and ' -n ' in (' ' + args + ' ') and args.endswith('-y') and '--cleanup-src' not in args, args)
    check('chiave del plugin usata (-i) e creata dal plugin', os.path.isfile(os.path.join(work, 'home', '.ssh', 'xmigrate_ct_ed25519')))
    check('elenco lavori mostra il job e il bottone torna attivo', 'done' in f.locator('#jobs').inner_text() and f.locator('#go').is_enabled())

    # migrazione vera: conferma, poi annullo, poi accetto
    f.locator('#dry_run').uncheck()
    check('senza dry-run il bottone diventa "Avvia migrazione"', f.locator('#go').inner_text() == 'Avvia migrazione')
    f.locator('#cleanup_src').check(); f.locator('#no_start').check()
    f.locator('#target_vmid').fill('9705'); f.locator('#dst_storage').fill('local-lvm'); f.locator('#staging').fill('bkp'); f.locator('#mode').select_option('local')
    n1 = len([l for l in LOG if l[1] == 'run'])
    ACCEPT[0] = False
    f.locator('#go').click(); time.sleep(0.6)
    check('conferma annullata -> nessun avvio', len([l for l in LOG if l[1] == 'run']) == n1 and len(dialogs) == 1 and 'Migrare il CT 705' in dialogs[0])
    ACCEPT[0] = True
    f.locator('#go').click()
    f.locator('#meta .badge.running').wait_for(timeout=5000)
    f.locator('#meta .badge.done').wait_for(timeout=10000)
    job = [j for j in mod.JOBS.values()][-1]
    args = job['args']
    check('migrazione vera: niente -n, ci sono i flag scelti',
          '-n' not in args and '--cleanup-src' in args and '--no-start' in args and
          args[args.index('-t') + 1] == '9705' and args[args.index('--dst-storage') + 1] == 'local-lvm' and
          args[args.index('--staging') + 1] == 'bkp' and args[args.index('--mode') + 1] == 'local', ' '.join(args))

    # ricarica pagina: ricorda i campi e riapre l'ultimo lavoro
    page.reload(); f = page.frame_locator('#f'); f.locator('h1').wait_for()
    f.locator('#meta .badge.done').wait_for(timeout=5000)
    check('dopo F5 origine/destinazione/modalità sono ricordati, storage e staging no', f.locator('#src').input_value() == '10.0.0.1' and f.locator('#mode').input_value() == 'local' and f.locator('#dst_storage').input_value() == '' and f.locator('#staging').input_value() == '')
    check('dopo F5 l\'ultimo lavoro viene riaperto, 2 righe in elenco', f.locator('tr.job').count() == 2 and 'riga finale' in f.locator('#log').inner_text())
    first = f.locator('tr.job').nth(1); first.click()
    check('clic su un lavoro dell\'elenco apre il suo log', first.get_attribute('class').find('sel') != -1)

    # errore dello script (rc != 0): script vero, qui senza ssh nel container
    import shutil; shutil.copy(os.path.join(NEW, 'xmigrate-ct.sh'), os.path.join(plug, 'xmigrate-ct.sh'))
    page.request.get(base + '/__set_script?path=' + os.path.join(plug, 'xmigrate-ct.sh'))
    f.locator('#srcToggle').click(); f.locator('#dstToggle').click()
    f.locator('#src').fill('127.0.0.1'); f.locator('#dst').fill('127.0.0.2')
    f.locator('#vmid').fill('705'); f.locator('#dry_run').check(); f.locator('#go').click()
    f.locator('#meta .badge.failed').wait_for(timeout=40000)
    # senza ssh: 'ssh non trovato'; con ssh: connessione rifiutata/negata verso 127.0.0.1. In ogni caso un ERRORE leggibile
    check('script vero: fallisce con messaggio leggibile e badge failed', 'ERRORE' in f.locator('#log').inner_text() and 'codice 1' in f.locator('#meta').inner_text(), f.locator('#log').inner_text()[-200:])

    # chiave SSH
    f.locator('#sshBox summary').click(); f.locator('#showKey').click()
    f.locator('#pubkey').wait_for(state='visible')
    check('chiave pubblica mostrata', f.locator('#pubkey').input_value().startswith('ssh-ed25519 AAAAFAKEKEY'))
    mod._install_pubkey = lambda h, u, pw, pk: (False, 'boom di prova')
    f.locator('#ik_host').fill('10.0.0.9'); f.locator('#ik_pw').fill('segreta'); f.locator('#ik_go').click()
    f.locator('#sshMsg.err').wait_for()
    check('installazione chiave fallita -> messaggio e password svuotata', 'boom di prova' in f.locator('#sshMsg').inner_text() and f.locator('#ik_pw').input_value() == '')
    mod._install_pubkey = lambda h, u, pw, pk: (True, 'installata')
    f.locator('#ik_pw').fill('segreta'); f.locator('#ik_go').click()
    f.locator('#sshMsg.ok').wait_for()
    check('installazione chiave riuscita', 'installata su 10.0.0.9' in f.locator('#sshMsg').inner_text())

    # sessione scaduta: errore comprensibile
    ctx.clear_cookies(); ctx.add_cookies([{'name': 'session_id', 'value': 'scaduta', 'url': base}])
    f.locator('#go').click()
    f.locator('#formMsg.err').wait_for()
    check('sessione scaduta -> messaggio che lo spiega', 'sessione scaduta' in f.locator('#formMsg').inner_text())

    real = [e for e in errors if 'Failed to load resource' not in e]   # 401/404/500 provocati apposta
    check('nessun errore di pagina, script o CSP in console', not real, str(real[:3]))
    b.close()

# CSRF: senza intestazione la scrittura verrebbe respinta come fa PegaProx; la pagina la manda (provato sopra, i POST sono passati)
r = requests.post(base + '/api/plugins/xmigrate_ct/api/run', json={}, cookies={'session_id': 'good'})
check('il finto PegaProx respinge POST senza X-Requested-With (prova del controllo)', r.status_code == 403)
bad = [n for n, ok in results if not ok]
print('\n%d prove, %d fallite' % (len(results), len(bad)))
sys.exit(1 if bad else 0)

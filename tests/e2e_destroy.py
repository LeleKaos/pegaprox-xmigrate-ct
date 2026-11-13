import json, logging, os, sys, tempfile, time
logging.disable(logging.CRITICAL)
from playwright.sync_api import sync_playwright, expect
import harness

NEW = harness.PLUGIN_DIR
work = tempfile.mkdtemp(prefix='xmx_')
srv, mod, LOG, plug = harness.build(NEW, work, harness.STUB)
base = f'http://127.0.0.1:{srv.server_port}'
res = []
def check(name, cond, extra=''):
    res.append(bool(cond)); print(('OK  ' if cond else 'KO  ') + name + (f'   [{extra}]' if extra and not cond else ''))
def runs(): return [l for l in LOG if l[1] == 'run']

with sync_playwright() as p:
    b = harness.launch(p)
    ctx = b.new_context(viewport={'width': 1300, 'height': 1100})
    ctx.add_cookies([{'name': 'session_id', 'value': 'good', 'url': base}])
    page = ctx.new_page()
    errors, dialogs = [], []
    plan = {'answers': []}      # come rispondere ai prossimi dialoghi: ('accept', testo) | ('dismiss',)
    def on_dialog(d):
        dialogs.append((d.type, d.message))
        a = plan['answers'].pop(0) if plan['answers'] else ('dismiss',)
        if a[0] == 'accept': d.accept(a[1] if len(a) > 1 else '')
        else: d.dismiss()
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    page.on('dialog', on_dialog)

    page.goto(base + '/host?theme=modern-dark')
    f = page.frame_locator('#f'); f.locator('h1').wait_for()
    expect(f.locator('#src_cluster option')).not_to_have_count(1)

    # --- stato iniziale
    check('nessuna spunta "marca": il lock e\' automatico', f.locator('#mark_src').count() == 0)
    check('"Elimina il CT sull\'origine" spuntata? no, di default spenta', f.locator('#destroy_src').count() == 1 and not f.locator('#destroy_src').is_checked())
    check('"Non avviare" attivabile all\'inizio', f.locator('#no_start').is_enabled())
    note = f.locator('#endNote').inner_text()
    check('nota iniziale: marcato in automatico (lock: migrate) e come eliminare', 'automatico' in note and 'lock: migrate' in note and 'pct unlock <vmid>' in note, note)

    f.locator('#src_cluster').select_option('c3'); expect(f.locator('#vmid_sel option')).to_have_count(3)
    f.locator('#vmid_sel').select_option('705')
    check('con il CT scelto la nota cita il VMID', 'pct unlock 705 && pct destroy 705 --purge' in f.locator('#endNote').inner_text(), f.locator('#endNote').inner_text())
    f.locator('#dst_cluster').select_option('c1'); expect(f.locator('#dst_ip')).to_contain_text('192.0.2.3')
    f.locator('#target_vmid').fill('9705')

    # --- esclusione reciproca con "Non avviare"
    f.locator('#destroy_src').check()
    check('eliminazione spuntata -> "Non avviare" spento e disattivato', not f.locator('#no_start').is_checked() and f.locator('#no_start').is_disabled())
    note = f.locator('#endNote').inner_text()
    check('nota: ELIMINATO solo se la destinazione e\' avviata, altrimenti resta e viene marcata', 'ELIMINATO' in note and 'solo se' in note and 'marcato come migrato' in note, note)
    check('bottone senza dry-run mostra l\'eliminazione', (f.locator('#dry_run').uncheck() or True) and "elimina" in f.locator('#go').inner_text().lower(), f.locator('#go').inner_text())
    f.locator('#dry_run').check()
    check('con la simulazione il bottone resta "Simula"', f.locator('#go').inner_text() == 'Simula')
    f.locator('#destroy_src').uncheck()
    check('tolta la spunta, "Non avviare" torna attivabile', f.locator('#no_start').is_enabled())
    f.locator('#no_start').check()
    check('"Non avviare" spuntato -> eliminazione disattivata con spiegazione', f.locator('#destroy_src').is_disabled() and 'Non avviare' in f.locator('#destroy_hint').inner_text(), f.locator('#destroy_hint').inner_text())
    f.locator('#no_start').uncheck()
    check('tolto "Non avviare" -> eliminazione di nuovo attivabile e spiegazione sparita', f.locator('#destroy_src').is_enabled() and f.locator('#destroy_hint').inner_text() == '')

    # --- CT spento in origine: eliminazione non ammessa
    f.locator('#vmid_sel').select_option('300')
    f.locator('#destroy_src').check()
    check('CT spento + eliminazione -> avviso nella pagina', 'spento in origine' in f.locator('#destroy_hint').inner_text(), f.locator('#destroy_hint').inner_text())
    n = len(runs()); f.locator('#go').click(); time.sleep(0.4)
    check('invio bloccato con messaggio chiaro, nessuna richiesta', 'spento in origine' in f.locator('#formMsg').inner_text() and len(runs()) == n, f.locator('#formMsg').inner_text())
    f.locator('#vmid_sel').select_option('705')
    check('scegliendo un CT acceso l\'avviso sparisce', f.locator('#destroy_hint').inner_text() == '')

    # --- simulazione con eliminazione: nessun dialogo, flag passato (la simulazione mostra il piano)
    d0 = len(dialogs); f.locator('#go').click(); f.locator('#meta .badge.done').wait_for(timeout=10000)
    body = json.loads(runs()[-1][2])
    check('simulazione + eliminazione: nessuna conferma e payload con destroy_src', len(dialogs) == d0 and body['dry_run'] is True and body['destroy_src'] is True, str(body))
    job = list(mod.JOBS.values())[-1]
    check('il server passa -n e --destroy-src allo script', '-n' in job['args'] and '--destroy-src' in job['args'], str(job['args']))

    # --- migrazione vera con eliminazione: conferma forte (si scrive il VMID)
    f.locator('#dry_run').uncheck()
    n = len(runs()); dd = len(dialogs)
    plan['answers'] = [('dismiss',)]; f.locator('#go').click(); time.sleep(0.5)
    check('la conferma e\' un prompt che chiede il VMID e avvisa dell\'eliminazione', dialogs[dd][0] == 'prompt' and 'ELIMINATO' in dialogs[dd][1] and 'Scrivi il VMID 705' in dialogs[dd][1], str(dialogs[dd:]))
    check('prompt annullato -> nessuna migrazione', len(runs()) == n and 'non valida' in f.locator('#formMsg').inner_text())
    plan['answers'] = [('accept', '706')]; f.locator('#go').click(); time.sleep(0.5)
    check('VMID sbagliato -> nessuna migrazione', len(runs()) == n and 'non valida' in f.locator('#formMsg').inner_text())
    plan['answers'] = [('accept', '')]; f.locator('#go').click(); time.sleep(0.5)
    check('risposta vuota -> nessuna migrazione', len(runs()) == n)
    plan['answers'] = [('accept', ' 705 ')]; f.locator('#go').click()
    f.locator('#meta .badge.done').wait_for(timeout=10000)
    body = json.loads(runs()[-1][2])
    job = list(mod.JOBS.values())[-1]
    check('VMID giusto -> parte: payload senza dry_run e con destroy_src', len(runs()) == n + 1 and body['dry_run'] is False and body['destroy_src'] is True, str(body))
    check('script lanciato con --destroy-src e senza -n', '--destroy-src' in job['args'] and '-n' not in job['args'] and '--no-start' not in job['args'], str(job['args']))
    check('nel payload non c\'e\' nessuna opzione "mark"', not any('mark' in k for k in body), str(body))

    # --- migrazione vera SENZA eliminazione: conferma normale che parla del lock
    f.locator('#destroy_src').uncheck()
    n = len(runs()); dd = len(dialogs)
    plan['answers'] = [('dismiss',)]; f.locator('#go').click(); time.sleep(0.5)
    m = dialogs[dd][1]
    check('senza eliminazione: confirm normale che spiega lock: migrate', dialogs[dd][0] == 'confirm' and 'lock: migrate' in m and 'ELIMINATO' not in m and 'Migrare il CT 705' in m, str(dialogs[dd:]))
    check('annullata -> nessuna migrazione', len(runs()) == n)
    plan['answers'] = [('accept',)]; f.locator('#go').click(); f.locator('#meta .badge.done').wait_for(timeout=10000)
    job = list(mod.JOBS.values())[-1]
    check('migrazione senza flag di eliminazione', len(runs()) == n + 1 and '--destroy-src' not in job['args'], str(job['args']))

    # --- modalità manuale: stesse regole, e il VMID scritto non puo' iniettare HTML nella nota
    page.goto(base + '/host?theme=modern-dark'); f = page.frame_locator('#f'); f.locator('h1').wait_for()
    expect(f.locator('#src_cluster option')).not_to_have_count(1)
    f.locator('#srcToggle').click(); f.locator('#dstToggle').click()
    f.locator('#vmid').fill('<img src=x onerror=window.__pwn=1>')
    check('VMID scritto a mano: nella nota e\' testo, non HTML', f.locator('#endNote img').count() == 0 and '<img src=x' in f.locator('#endNote').inner_text(), f.locator('#endNote').inner_html()[:200])
    check('nessuno script eseguito dal VMID', page.evaluate("() => window.__pwn === undefined"))
    f.locator('#vmid').fill('555'); f.locator('#src').fill('10.1.1.1'); f.locator('#dst').fill('10.1.1.2')
    f.locator('#destroy_src').check()
    check('manuale: la nota cita il VMID scritto', 'pct destroy 555 --purge' in f.locator('#endNote').inner_text())
    check('manuale: l\'esclusione con "Non avviare" vale lo stesso', f.locator('#no_start').is_disabled())
    f.locator('#destroy_src').uncheck(); f.locator('#no_start').check()
    check('manuale: "Non avviare" blocca l\'eliminazione', f.locator('#destroy_src').is_disabled())
    # validazione anche se qualcuno forza i valori (difesa in profondita' della pagina)
    check('validate(): destroy_src + no_start -> errore', 'togli' in page.frame('f').evaluate("() => window.__xm.validate(window.__xm.buildPayload({src:'a',dst:'b',vmid:'1',destroy_src:true,no_start:true}))"))
    check('buildPayload: destroy_src sempre booleano, nessuna chiave mark', page.frame('f').evaluate("() => { var p = window.__xm.buildPayload({src:'a',dst:'b',vmid:'1'}); return p.destroy_src === false && !('mark_src' in p); }"))

    real = [e for e in errors if 'favicon' not in e and 'Failed to load resource' not in e]
    check('nessun errore di pagina, script o CSP in console', not real, str(real[:3]))
    b.close()

print('\n%d prove, %d fallite' % (len(res), res.count(False))); sys.exit(1 if False in res else 0)

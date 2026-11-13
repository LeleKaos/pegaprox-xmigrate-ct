import json, logging, os, sys, tempfile, time
logging.disable(logging.CRITICAL)
from playwright.sync_api import sync_playwright, expect
import harness

NEW = harness.PLUGIN_DIR
work = tempfile.mkdtemp(prefix='xmd_')
srv, mod, LOG, plug = harness.build(NEW, work, harness.STUB)
from pegaprox.utils import rbac
base = f'http://127.0.0.1:{srv.server_port}'
res = []
def check(name, cond, extra=''):
    res.append(bool(cond)); print(('OK  ' if cond else 'KO  ') + name + (f'   [{extra}]' if extra and not cond else ''))
def opts(f, sel): return f.locator(sel + ' option').all_inner_texts()
def enabled(f, sel): return [o.get_attribute('disabled') is None for o in f.locator(sel + ' option').all()]

with sync_playwright() as p:
    b = harness.launch(p)
    ctx = b.new_context(viewport={'width': 1300, 'height': 1000})
    ctx.add_cookies([{'name': 'session_id', 'value': 'good', 'url': base}])
    page = ctx.new_page()
    errors, dialogs = [], []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
    def open_page():
        page.goto(base + '/host?theme=modern-dark')
        f = page.frame_locator('#f'); f.locator('h1').wait_for()
        expect(f.locator('#src_cluster option')).not_to_have_count(1)   # attende i cluster
        return f

    # A) cluster iniziali
    f = open_page()
    check('cluster di origine: solo Proxmox (XCP-ng escluso), con il nome mostrato in PegaProx',
          opts(f, '#src_cluster')[1:] == ['PVE00', 'Sede', 'PVE02'], str(opts(f, '#src_cluster')))
    check('stessi cluster per la destinazione all\'inizio', opts(f, '#dst_cluster')[1:] == ['PVE00', 'Sede', 'PVE02'])
    check('modalità tendine di default, campi manuali nascosti', f.locator('#srcDrop').is_visible() and not f.locator('#src').is_visible())

    # J) messaggi di scelta mancante
    f.locator('#go').click()
    check('nessuna scelta -> "Scegli il cluster di origine."', 'cluster di origine' in f.locator('#formMsg').inner_text())

    # B) origine: cluster PVE02 con un nodo offline
    f.locator('#src_cluster').select_option('c3')
    expect(f.locator('#src_ip')).to_contain_text('192.0.2.5')
    check('nodi: pve02 online e pve02b offline (disattivato)', opts(f, '#src_node')[1:] == ['pve02', 'pve02b (offline)'] and enabled(f, '#src_node')[1:] == [True, False], str(opts(f, '#src_node')))
    check('un solo nodo online -> scelto da solo', f.locator('#src_node').input_value() == 'pve02')
    check('IP del nodo mostrato', f.locator('#src_ip').inner_text() == 'SSH verso 192.0.2.5')
    expect(f.locator('#vmid_sel option')).to_have_count(3)
    check('CT: solo gli LXC del nodo scelto (niente VM qemu, niente CT di altri nodi)', [o.split(' · ')[0] for o in opts(f, '#vmid_sel')[1:]] == ['300', '705'], str(opts(f, '#vmid_sel')))
    check('il CT non viene scelto da solo', f.locator('#vmid_sel').input_value() == '')
    f.locator('#go').click()
    check('manca il CT -> "Scegli il container da migrare." (dopo aver scelto solo l\'origine, la destinazione manca prima)',
          'cluster di destinazione' in f.locator('#formMsg').inner_text())
    check('destinazione: il cluster di origine non e\' tra le scelte', opts(f, '#dst_cluster')[1:] == ['PVE00', 'Sede'], str(opts(f, '#dst_cluster')))

    # C) CT acceso
    f.locator('#vmid_sel').select_option('705')
    check('CT acceso -> avviso che verra\' spento', 'verrà spento' in f.locator('#vm_hint').inner_text())

    # D) destinazione con VMID gia' in uso
    f.locator('#dst_cluster').select_option('c1')
    expect(f.locator('#dst_ip')).to_contain_text('192.0.2.3')
    check('destinazione: nodo unico scelto da solo e IP mostrato', f.locator('#dst_node').input_value() == 'pve00')
    expect(f.locator('#dst_hint')).to_contain_text('già in uso')
    check('VMID 705 gia\' usato nel cluster di destinazione -> avviso', '705' in f.locator('#dst_hint').inner_text())
    f.locator('#target_vmid').fill('9705')
    check('con un altro VMID l\'avviso sparisce', f.locator('#dst_hint').inner_text() == '')

    # F) simulazione con i valori delle tendine
    f.locator('#go').click()
    f.locator('#meta .badge.done').wait_for(timeout=10000)
    body = json.loads([l for l in LOG if l[1] == 'run'][-1][2])
    check('payload: IP dei nodi scelti, VMID come stringa', body['src'] == '192.0.2.5' and body['dst'] == '192.0.2.3' and body['vmid'] == '705' and body['target_vmid'] == '9705', str(body))
    job = list(mod.JOBS.values())[-1]
    check('argomenti dello script: -s IP -d IP -c 705 -t 9705', job['args'][:8] == ['-s', '192.0.2.5', '-d', '192.0.2.3', '-c', '705', '-i', job['args'][7]] and job['args'][job['args'].index('-t') + 1] == '9705', str(job['args']))
    check('il lavoro e\' attribuito all\'utente (alice) e la pagina lo mostra', job['user'] == 'alice' and 'alice' in f.locator('#jobs').inner_text())

    # G) conferma con i nomi
    f.locator('#dry_run').uncheck(); f.locator('#go').click(); time.sleep(0.6)
    d = dialogs[-1] if dialogs else ''
    check('conferma con nomi leggibili di CT, origine e destinazione', '705 (web1.example.com)' in d and 'PVE02 / pve02 (192.0.2.5)' in d and 'PVE00 / pve00 (192.0.2.3)' in d, d)

    # H) cambiare l'origine azzera quello che dipende da essa
    f.locator('#dry_run').check()
    f.locator('#src_cluster').select_option('c1')
    expect(f.locator('#src_ip')).to_contain_text('192.0.2.3')
    check('origine = cluster scelto come destinazione -> la destinazione si azzera', f.locator('#dst_cluster').input_value() == '' and f.locator('#dst_node').input_value() == '' and opts(f, '#dst_cluster')[1:] == ['Sede', 'PVE02'], str(opts(f, '#dst_cluster')))
    check('elenco CT ricaricato per il nuovo nodo (solo LXC: nessuno su pve00)', 'Nessun container LXC' in opts(f, '#vmid_sel')[0], str(opts(f, '#vmid_sel')))
    f.locator('#go').click()
    check('senza destinazione -> messaggio sulla destinazione', 'cluster di destinazione' in f.locator('#formMsg').inner_text())

    # I) nodo senza IP noto a PegaProx
    srv.DATA['nodes']['c3'][1]['status'] = 'online'
    f = open_page()
    f.locator('#src_cluster').select_option('c3')
    expect(f.locator('#src_node option')).to_have_count(3)
    check('due nodi online -> nessuna scelta automatica', f.locator('#src_node').input_value() == '')
    f.locator('#src_node').select_option('pve02b')
    expect(f.locator('#src_ip')).to_contain_text('attenzione')
    check('IP ricavato dall\'host del cluster -> avviso esplicito', 'host del cluster' in f.locator('#src_ip').inner_text())
    expect(f.locator('#vmid_sel option')).to_have_count(2)
    check('CT del nodo pve02b (solo il 800)', opts(f, '#vmid_sel')[1].startswith('800 · '))
    srv.DATA['nodes']['c3'][1]['status'] = 'offline'   # ripristina i dati di prova

    # O) cambi rapidi: vince l'ultima scelta
    f = open_page()
    f.locator('#src_cluster').select_option('c1'); f.locator('#src_cluster').select_option('c3')
    time.sleep(1.2)
    check('cambio rapido di cluster: restano i dati dell\'ultimo', f.locator('#src_ip').inner_text().endswith('192.0.2.5') or f.locator('#src_node').input_value() in ('pve02', ''), f.locator('#src_ip').inner_text())

    # K) elenco cluster non disponibile -> manuale
    srv.FAIL.add('/api/clusters')
    f = open_page.__call__() if False else None
    page.goto(base + '/host'); f = page.frame_locator('#f'); f.locator('h1').wait_for()
    expect(f.locator('#banner')).to_contain_text('Elenco dei cluster non disponibile')
    check('API dei cluster in errore -> modalità manuale forzata', f.locator('#src').is_visible() and f.locator('#dst').is_visible() and not f.locator('#srcToggle').is_visible())
    srv.FAIL.discard('/api/clusters')

    # L) nodi in errore
    srv.FAIL.add('nodes')
    f = open_page(); f.locator('#src_cluster').select_option('c3')
    expect(f.locator('#src_ip')).to_contain_text('Errore')
    check('API dei nodi in errore -> messaggio e nessun nodo', opts(f, '#src_node') == ['Nodi non disponibili'])
    srv.FAIL.discard('nodes')

    # M) elenco guest in errore
    srv.FAIL.add('vms')
    f = open_page(); f.locator('#src_cluster').select_option('c3')
    expect(f.locator('#vm_hint')).to_contain_text('non disponibile')
    check('API dei guest in errore -> messaggio con via d\'uscita manuale', opts(f, '#vmid_sel') == ['Elenco non disponibile'] and 'a mano' in f.locator('#vm_hint').inner_text())
    srv.FAIL.discard('vms')

    # modalità manuale dopo le tendine
    f = open_page(); f.locator('#srcToggle').click(); f.locator('#dstToggle').click()
    f.locator('#src').fill('10.1.1.1'); f.locator('#dst').fill('10.1.1.2'); f.locator('#vmid').fill('555'); f.locator('#go').click()
    f.locator('#meta .badge.done').wait_for(timeout=10000)
    body = json.loads([l for l in LOG if l[1] == 'run'][-1][2])
    check('modalità manuale: si usano i valori scritti', body['src'] == '10.1.1.1' and body['dst'] == '10.1.1.2' and body['vmid'] == '555')

    # N) senza permesso plugins.manage
    rbac.PERMS = {'plugins.view'}
    ctx.clear_cookies(); ctx.add_cookies([{'name': 'session_id', 'value': 'viewer', 'url': base}])
    f = open_page()
    expect(f.locator('#banner')).to_contain_text('plugins.manage')
    check('senza plugins.manage: avviso chiaro e bottone disattivato', f.locator('#go').is_disabled() and 'amministratore' in f.locator('#banner').inner_text())
    rbac.PERMS = {'plugins.view', 'plugins.manage'}

    real = [e for e in errors if 'Failed to load resource' not in e]
    check('nessun errore di pagina, script o CSP in console', not real, str(real[:3]))
    b.close()
print('\n%d prove, %d fallite' % (len(res), res.count(False))); sys.exit(1 if False in res else 0)

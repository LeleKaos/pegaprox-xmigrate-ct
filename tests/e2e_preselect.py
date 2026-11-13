import json, logging, os, sys, tempfile, time
logging.disable(logging.CRITICAL)
from playwright.sync_api import sync_playwright, expect
import harness

NEW = harness.PLUGIN_DIR
work = tempfile.mkdtemp(prefix='xmp_')
srv, mod, LOG, plug = harness.build(NEW, work, harness.STUB)
base = f'http://127.0.0.1:{srv.server_port}'
res = []
def check(name, cond, extra=''):
    res.append(bool(cond)); print(('OK  ' if cond else 'KO  ') + name + (f'   [{extra}]' if extra and not cond else ''))
def opts(f, sel): return f.locator(sel + ' option').all_inner_texts()

with sync_playwright() as p:
    b = harness.launch(p)
    ctx = b.new_context(viewport={'width': 1300, 'height': 1000})
    ctx.add_cookies([{'name': 'session_id', 'value': 'good', 'url': base}])
    page = ctx.new_page()
    errors, dialogs = [], []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    page.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
    def open_page(qs):
        page.goto(base + '/host' + qs)
        f = page.frame_locator('#f'); f.locator('h1').wait_for()
        expect(f.locator('#src_cluster option')).not_to_have_count(1)   # cluster caricati
        time.sleep(0.5)
        return f

    # 1) tab aperta nel cluster PVE02 (c3)
    f = open_page('?theme=modern-dark&cluster=c3')
    check('origine: il cluster della tab e\' preselezionato', f.locator('#src_cluster').input_value() == 'c3' and 'PVE02' in f.locator('#src_cluster option:checked').inner_text())
    check('nodo scelto da solo (un solo nodo online) e IP mostrato', f.locator('#src_node').input_value() == 'pve02' and f.locator('#src_ip').inner_text() == 'SSH verso 192.0.2.5', f.locator('#src_ip').inner_text())
    check('elenco CT gia\' caricato (solo gli LXC di pve02)', [o.split(' · ')[0] for o in opts(f, '#vmid_sel')[1:]] == ['300', '705'], str(opts(f, '#vmid_sel')))
    check('destinazione NON preselezionata, e senza il cluster di origine', f.locator('#dst_cluster').input_value() == '' and opts(f, '#dst_cluster')[1:] == ['PVE00', 'Sede'], str(opts(f, '#dst_cluster')))
    check('nessun avviso/errore al caricamento', f.locator('#banner').inner_text().strip() == '' or not f.locator('#banner').is_visible())
    check('il CT non viene scelto da solo', f.locator('#vmid_sel').input_value() == '')

    # 2) si puo' cambiare l'origine come sempre
    f.locator('#src_cluster').select_option('c1')
    expect(f.locator('#src_ip')).to_contain_text('192.0.2.3')
    check('origine cambiabile: nuovo cluster, nodo e IP', f.locator('#src_cluster').input_value() == 'c1' and f.locator('#src_node').input_value() == 'pve00')
    check('dopo il cambio la tendina destinazione esclude il nuovo origine', 'PVE00' not in opts(f, '#dst_cluster') and 'PVE02' in opts(f, '#dst_cluster'), str(opts(f, '#dst_cluster')))
    f.locator('#src_cluster').select_option('')
    check('si puo\' anche svuotare', f.locator('#src_node').input_value() == '' and 'prima il cluster' in opts(f, '#src_node')[0])

    # 3) flusso completo partendo dalla preselezione
    f = open_page('?theme=modern-dark&cluster=c3')
    f.locator('#vmid_sel').select_option('705')
    f.locator('#dst_cluster').select_option('c2'); expect(f.locator('#dst_ip')).to_contain_text('192.0.2.4')
    f.locator('#go').click(); f.locator('#meta .badge.done').wait_for(timeout=10000)
    body = json.loads([l for l in LOG if l[1] == 'run'][-1][2])
    check('simulazione dalla preselezione: origine = IP di pve02, dest = pve01, CT 705', body['src'] == '192.0.2.5' and body['dst'] == '192.0.2.4' and body['vmid'] == '705' and body['dry_run'] is True, str(body))

    # 4) cluster senza CT LXC
    f = open_page('?theme=modern-dark&cluster=c1')
    check('tab nel cluster PVE00: preselezionato, nodo unico, nessun CT LXC segnalato', f.locator('#src_cluster').input_value() == 'c1' and f.locator('#src_node').input_value() == 'pve00' and 'Nessun container LXC' in opts(f, '#vmid_sel')[0], str(opts(f, '#vmid_sel')))

    # 5) casi senza preselezione
    for name, qs in {'parametro cluster vuoto': '?cluster=', 'senza parametro cluster': '?theme=modern-dark',
                     'cluster sconosciuto': '?cluster=zzz', 'cluster XCP-ng (non migrabile con questo plugin)': '?cluster=c4',
                     'id con HTML': '?cluster=%3Cimg%20src%3Dx%20onerror%3Dwindow.__pwn%3D1%3E',
                     'id con maiuscole diverse (id e\' esatto)': '?cluster=C3'}.items():
        f = open_page(qs)
        check(f'nessuna preselezione: {name}', f.locator('#src_cluster').input_value() == '' and f.locator('#src_node').input_value() == '' and len(opts(f, '#dst_cluster')) == 4, f.locator('#src_cluster').input_value())
    check('l\'id con HTML non esegue nulla', page.evaluate("() => window.__pwn === undefined"))

    # 6) elenco cluster non disponibile + parametro: modalita' manuale, nessun errore
    srv.FAIL.add('/api/clusters')
    page.goto(base + '/host?cluster=c3'); f = page.frame_locator('#f'); f.locator('h1').wait_for()
    expect(f.locator('#banner')).to_contain_text('Elenco dei cluster non disponibile')
    check('API cluster in errore: manuale forzato come prima', f.locator('#src').is_visible() and not f.locator('#srcDrop').is_visible())
    srv.FAIL.discard('/api/clusters')

    # 7) nodo del cluster preselezionato con piu' nodi online: niente scelta automatica del nodo
    srv.DATA['nodes']['c3'][1]['status'] = 'online'
    f = open_page('?cluster=c3')
    check('cluster con due nodi online: cluster preselezionato, nodo da scegliere', f.locator('#src_cluster').input_value() == 'c3' and f.locator('#src_node').input_value() == '' and f.locator('#src_ip').inner_text() == '')
    srv.DATA['nodes']['c3'][1]['status'] = 'offline'

    # 8) cluster preselezionato non connesso: resta scelto, la pagina non si rompe
    srv.DATA['clusters'][2]['connected'] = False
    f = open_page('?cluster=c3')
    check('cluster non connesso: preselezionato con la dicitura "(non connesso)"', f.locator('#src_cluster').input_value() == 'c3' and 'non connesso' in f.locator('#src_cluster option:checked').inner_text())
    srv.DATA['clusters'][2]['connected'] = True

    real = [e for e in errors if 'favicon' not in e and 'Failed to load resource' not in e]
    check('nessun errore di pagina, script o CSP in console', not real, str(real[:3]))
    b.close()

print('\n%d prove, %d fallite' % (len(res), res.count(False))); sys.exit(1 if False in res else 0)

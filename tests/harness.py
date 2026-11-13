"""Mini PegaProx finto: stessa catch-all dei plugin, stessa CSP, stesso controllo CSRF in scrittura.
Carica il plugin vero (__init__.py + ui.html + manifest) da una cartella data."""
import importlib.util, json, os, shutil, stat, sys, tempfile, threading
from flask import Flask, jsonify, request, Response
from werkzeug.serving import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
# build() cambia HOME (per isolare la chiave SSH del plugin): senza questa riga Playwright
# cercherebbe i browser nella nuova HOME invece che in quella vera
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.join(os.path.expanduser('~'), '.cache', 'ms-playwright'))
PLUGIN_DIR = os.environ.get('XM_NEW') or os.path.join(HERE, '..', 'plugin', 'xmigrate_ct')
STUB = os.path.join(HERE, 'stub.sh')


def launch(p):
    """Chromium di Playwright; con XM_CHROME=/percorso/chrome si usa un browser gia' installato."""
    kw = dict(headless=True, args=['--no-sandbox'])
    if os.environ.get('XM_CHROME'):
        kw['executable_path'] = os.environ['XM_CHROME']
    return p.chromium.launch(**kw)


def build(plugin_src_dir, workdir, script_path):
    home = os.path.join(workdir, 'home'); os.makedirs(home, exist_ok=True)
    os.environ['HOME'] = home
    fake_bin = os.path.join(workdir, 'bin'); os.makedirs(fake_bin, exist_ok=True)
    kg = os.path.join(fake_bin, 'ssh-keygen')
    open(kg, 'w').write('#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do [ "$1" = -f ] && f=$2; shift; done\n'
                        'echo FAKEPRIVATE > "$f"; echo "ssh-ed25519 AAAAFAKEKEY xmigrate@test" > "$f.pub"\n')
    os.chmod(kg, 0o755)
    os.environ['PATH'] = fake_bin + os.pathsep + os.environ['PATH']

    stubs = os.path.join(workdir, 'stubs')
    for d in ('pegaprox', 'pegaprox/api', 'pegaprox/utils'):
        os.makedirs(os.path.join(stubs, d), exist_ok=True)
        open(os.path.join(stubs, d, '__init__.py'), 'w').write('')
    open(os.path.join(stubs, 'pegaprox/api/plugins.py'), 'w').write(
        'ROUTES = {}\n'
        'def register_plugin_route(plugin_id, path, handler):\n'
        '    ROUTES.setdefault(plugin_id, {})[path] = handler\n')
    open(os.path.join(stubs, 'pegaprox/utils/auth.py'), 'w').write(
        'def build_authz_user(username, session):\n'
        '    return {"username": username, "role": session.get("role")}\n')
    open(os.path.join(stubs, 'pegaprox/utils/rbac.py'), 'w').write(
        'PERMS = {"plugins.view", "plugins.manage"}\n'
        'RAISE = False\n'
        'def has_permission(user, perm):\n'
        '    if RAISE:\n        raise RuntimeError("boom")\n'
        '    return perm in PERMS\n')
    open(os.path.join(stubs, 'pegaprox/utils/audit.py'), 'w').write(
        'CALLS = []\n'
        'def log_audit(*a, **k):\n    CALLS.append(a)\n')
    sys.path.insert(0, stubs)

    plug = os.path.join(workdir, 'plugins', 'xmigrate_ct'); os.makedirs(plug)
    for f in os.listdir(plugin_src_dir):
        if os.path.isfile(os.path.join(plugin_src_dir, f)):
            shutil.copy(os.path.join(plugin_src_dir, f), plug)
    spec = importlib.util.spec_from_file_location('plugin_xmigrate_ct', os.path.join(plug, '__init__.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.register(None)
    if script_path:
        mod.SCRIPT_PATH = script_path
    from pegaprox.api import plugins as P
    app = Flask(__name__)
    CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
           "font-src 'self' data: https://fonts.gstatic.com https://fonts.googleapis.com; img-src 'self' data: blob:; "
           "connect-src 'self' wss: ws: https://cdn.jsdelivr.net; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")
    LOG = []
    SESSIONS = {'good': {'user': 'alice', 'role': 'admin', 'effective_role': 'admin'},
                'viewer': {'user': 'bob', 'role': 'viewer', 'effective_role': 'viewer'}}
    FAIL = set()
    DATA = {
        'clusters': [
            {'id': 'c1', 'name': 'PVE00', 'display_name': '', 'cluster_type': 'proxmox', 'connected': True, 'host': '192.0.2.3'},
            {'id': 'c2', 'name': 'PVE01', 'display_name': 'Sede', 'cluster_type': 'proxmox', 'connected': True, 'host': '192.0.2.4'},
            {'id': 'c3', 'name': 'PVE02', 'display_name': '', 'cluster_type': 'proxmox', 'connected': True, 'host': '192.0.2.5'},
            {'id': 'c4', 'name': 'XCP', 'display_name': '', 'cluster_type': 'xcpng', 'connected': True, 'host': '192.0.2.9'},
        ],
        'nodes': {'c1': [{'node': 'pve00', 'status': 'online'}], 'c2': [{'node': 'pve01', 'status': 'online'}],
                  'c3': [{'node': 'pve02', 'status': 'online'}, {'node': 'pve02b', 'status': 'offline'}]},
        'ips': {('c1', 'pve00'): ('192.0.2.3', 'manager_get_node_ip'), ('c2', 'pve01'): ('192.0.2.4', 'manager_get_node_ip'),
                ('c3', 'pve02'): ('192.0.2.5', 'manager_get_node_ip'), ('c3', 'pve02b'): ('192.0.2.5', 'cluster_host_fallback')},
        'vms': {
            'c1': [{'vmid': 705, 'name': 'gia-usato', 'node': 'pve00', 'type': 'qemu', 'status': 'stopped'}],
            'c2': [],
            'c3': [{'vmid': 100, 'name': 'una-vm', 'node': 'pve02', 'type': 'qemu', 'status': 'running'},
                   {'vmid': 300, 'name': 'test-ct', 'node': 'pve02', 'type': 'lxc', 'status': 'stopped'},
                   {'vmid': 705, 'name': 'web1.example.com', 'node': 'pve02', 'type': 'lxc', 'status': 'running'},
                   {'vmid': 800, 'name': 'altro-nodo', 'node': 'pve02b', 'type': 'lxc', 'status': 'running'}],
        },
    }

    @app.after_request
    def hdr(r):
        r.headers['X-Frame-Options'] = 'SAMEORIGIN'; r.headers['Content-Security-Policy'] = CSP
        r.headers['X-Content-Type-Options'] = 'nosniff'
        return r

    @app.before_request
    def csrf_and_auth():
        # come app.py: scrittura solo con X-Requested-With o Origin dello stesso host
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.path.startswith('/api/'):
            ok = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
                 request.headers.get('Origin', '').endswith(request.host)
            if not ok:
                return jsonify({'error': 'CSRF validation failed'}), 403
        # come require_auth: cookie di sessione obbligatorio sulle route dei plugin
        if request.path.startswith('/api/'):
            sess = SESSIONS.get(request.cookies.get('session_id'))
            if not sess:
                return jsonify({'error': 'Unauthorized'}), 401
            request.session = sess

    @app.route('/api/plugins/<plugin_id>/api/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
    def plugin_proxy(plugin_id, subpath):
        LOG.append((request.method, subpath, request.get_data(as_text=True)))
        handler = P.ROUTES.get(plugin_id, {}).get(subpath)
        if not handler:
            return jsonify({'error': f'Route not found: {subpath}'}), 404
        result = handler()
        if isinstance(result, (dict, list)):
            return jsonify(result)
        return result

    @app.route('/api/clusters')
    def r_clusters():
        if '/api/clusters' in FAIL: return jsonify({'error': 'boom'}), 500
        return jsonify(DATA['clusters'])

    @app.route('/api/clusters/<cid>/nodes')
    def r_nodes(cid):
        if 'nodes' in FAIL: return jsonify({'error': 'Connection temporarily unavailable', 'nodes': [], 'offline': True}), 503
        return jsonify(DATA['nodes'].get(cid, []))

    @app.route('/api/clusters/<cid>/vms')
    def r_vms(cid):
        if 'vms' in FAIL: return jsonify({'error': 'boom'}), 500
        return jsonify({'vms': DATA['vms'].get(cid, [])})

    @app.route('/api/clusters/<cid>/nodes/<node>/ip')
    def r_ip(cid, node):
        ip = DATA['ips'].get((cid, node))
        if not ip: return jsonify({'error': 'not found'}), 404
        return jsonify({'ip': ip[0], 'node': node, 'source': ip[1]})

    @app.route('/host')
    def host():
        qs = request.query_string.decode()
        return Response('<!doctype html><title>host</title><body style="margin:0">'
                        '<iframe id="f" style="width:100%;height:95vh;border:0" '
                        'sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads" '
                        f'src="/api/plugins/xmigrate_ct/api/ui?{qs}"></iframe>', mimetype='text/html')

    @app.route('/__set_script')
    def set_script():
        mod.SCRIPT_PATH = request.args['path']; return 'ok'

    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.SESSIONS, srv.FAIL, srv.DATA = SESSIONS, FAIL, DATA
    return srv, mod, LOG, plug

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
XMigrate CT plugin for PegaProx.

Wraps xmigrate-ct.sh (cross-cluster LXC migration as root) so it can be
launched from the PegaProx UI/API instead of a shell. The script runs on the
PegaProx server itself and uses SSH (root) to reach both clusters, therefore
the PegaProx host must have:
  - bash, ssh, jq (auto-installed by the script if missing)
  - a private key with root access to both nodes (--identity)
  - network reachability to both nodes on SSH
"""
import logging
import os
import re
import subprocess
import threading
import time
import uuid

from flask import request, Response

try:
    from pegaprox.api.plugins import register_plugin_route
except Exception:  # pragma: no cover - only when imported outside PegaProx
    def register_plugin_route(plugin_id, path, handler):
        return None

try:
    from pegaprox.utils.audit import log_audit
except Exception:  # pragma: no cover
    def log_audit(*a, **k):
        return None

PLUGIN_ID = "xmigrate_ct"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(PLUGIN_DIR, "xmigrate-ct.sh")
UI_PATH = os.path.join(PLUGIN_DIR, "ui.html")

# Chiave SSH gestita dal plugin (generata al primo uso se manca)
SSH_KEY_DIR = os.path.join(os.path.expanduser("~"), ".ssh")
SSH_KEY_PATH = os.path.join(SSH_KEY_DIR, "xmigrate_ct_ed25519")

JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_LOG_LINES = 5000

# ---- validazione dei parametri -------------------------------------------------
# I valori finiscono come argomenti dello script e poi di ssh: un nodo che inizia con "-"
# (es. "-oProxyCommand=...") verrebbe letto da ssh come opzione ed eseguirebbe comandi
# sul server PegaProx. Per questo si accetta solo un insieme stretto di caratteri, mai
# "-" iniziale, mai spazi.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,63}@)?[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
_STORAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")   # id di storage Proxmox
_VMID_RE = re.compile(r"^[0-9]{1,9}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]{1,255}$")
_MODES = ("auto", "pbs", "local")


def _field(body, key):
    """Valore testuale di un campo del JSON: accetta stringhe e numeri, niente altro."""
    v = body.get(key)
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise ValueError(f"{key}: valore non valido")
    return str(v).strip()


def _actor():
    """Utente PegaProx che sta facendo la richiesta (per audit e per i lavori)."""
    return (getattr(request, "session", None) or {}).get("user") or "unknown"


def _deny_unless_manager():
    """None se l'utente puo' gestire i plugin (permesso plugins.manage), altrimenti la
    risposta 403. Le route dei plugin richiedono di per se' solo plugins.view, ma qui si
    lancia root via SSH sui nodi e si riceve una password di root."""
    try:
        from pegaprox.utils.auth import build_authz_user
        from pegaprox.utils.rbac import has_permission
        sess = getattr(request, "session", None) or {}
        allowed = bool(has_permission(build_authz_user(sess.get("user", ""), sess), "plugins.manage"))
    except Exception:  # noqa: BLE001 - versione di PegaProx diversa: si ripiega sul ruolo admin
        logging.exception("[xmigrate_ct] controllo permessi non disponibile, uso il ruolo")
        sess = getattr(request, "session", None) or {}
        allowed = (sess.get("effective_role") or sess.get("role")) == "admin"
    if allowed:
        return None
    return {"ok": False, "error": "Permesso mancante: plugins.manage",
            "code": "MISSING_PERMISSION", "required": "plugins.manage"}, 403


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _append(job, line):
    with JOBS_LOCK:
        job["log"].append(line)
        if len(job["log"]) > MAX_LOG_LINES:
            job["log"] = job["log"][-MAX_LOG_LINES:]


def _ensure_ssh_key():
    """Genera una chiave SSH per il plugin se non esiste. Ritorna (path, pubkey)."""
    if not os.path.isfile(SSH_KEY_PATH):
        os.makedirs(SSH_KEY_DIR, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", SSH_KEY_PATH],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        os.chmod(SSH_KEY_PATH, 0o600)
    except Exception:
        pass
    pub_path = SSH_KEY_PATH + ".pub"
    pubkey = ""
    if os.path.isfile(pub_path):
        with open(pub_path) as f:
            pubkey = f.read().strip()
    return SSH_KEY_PATH, pubkey


def _handle_ssh_key():
    denied = _deny_unless_manager()
    if denied:
        return denied
    try:
        path, pubkey = _ensure_ssh_key()
    except Exception as e:  # noqa: BLE001
        logging.exception("[xmigrate_ct] ssh-keygen failed")
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True, "path": path, "public_key": pubkey,
            "hint": "Copia la public_key in ~/.ssh/authorized_keys di root sui nodi origine/destinazione."}


def _install_pubkey(host, user, password, pubkey):
    """Tenta di installare la pubkey su host. Ritorna (ok, msg)."""
    # 1) paramiko se disponibile
    try:
        import paramiko  # type: ignore
    except ImportError:
        paramiko = None
    if paramiko is not None:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host, username=user, password=password, timeout=15, look_for_keys=False, allow_agent=False)
            safe = pubkey.replace("'", "'\\''")
            cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                   "grep -qF '{pk}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pk}' >> ~/.ssh/authorized_keys && "
                   "chmod 600 ~/.ssh/authorized_keys").format(pk=safe)
            stdin, stdout, stderr = client.exec_command(cmd)
            rc = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace").strip()
            client.close()
            if rc == 0:
                return True, "installata"
            return False, err or f"rc={rc}"
        except Exception as e:  # noqa: BLE001
            return False, f"paramiko: {e}"
    # 2) fallback sshpass
    import shutil
    if shutil.which("sshpass"):
        try:
            import tempfile
            fd, tmp = tempfile.mkstemp()
            with os.fdopen(fd, "w") as f:
                f.write(pubkey + "\n")
            # usa ssh per appendere (password via sshpass)
            safe = pubkey.replace("'", "'\\''")
            cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                   "grep -qF '{pk}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pk}' >> ~/.ssh/authorized_keys && "
                   "chmod 600 ~/.ssh/authorized_keys").format(pk=safe)
            r = subprocess.run(
                ["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null", f"{user}@{host}", cmd],
                capture_output=True, text=True, timeout=30,
            )
            os.remove(tmp)
            if r.returncode == 0:
                return True, "installata"
            return False, (r.stderr or r.stdout or f"rc={r.returncode}").strip()
        except Exception as e:  # noqa: BLE001
            return False, f"sshpass: {e}"
    return False, ("paramiko non installato e sshpass non disponibile. "
                   "Installa paramiko (pip install paramiko) sul server PegaProx, oppure copia la pubkey a mano.")


def _handle_install_ssh_key():
    denied = _deny_unless_manager()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        host = _field(body, "host")
        user = _field(body, "user") or "root"
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    password = body.get("password") or ""
    if not host:
        return {"ok": False, "error": "host obbligatorio"}, 400
    if "@" in host or not _HOST_RE.match(host):
        return {"ok": False, "error": "host non valido (solo lettere, cifre, . _ : -, mai '-' iniziale)"}, 400
    if not _USER_RE.match(user):
        return {"ok": False, "error": "utente non valido"}, 400
    if not isinstance(password, str) or not password:
        return {"ok": False, "error": "password obbligatoria per l'installazione automatica"}, 400
    try:
        _, pubkey = _ensure_ssh_key()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}, 500
    ok, msg = _install_pubkey(host, user, password, pubkey)
    try:
        log_audit(_actor(), "xmigrate_ct_install_key", f"host={host} user={user} ok={ok}")
    except Exception:
        pass
    if ok:
        return {"ok": True, "host": host, "message": msg}
    return {"ok": False, "host": host, "error": msg}, 500


def _flag(body, key):
    """Flag booleano rigoroso: solo true/false JSON. None se assente.
    (Per le opzioni che eliminano dati non basta la "verita'" di una stringa:
    "false" sarebbe vero.)"""
    if key not in body or body[key] is None:
        return None
    v = body[key]
    if v is True or v is False:
        return v
    raise ValueError(f"{key}: deve essere true o false")


def _build_args(body):
    args = []
    src = _field(body, "src")
    dst = _field(body, "dst")
    vmid = _field(body, "vmid")
    if not src or not dst or not vmid:
        raise ValueError("src, dst e vmid sono obbligatori")
    if not _HOST_RE.match(src):
        raise ValueError("src: nodo non valido (solo lettere, cifre, . _ : -, niente spazi, mai '-' iniziale)")
    if not _HOST_RE.match(dst):
        raise ValueError("dst: nodo non valido (solo lettere, cifre, . _ : -, niente spazi, mai '-' iniziale)")
    if not _VMID_RE.match(vmid):
        raise ValueError("vmid: deve essere un numero")
    args += ["-s", src, "-d", dst, "-c", vmid]

    identity = _field(body, "identity")
    if identity:
        if not _PATH_RE.match(identity) or ".." in identity.split("/") or not os.path.isfile(identity):
            raise ValueError("identity: deve essere il percorso assoluto di un file esistente")
    else:
        # auto: usa la chiave gestita dal plugin
        try:
            identity, _ = _ensure_ssh_key()
        except Exception:
            identity = ""
    if identity:
        args += ["-i", identity]

    target = _field(body, "target_vmid")
    if target:
        if not _VMID_RE.match(target):
            raise ValueError("target_vmid: deve essere un numero")
        args += ["-t", target]
    mode = _field(body, "mode") or "auto"
    if mode not in _MODES:
        raise ValueError("mode: deve essere auto, pbs o local")
    if mode != "auto":
        args += ["--mode", mode]
    staging = _field(body, "staging")
    if staging:
        if not _STORAGE_RE.match(staging):
            raise ValueError("staging: id di storage non valido")
        args += ["--staging", staging]
    dst_storage = _field(body, "dst_storage")
    if dst_storage:
        if not _STORAGE_RE.match(dst_storage):
            raise ValueError("dst_storage: id di storage non valido")
        args += ["--dst-storage", dst_storage]
    if body.get("dry_run"):
        args += ["-n"]
    if body.get("no_start"):
        args += ["--no-start"]
    if body.get("cleanup_src"):
        args += ["--cleanup-src"]
    # origine: eliminata solo se richiesto esplicitamente e solo se la destinazione
    # risulta avviata (lo verifica lo script); altrimenti lo script la marca da solo
    # come "migrata" (lock: migrate), senza nessuna opzione
    destroy_src = _flag(body, "destroy_src")
    if destroy_src:
        if body.get("no_start"):
            raise ValueError("destroy_src non e' compatibile con no_start: l'origine si elimina solo se il CT e' stato avviato sulla destinazione")
        args += ["--destroy-src"]
    # non-interactive: accept confirmations automatically
    args += ["-y"]
    return args


def _run_job(job_id, args):
    job = JOBS[job_id]
    _append(job, f"[{_now()}] avvio: bash {SCRIPT_PATH} {' '.join(args)}")
    try:
        proc = subprocess.Popen(
            ["bash", SCRIPT_PATH] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PLUGIN_DIR,
        )
        job["pid"] = proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            _append(job, line.rstrip("\n"))
        proc.wait()
        job["rc"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "failed"
        _append(job, f"[{_now()}] terminato con codice {proc.returncode}")
    except Exception as e:  # noqa: BLE001
        job["rc"] = -1
        job["status"] = "failed"
        _append(job, f"[{_now()}] errore: {e}")
    finally:
        job["finished_at"] = _now()


def _start_job(body, actor="unknown"):
    args = _build_args(body)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "running",
        "created_at": _now(),
        "finished_at": None,
        "rc": None,
        "pid": None,
        "user": actor,
        "args": args,
        "log": [],
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    t = threading.Thread(target=_run_job, args=(job_id, args), daemon=True)
    t.start()
    job["thread"] = t
    try:
        log_audit(actor, "xmigrate_ct_start", f"job={job_id} args={' '.join(args)}")
    except Exception:
        pass
    return job_id


def _handle_run():
    denied = _deny_unless_manager()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        job_id = _start_job(body, _actor())
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:  # noqa: BLE001
        logging.exception("[xmigrate_ct] start failed")
        return {"ok": False, "error": str(e)}, 500
    return {"ok": True, "job_id": job_id}


def _handle_jobs():
    denied = _deny_unless_manager()
    if denied:
        return denied
    with JOBS_LOCK:
        items = [
            {
                "id": j["id"],
                "status": j["status"],
                "created_at": j["created_at"],
                "finished_at": j["finished_at"],
                "rc": j["rc"],
                "user": j.get("user", ""),
            }
            for j in JOBS.values()
        ]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"jobs": items}


def _handle_job(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return {"ok": False, "error": "job non trovato"}, 404
        snap = {
            "id": j["id"],
            "status": j["status"],
            "created_at": j["created_at"],
            "finished_at": j["finished_at"],
            "rc": j["rc"],
            "user": j.get("user", ""),
            "log": list(j["log"]),
        }
    return snap


def _handle_job_route():
    denied = _deny_unless_manager()
    if denied:
        return denied
    jid = request.args.get("id")
    if not jid:
        return {"ok": False, "error": "parametro 'id' mancante"}, 400
    return _handle_job(jid)


def _handle_ui():
    """Pagina della GUI: PegaProx la mostra come tab del cluster (manifest.json,
    has_frontend + frontend_route = "ui"). Usa le stesse API del bookmarklet."""
    try:
        with open(UI_PATH, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        logging.error("[xmigrate_ct] ui.html non leggibile: %s", e)
        return {"ok": False, "error": "ui.html non trovato nella cartella del plugin"}, 404
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-cache"})


def register(app):
    if not os.path.isfile(SCRIPT_PATH):
        logging.error("[xmigrate_ct] script non trovato: %s", SCRIPT_PATH)
    else:
        try:
            os.chmod(SCRIPT_PATH, 0o755)
        except Exception:
            pass
    register_plugin_route(PLUGIN_ID, "run", _handle_run)
    register_plugin_route(PLUGIN_ID, "jobs", _handle_jobs)
    register_plugin_route(PLUGIN_ID, "job", _handle_job_route)
    register_plugin_route(PLUGIN_ID, "ssh-key", _handle_ssh_key)
    register_plugin_route(PLUGIN_ID, "install-ssh-key", _handle_install_ssh_key)
    register_plugin_route(PLUGIN_ID, "ui", _handle_ui)
    logging.info("[xmigrate_ct] xmigrate_ct registered (script=%s)", SCRIPT_PATH)


# Routes (via PegaProx catch-all proxy):
#   POST /api/plugins/xmigrate_ct/api/run          body JSON -> {ok, job_id}
#   GET  /api/plugins/xmigrate_ct/api/jobs         -> {jobs:[...]}
#   GET  /api/plugins/xmigrate_ct/api/job?id=...   -> {id,status,...,log:[...]}
#   GET  /api/plugins/xmigrate_ct/api/ui           -> pagina HTML della GUI (tab in PegaProx)

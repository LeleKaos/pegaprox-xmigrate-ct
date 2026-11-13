#!/usr/bin/env python3
"""Test di xmigrate-ct.sh con nodi Proxmox simulati (mockssh.py)."""
import os, sys, json, shutil, subprocess, tempfile, re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("XM_SCRIPT", HERE + "/../plugin/xmigrate_ct/xmigrate-ct.sh")
CONF = """arch: amd64
cores: 2
hostname: web1
memory: 512
net0: name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:01,ip=dhcp
ostype: debian
rootfs: local-lvm:vm-101-disk-0,size=8G
"""
FAILS = []
COUNT = 0

def check(name, cond, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")
    else:
        print(f"  ok    {name}")

class Env:
    def __init__(self, running=True, src_conf=CONF, knobs_src=None, knobs_dst=None):
        self.d = tempfile.mkdtemp(prefix="xm_")
        for n in ("src", "dst"):
            os.makedirs(f"{self.d}/{n}/ct")
        open(f"{self.d}/src/ct/101.conf", "w").write(src_conf)
        open(f"{self.d}/src/ct/101.status", "w").write("running" if running else "stopped")
        if knobs_src: json.dump(knobs_src, open(f"{self.d}/src/knobs.json", "w"))
        if knobs_dst: json.dump(knobs_dst, open(f"{self.d}/dst/knobs.json", "w"))
    def run(self, *args, tty_input=None, base=True):
        env = dict(os.environ, MOCK=self.d, PATH=f"{HERE}/bin:" + os.environ["PATH"], HOME=self.d)
        a = ["bash", SCRIPT]
        if base:
            a += ["-s", "src", "-d", "dst", "-c", "101", "-t", "101", "-i", "/tmp/fakekey"]
        a += list(args)
        if tty_input is None:
            p = subprocess.run(a, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            return p.returncode, p.stdout, p.stderr
        cmd = " ".join(__import__("shlex").quote(x) for x in a)
        p = subprocess.run(["script", "-qec", cmd, "/dev/null"], env=env, input=tty_input,
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr
    def conf(self, n, v="101"):
        p = f"{self.d}/{n}/ct/{v}.conf"
        return open(p).read() if os.path.exists(p) else None
    def status(self, n, v="101"):
        p = f"{self.d}/{n}/ct/{v}.status"
        return open(p).read().strip() if os.path.exists(p) else None
    def calls(self):
        p = f"{self.d}/calls.log"
        return open(p).read().splitlines() if os.path.exists(p) else []
    def has_call(self, pat):
        return any(re.search(pat, c) for c in self.calls())
    def archive_src(self):
        d = f"{self.d}/src/fs/var/lib/vz/dump"
        return os.path.isdir(d) and bool(os.listdir(d))

def scenario(title):
    print(f"\n== {title}")

# 1 ---------------------------------------------------------------------------
scenario("1. migrazione normale: origine marcata 'migrated'")
e = Env(); rc, out, err = e.run("-y")
check("exit 0", rc == 0, f"rc={rc}\n{out}\n{err}")
check("destinazione accesa", e.status("dst") == "running")
check("origine spenta", e.status("src") == "stopped")
check("origine con lock: migrate", e.conf("src") and e.conf("src").startswith("lock: migrate"))
check("CT non eliminato", e.conf("src") is not None)
check("istruzioni: pct unlock && pct destroy", "pct unlock 101 && pct destroy 101 --purge" in out, out[-400:])
check("nessun pct destroy eseguito", not e.has_call(r"pct destroy"))
check("archivio di sicurezza sull'origine", e.archive_src())
check("onboot=0 impostato", e.has_call(r"src: pct set 101 --onboot 0"))

# 2 ---------------------------------------------------------------------------
scenario("2. nessuna opzione per il lock: --no-mark-src non esiste piu'")
e = Env(); rc, out, err = e.run("-y", "--no-mark-src")
check("opzione sconosciuta, exit 1", rc == 1 and "opzione sconosciuta" in err, f"rc={rc} {err}")
check("nessuna chiamata ssh", e.calls() == [])

# 3 ---------------------------------------------------------------------------
scenario("3. --destroy-src, destinazione avviata: origine eliminata")
e = Env(); rc, out, err = e.run("-y", "--destroy-src")
check("exit 0", rc == 0, f"rc={rc}\n{out}\n{err}")
check("CT eliminato dall'origine", e.conf("src") is None)
check("destinazione ancora accesa", e.status("dst") == "running" and e.conf("dst") is not None)
check("messaggio 'eliminato dall'origine'", "eliminato dall'origine" in out)
check("nessun lock impostato (CT eliminato)", not e.has_call(r"--lock"))
calls = e.calls()
i_st = max(i for i, c in enumerate(calls) if re.search(r"dst: pct status 101", c))
i_de = next(i for i, c in enumerate(calls) if re.search(r"src: pct destroy 101 --purge", c))
check("verifica destinazione PRIMA del destroy", i_st < i_de)
check("archivio di sicurezza resta sull'origine", e.archive_src())

# 4 ---------------------------------------------------------------------------
scenario("4. --destroy-src, il CT non parte sulla destinazione (rollback, nessun destroy)")
e = Env(knobs_dst={"never_running": True}); rc, out, err = e.run("-y", "--destroy-src")
check("exit != 0", rc != 0, f"rc={rc}")
check("origine NON eliminata", e.conf("src") is not None)
check("origine riavviata dal rollback", e.status("src") == "running", e.status("src"))
check("nessun pct destroy sull'origine", not e.has_call(r"src: pct destroy"))
check("nessun lock sull'origine", e.conf("src") and "lock:" not in e.conf("src"))

# 5 ---------------------------------------------------------------------------
scenario("5. --destroy-src, il CT parte ma crolla subito: origine NON eliminata, exit 3")
e = Env(knobs_dst={"crash_after_start": True}); rc, out, err = e.run("-y", "--destroy-src")
check("exit 3", rc == 3, f"rc={rc}\n{out}\n{err}")
check("origine NON eliminata", e.conf("src") is not None)
check("nessun pct destroy", not e.has_call(r"pct destroy"))
check("avviso 'NON elimino'", "NON elimino" in err)
check("origine marcata migrated", e.conf("src") and e.conf("src").startswith("lock: migrate"))
check("origine resta spenta", e.status("src") == "stopped")

# 6 ---------------------------------------------------------------------------
scenario("6. --destroy-src + --no-start: rifiutato subito, nessuna modifica")
e = Env(); rc, out, err = e.run("-y", "--destroy-src", "--no-start")
check("exit 1", rc == 1, f"rc={rc}")
check("messaggio di incompatibilita'", "non e' compatibile con --no-start" in err, err)
check("nessuna chiamata ssh", e.calls() == [], str(e.calls()[:3]))
check("origine intatta", e.status("src") == "running" and "lock:" not in e.conf("src"))

# 7 ---------------------------------------------------------------------------
scenario("7. --destroy-src con CT spento in origine: rifiutato prima di toccare qualcosa")
e = Env(running=False); rc, out, err = e.run("-y", "--destroy-src")
check("exit 1", rc == 1, f"rc={rc}\n{err}")
check("messaggio chiaro", "spento sull'origine" in err and "non potrei verificarne l'avvio" in err, err)
check("nessun vzdump/restore/destroy", not e.has_call(r"^\w+: (vzdump \d|pct restore|pct destroy|pct set)"))
check("niente sulla destinazione", e.conf("dst") is None)

# 8 ---------------------------------------------------------------------------
scenario("8. --destroy-src + --dry-run (solo simulazione): non tocca nulla")
e = Env(); rc, out, err = e.run("-n", "--destroy-src")
check("exit 0", rc == 0, f"{out}\n{err}")
check("il riepilogo dice 'SOLO se'", "ELIMINATO" in out and "SOLO se" in out, out)
check("nessuna modifica", not e.has_call(r"^\w+: (vzdump \d|pct restore|pct destroy|pct set|pct start|pct shutdown)"))
check("origine intatta", e.conf("src") == CONF and e.status("src") == "running")

# 9 ---------------------------------------------------------------------------
scenario("9. pct destroy fallisce (protezione): exit 3, origine marcata e intatta")
e = Env(knobs_src={"destroy_fail": True}); rc, out, err = e.run("-y", "--destroy-src")
check("exit 3", rc == 3, f"rc={rc}\n{err}")
check("origine presente", e.conf("src") is not None)
check("origine marcata migrated", e.conf("src").startswith("lock: migrate"))
check("destinazione accesa", e.status("dst") == "running")
check("messaggio su come eliminare a mano", "pct unlock 101 && pct destroy 101 --purge" in out)

# 10 --------------------------------------------------------------------------
scenario("10. pct set --lock fallisce: solo un avviso, migrazione riuscita")
e = Env(knobs_src={"lock_fail": True}); rc, out, err = e.run("-y")
check("exit 0", rc == 0, f"rc={rc}\n{err}")
check("avviso lock", "impossibile impostare lock" in err)
check("senza unlock nelle istruzioni", "pct unlock" not in out and "pct destroy 101 --purge" in out)
check("destinazione accesa", e.status("dst") == "running")

# 11 --------------------------------------------------------------------------
scenario("11. --no-start (senza destroy): destinazione spenta, origine marcata")
e = Env(); rc, out, err = e.run("-y", "--no-start")
check("exit 0", rc == 0, f"{out}\n{err}")
check("destinazione spenta", e.status("dst") == "stopped")
check("origine marcata", e.conf("src").startswith("lock: migrate"))
check("nessun destroy", not e.has_call(r"pct destroy"))

# 12 --------------------------------------------------------------------------
scenario("12. CT spento in origine (senza destroy): migra, destinazione spenta")
e = Env(running=False); rc, out, err = e.run("-y")
check("exit 0", rc == 0, f"{out}\n{err}")
check("destinazione spenta", e.status("dst") == "stopped")
check("origine marcata", e.conf("src").startswith("lock: migrate"))

# 13 --------------------------------------------------------------------------
scenario("13. restore fallisce: rollback, niente destroy, niente lock")
e = Env(knobs_dst={"restore_fail": True}); rc, out, err = e.run("-y", "--destroy-src")
check("exit != 0", rc != 0)
check("origine riavviata", e.status("src") == "running")
check("origine intatta, senza lock", e.conf("src") == CONF)
check("nessun destroy", not e.has_call(r"pct destroy"))

# 14 --------------------------------------------------------------------------
scenario("14. origine gia' con lock: migrate -> rifiutata con messaggio chiaro")
e = Env(src_conf="lock: migrate\n" + CONF, running=False); rc, out, err = e.run("-y")
check("exit 1", rc == 1)
check("messaggio 'gia' migrato'", "gia' migrato" in err, err)
check("nessuna modifica", not e.has_call(r"^\w+: (vzdump \d|pct restore|pct destroy|pct set)"))

# 15 --------------------------------------------------------------------------
scenario("15. --cleanup-src non elimina il CT")
e = Env(); rc, out, err = e.run("-y", "--cleanup-src")
check("exit 0", rc == 0)
check("CT ancora sull'origine", e.conf("src") is not None)
check("archivio origine eliminato", not e.archive_src())

# 16 --------------------------------------------------------------------------
scenario("16. modalita' guidata (tty): chiede di eliminare, risposta = VMID")
e = Env(); rc, out, err = e.run(tty_input="y\n101\n")
check("exit 0", rc == 0, f"rc={rc}\n{out}")
check("compare la domanda di eliminazione", "Eliminare ORA il CT 101" in out, out[-600:])
check("CT eliminato", e.conf("src") is None)

scenario("17. modalita' guidata (tty): risposta diversa dal VMID -> resta, marcato")
e = Env(); rc, out, err = e.run(tty_input="y\nno\n")
check("exit 0", rc == 0, f"rc={rc}\n{out}")
check("CT presente e marcato", e.conf("src") is not None and e.conf("src").startswith("lock: migrate"))
check("nessun destroy", not e.has_call(r"pct destroy"))

scenario("18. modalita' guidata (tty) + --no-start: nessuna domanda di eliminazione")
e = Env(); rc, out, err = e.run("--no-start", tty_input="y\n")
check("exit 0", rc == 0, f"rc={rc}\n{out}")
check("nessuna domanda", "Eliminare ORA" not in out)

print(f"\n{COUNT - len(FAILS)}/{COUNT} controlli ok")
if FAILS:
    print("FALLITI:", *FAILS, sep="\n  - ")
    sys.exit(1)

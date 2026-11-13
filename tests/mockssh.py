#!/usr/bin/env python3
"""ssh finto: simula due nodi Proxmox (root@src, root@dst) con stato su file in $MOCK."""
import os, sys, json, re, shlex, hashlib

MOCK = os.environ["MOCK"]
args = sys.argv[1:]
host = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-i", "-p", "-l"):
        i += 2
        continue
    if a.startswith("-"):
        i += 1
        continue
    host = a
    i += 1
    break
cmd = " ".join(args[i:])
node = host.split("@")[-1]          # src | dst
NODE = os.path.join(MOCK, node)
os.makedirs(NODE + "/ct", exist_ok=True)
os.makedirs(NODE + "/fs", exist_ok=True)

def knobs():
    p = os.path.join(NODE, "knobs.json")
    return json.load(open(p)) if os.path.exists(p) else {}

def setk(k, v):
    d = knobs(); d[k] = v
    json.dump(d, open(os.path.join(NODE, "knobs.json"), "w"))

def log(line):
    open(os.path.join(MOCK, "calls.log"), "a").write(f"{node}: {line}\n")

def conf_path(v): return f"{NODE}/ct/{v}.conf"
def stat_path(v): return f"{NODE}/ct/{v}.status"
def exists(v): return os.path.exists(conf_path(v))
def status(v): return open(stat_path(v)).read().strip() if os.path.exists(stat_path(v)) else "stopped"
def setstatus(v, s): open(stat_path(v), "w").write(s)
def out(s): sys.stdout.write(s if s.endswith("\n") or not s else s + "\n")
def fs(p): return NODE + "/fs/" + p.lstrip("/")

log(cmd)
K = knobs()
nodename = "pve-" + node
cluster = {"src": "clA", "dst": "clB"}[node]

if cmd == "true":
    sys.exit(0)
if cmd.startswith("command -v"):
    sys.exit(0)
if cmd == "hostname -s":
    out(nodename); sys.exit(0)
if cmd.startswith("pvesh get /cluster/status"):
    out(json.dumps([{"type": "cluster", "name": cluster}, {"type": "node", "name": nodename}])); sys.exit(0)
m = re.match(r"pvesh get /nodes/(\S+)/lxc --output-format json", cmd)
if m:
    items = []
    for f in sorted(os.listdir(NODE + "/ct")):
        if f.endswith(".conf"):
            v = f[:-5]
            c = open(conf_path(v)).read()
            hn = re.search(r"^hostname: (.*)$", c, re.M).group(1)
            lk = re.search(r"^lock: (.*)$", c, re.M)
            e = {"vmid": int(v), "name": hn, "status": status(v)}
            if lk: e["lock"] = lk.group(1)
            items.append(e)
    out(json.dumps(items)); sys.exit(0)
m = re.match(r"pct config (\d+)$", cmd)
if m:
    v = m.group(1)
    if not exists(v):
        sys.stderr.write(f"Configuration file 'nodes/{nodename}/lxc/{v}.conf' does not exist\n"); sys.exit(2)
    sys.stdout.write(open(conf_path(v)).read()); sys.exit(0)
if cmd.startswith("pvesh get /cluster/ha/resources"):
    out(json.dumps(K.get("ha", []))); sys.exit(0)
m = re.match(r"pct status (\d+)$", cmd)
if m:
    v = m.group(1)
    if not exists(v): sys.stderr.write("no such CT\n"); sys.exit(2)
    s = status(v)
    # crash_after_start: il primo controllo dice running, poi il CT "muore"
    if s == "running" and K.get("crash_after_start"):
        n = K.get("status_calls_" + v, 0) + 1
        setk("status_calls_" + v, n)
        if n > 1:
            setstatus(v, "stopped"); s = "stopped"
    out("status: " + s); sys.exit(0)
if cmd.startswith("pvesh get /cluster/resources --type vm"):
    res = []
    for f in sorted(os.listdir(NODE + "/ct")):
        if f.endswith(".conf"): res.append({"vmid": int(f[:-5]), "type": "lxc"})
    out(json.dumps(res)); sys.exit(0)
m = re.match(r"pvesh get /nodes/(\S+)/storage --content (\S+) --enabled 1 --output-format json", cmd)
if m:
    if m.group(2) == "rootdir":
        out(json.dumps([{"storage": "local-lvm", "type": "lvmthin", "active": 1, "avail": 500 * 1024**3}]))
    else:
        out(json.dumps([{"storage": "local", "type": "dir", "active": 1, "avail": 500 * 1024**3}]))
    sys.exit(0)
if cmd.startswith("pvesh get /storage --type pbs"):
    out("[]"); sys.exit(0)
m = re.match(r"pvesh get /storage/(\S+) --output-format json", cmd)
if m:
    out(json.dumps({"path": "/var/lib/vz"})); sys.exit(0)
if cmd.startswith("test -d /sys/class/net/"):
    sys.exit(0)
m = re.match(r"pct shutdown (\d+)", cmd)
if m:
    setstatus(m.group(1), "stopped"); sys.exit(0)
m = re.match(r"pvesm list (\S+) --content backup --vmid (\d+)", cmd)
if m:
    print("Volid Format Type Size VMID")
    d = fs("/var/lib/vz/dump")
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.startswith(f"vzdump-lxc-{m.group(2)}-"):
                print(f"{m.group(1)}:backup/{f} tar.zst backup 1234 {m.group(2)}")
    sys.exit(0)
m = re.match(r"vzdump (\d+) --storage (\S+) --mode stop", cmd)
if m:
    v = m.group(1)
    if K.get("vzdump_fail"): sys.stderr.write("vzdump failed\n"); sys.exit(1)
    d = fs("/var/lib/vz/dump"); os.makedirs(d, exist_ok=True)
    name = f"vzdump-lxc-{v}-2026_11_13-22_00_00.tar.zst"
    open(d + "/" + name, "w").write("ARCHIVE-OF-" + v + "\n" + open(conf_path(v)).read())
    print("INFO: backup finished"); sys.exit(0)
m = re.match(r"pvesm path '(\S+):backup/(\S+)'", cmd)
if m:
    out("/var/lib/vz/dump/" + m.group(2)); sys.exit(0)
m = re.match(r"test ! -e '(.+)'", cmd)
if m:
    sys.exit(1 if os.path.exists(fs(m.group(1))) else 0)
m = re.match(r"cat '(.+)'$", cmd)
if m:
    sys.stdout.buffer.write(open(fs(m.group(1)), "rb").read()); sys.exit(0)
m = re.match(r"cat > '(.+)'$", cmd)
if m:
    p = fs(m.group(1)); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(sys.stdin.buffer.read()); sys.exit(0)
m = re.match(r"sha256sum '(.+)'$", cmd)
if m:
    print(hashlib.sha256(open(fs(m.group(1)), "rb").read()).hexdigest() + "  " + m.group(1)); sys.exit(0)
m = re.match(r"pct restore (\d+) '(\S+)' --storage '(\S+)'", cmd)
if m:
    v, volid = m.group(1), m.group(2)
    if K.get("restore_fail"): sys.stderr.write("restore failed\n"); sys.exit(1)
    fname = volid.split("backup/")[1]
    data = open(fs("/var/lib/vz/dump/" + fname)).read()
    conf = data.split("\n", 1)[1]
    open(conf_path(v), "w").write(conf); setstatus(v, "stopped"); sys.exit(0)
m = re.match(r"pct start (\d+)", cmd)
if m:
    v = m.group(1)
    if K.get("start_fail"): sys.stderr.write("start failed\n"); sys.exit(1)
    if K.get("never_running"): sys.exit(0)   # il comando "riesce" ma il CT non parte
    setstatus(v, "running"); setk("status_calls_" + v, 0); sys.exit(0)
m = re.match(r"pct set (\d+) --onboot 0", cmd)
if m:
    sys.exit(0)
m = re.match(r"pct set (\d+) --lock migrate", cmd)
if m:
    v = m.group(1)
    if K.get("lock_fail"): sys.stderr.write("500 unable to set lock\n"); sys.exit(255)
    c = open(conf_path(v)).read()
    if "\nlock:" not in "\n" + c:
        open(conf_path(v), "w").write("lock: migrate\n" + c)
    sys.exit(0)
m = re.match(r"pvesm free '(\S+)'", cmd)
if m:
    mm = re.match(r"\S+:backup/(\S+)", m.group(1))
    p = fs("/var/lib/vz/dump/" + mm.group(1))
    if os.path.exists(p): os.remove(p)
    sys.exit(0)
m = re.match(r"pct unlock (\d+)", cmd)
if m:
    v = m.group(1)
    c = re.sub(r"^lock: .*\n", "", open(conf_path(v)).read(), flags=re.M)
    open(conf_path(v), "w").write(c); sys.exit(0)
m = re.match(r"pct destroy (\d+) --purge", cmd)
if m:
    v = m.group(1)
    if K.get("destroy_fail"): sys.stderr.write("destroy failed: protection mode enabled\n"); sys.exit(255)
    if re.search(r"^lock: ", open(conf_path(v)).read(), re.M):
        sys.stderr.write(f"CT is locked (migrate)\n"); sys.exit(255)
    os.remove(conf_path(v))
    if os.path.exists(stat_path(v)): os.remove(stat_path(v))
    sys.exit(0)

sys.stderr.write("mockssh: comando non gestito: " + cmd + "\n")
sys.exit(97)

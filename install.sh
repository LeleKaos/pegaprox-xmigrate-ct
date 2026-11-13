#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# install.sh - installa, aggiorna o rimuove il plugin XMigrate CT in PegaProx
#
# Uso (come root, sul server PegaProx):
#   curl -fsSL https://raw.githubusercontent.com/LeleKaos/pegaprox-xmigrate-ct/main/install.sh | sudo bash
#   curl -fsSL .../install.sh | sudo bash -s -- --restart
#   sudo ./install.sh [opzioni]        (da una copia clonata della repo)
#
# Cosa fa:
#   1. trova la cartella dei plugin di PegaProx (o usa --dir)
#   2. scarica la repo (o usa la copia locale), controlla la sintassi dei file
#   3. salva in /var/backups/xmigrate_ct la versione gia' installata
#   4. copia i file del plugin (sostituzione atomica, mantiene il proprietario
#      della cartella plugin) e installa jq se manca
#   5. ti dice come attivare il plugin (oppure riavvia PegaProx con --restart)
#
# Non tocca la chiave SSH del plugin ne' la configurazione di PegaProx.
# =============================================================================
set -Eeuo pipefail

REPO="${XMIGRATE_REPO:-LeleKaos/pegaprox-xmigrate-ct}"
REF="main"
PLUGIN_ID="xmigrate_ct"
FILES=(manifest.json __init__.py ui.html xmigrate-ct.sh)
BACKUP_ROOT="${XMIGRATE_BACKUP_DIR:-/var/backups/xmigrate_ct}"
KEEP_BACKUPS=5

PLUGINS_DIR="" ACTION=install RESTART=0 DRY=0 FORCE=0 NO_DEPS=0 NO_BACKUP=0
TMP=""

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERRORE:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" -eq 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

usage() {
  cat <<'EOF'
Uso: install.sh [opzioni]

  --dir DIR        cartella dei plugin di PegaProx (default: rilevata in automatico,
                   oppure la variabile PEGAPROX_PLUGINS_DIR)
  --ref RIF        ramo, tag o commit da installare (default: main). Consigliato
                   un tag di versione, es. --ref v1.1.0
  --restart        riavvia il servizio pegaprox a fine installazione
                   (interrompe le sessioni aperte e i lavori di migrazione in corso)
  --uninstall      rimuove il plugin (con backup)
  --no-deps        non installare jq automaticamente se manca
  --no-backup      non salvare la versione precedente
  --force          prosegue anche se una migrazione e' in corso (sconsigliato)
  -n, --dry-run    mostra cosa farebbe, senza modificare nulla
  -h, --help

Variabili: XMIGRATE_REPO (utente/repo GitHub), XMIGRATE_ARCHIVE_URL (URL del
tar.gz da usare al posto di GitHub), XMIGRATE_BACKUP_DIR.
EOF
}

cleanup() { [ -z "$TMP" ] || rm -rf "$TMP"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

while [ $# -gt 0 ]; do
  case $1 in
    --dir)       PLUGINS_DIR=${2:?manca il valore di --dir}; shift 2 ;;
    --ref)       REF=${2:?manca il valore di --ref}; shift 2 ;;
    --restart)   RESTART=1; shift ;;
    --uninstall) ACTION=uninstall; shift ;;
    --no-deps)   NO_DEPS=1; shift ;;
    --no-backup) NO_BACKUP=1; shift ;;
    --force)     FORCE=1; shift ;;
    -n|--dry-run) DRY=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) usage >&2; die "opzione sconosciuta: $1" ;;
  esac
done
[[ $REF =~ ^[A-Za-z0-9._/-]+$ ]] || die "--ref non valido: $REF"
[[ $REPO =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "XMIGRATE_REPO non valido: $REPO (atteso utente/repo)"

# ----------------------------------------------------------------------------- cartella dei plugin
is_plugins_dir() { [ -d "$1" ]; }

detect_plugins_dir() {
  local -a cand=() found=() d
  # 1) fonti autorevoli: variabile d'ambiente, servizio systemd (e' li' che PegaProx legge i plugin)
  [ -z "${PEGAPROX_PLUGINS_DIR:-}" ] || cand+=("$PEGAPROX_PLUGINS_DIR")
  if command -v systemctl >/dev/null 2>&1; then
    local env wd
    env=$(systemctl show pegaprox -p Environment --value 2>/dev/null || true)
    for d in $env; do case $d in PEGAPROX_PLUGINS_DIR=*) cand+=("${d#*=}") ;; esac; done
    wd=$(systemctl show pegaprox -p WorkingDirectory --value 2>/dev/null || true)
    case $wd in /*) cand+=("$wd/plugins") ;; esac
  fi
  for d in "${cand[@]}"; do
    [ -n "$d" ] && is_plugins_dir "$d" || continue
    PLUGINS_DIR=$d; log "cartella dei plugin ricavata dal servizio/ambiente: $d"; return 0
  done
  # 2) percorsi abituali
  cand=(/opt/PegaProx/plugins /opt/pegaprox/plugins /var/lib/pegaprox/plugins /usr/lib/pegaprox/plugins)
  local -A seen=()
  for d in "${cand[@]}"; do
    [ -n "$d" ] || continue
    d=$(readlink -f "$d" 2>/dev/null || echo "$d")
    [ -z "${seen[$d]:-}" ] || continue
    seen[$d]=1
    if is_plugins_dir "$d"; then found+=("$d"); fi
  done
  if [ "${#found[@]}" -eq 1 ]; then PLUGINS_DIR=${found[0]}; return 0; fi
  if [ "${#found[@]}" -eq 0 ]; then
    die "cartella dei plugin di PegaProx non trovata. Indicala con --dir (con Docker e' il volume montato su PEGAPROX_PLUGINS_DIR, tipicamente .../config/plugins)"
  fi
  printf 'Trovate piu'"'"' cartelle dei plugin:\n' >&2
  printf '  %s\n' "${found[@]}" >&2
  die "indica quella giusta con --dir"
}

# ----------------------------------------------------------------------------- sorgente dei file
SRC_DIR=""
fetch_source() {
  # copia locale solo se lo script e' un file vero (con "curl | bash" non lo e': si scarica)
  local self="${BASH_SOURCE[0]:-}" here=""
  if [ -n "$self" ] && [ -f "$self" ]; then here=$(cd "$(dirname "$self")" && pwd); fi
  if [ -n "$here" ] && [ -f "$here/plugin/$PLUGIN_ID/manifest.json" ]; then
    SRC_DIR="$here/plugin/$PLUGIN_ID"
    log "uso i file locali: $SRC_DIR"
    return 0
  fi
  local url="${XMIGRATE_ARCHIVE_URL:-https://codeload.github.com/$REPO/tar.gz/$REF}"
  TMP=$(mktemp -d)
  log "scarico $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 2 --connect-timeout 15 -o "$TMP/src.tar.gz" "$url" || die "download non riuscito: $url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$TMP/src.tar.gz" "$url" || die "download non riuscito: $url"
  else
    die "serve curl o wget (oppure clona la repo e lancia ./install.sh da li')"
  fi
  mkdir "$TMP/x"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP/x" || die "archivio non valido"
  SRC_DIR=$(find "$TMP/x" -mindepth 3 -maxdepth 3 -type d -path "*/plugin/$PLUGIN_ID" | head -n 1)
  [ -n "$SRC_DIR" ] || die "l'archivio non contiene plugin/$PLUGIN_ID (riferimento '$REF' sbagliato?)"
}

check_source() {
  local f
  for f in "${FILES[@]}"; do [ -f "$SRC_DIR/$f" ] || die "manca $f nei file da installare"; done
  # copia di lavoro con fine riga LF (una checkout da Windows romperebbe lo script)
  local w="${TMP:-}"; [ -n "$w" ] || { TMP=$(mktemp -d); w=$TMP; }
  mkdir -p "$w/stage"
  for f in "${FILES[@]}"; do sed 's/\r$//' "$SRC_DIR/$f" > "$w/stage/$f"; done
  SRC_DIR="$w/stage"
  bash -n "$SRC_DIR/xmigrate-ct.sh" || die "xmigrate-ct.sh ha errori di sintassi: installazione annullata"
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$SRC_DIR/__init__.py" \
      || die "__init__.py ha errori di sintassi: installazione annullata"
    python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); assert m.get("id")==sys.argv[2]' "$SRC_DIR/manifest.json" "$PLUGIN_ID" \
      || die "manifest.json non valido: installazione annullata"
  fi
}

# ----------------------------------------------------------------------------- dipendenze
install_deps() {
  [ "$NO_DEPS" -eq 0 ] || return 0
  local missing=()
  command -v ssh        >/dev/null 2>&1 || missing+=("ssh (openssh-client)")
  command -v ssh-keygen >/dev/null 2>&1 || missing+=("ssh-keygen (openssh-client)")
  if [ "${#missing[@]}" -gt 0 ]; then
    warn "mancano: ${missing[*]}. Il plugin ne ha bisogno: installa il pacchetto openssh-client"
  fi
  if command -v jq >/dev/null 2>&1; then return 0; fi
  log "jq non trovato: lo installo (lo script lo usa per leggere le API di Proxmox; il servizio PegaProx non e' root e non potrebbe farlo da solo)"
  [ "$(id -u)" -eq 0 ] || { warn "non sei root: installa jq a mano (es. apt install jq)"; return 0; }
  if   command -v apt-get >/dev/null 2>&1; then run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq jq || { run apt-get update -qq && run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq jq; }
  elif command -v dnf     >/dev/null 2>&1; then run dnf install -y -q jq
  elif command -v yum     >/dev/null 2>&1; then run yum install -y -q jq
  elif command -v zypper  >/dev/null 2>&1; then run zypper --non-interactive install jq
  elif command -v apk     >/dev/null 2>&1; then run apk add --no-cache jq
  elif command -v pacman  >/dev/null 2>&1; then run pacman -Sy --noconfirm jq
  else warn "gestore di pacchetti non riconosciuto: installa jq a mano"; return 0
  fi || warn "installazione di jq non riuscita: installalo a mano (es. apt install jq)"
}

# ----------------------------------------------------------------------------- operazioni
busy_check() {
  [ "$FORCE" -eq 0 ] || return 0
  # processi "bash [opzioni] .../xmigrate-ct.sh ..." (non basta che il nome compaia in una riga di comando qualsiasi)
  if command -v pgrep >/dev/null 2>&1 && pgrep -f '^([^ ]*/)?(ba|da|z)?sh( +-[^ ]+)* +[^ ]*xmigrate-ct\.sh( |$)' >/dev/null 2>&1; then
    die "una migrazione (xmigrate-ct.sh) e' in corso: aspetta che finisca, oppure usa --force"
  fi
}

backup_current() {
  local dest="$1" ts
  [ -d "$dest" ] || return 0
  [ "$NO_BACKUP" -eq 0 ] || return 0
  ts=$(date +%Y%m%d-%H%M%S)-$$
  log "salvo la versione attuale in $BACKUP_ROOT/$ts"
  if [ "$DRY" -eq 0 ]; then
    # il backup contiene file di configurazione del plugin: leggibile solo da root
    ( umask 077
      mkdir -p "$BACKUP_ROOT/$ts"
      cp -a "$dest/." "$BACKUP_ROOT/$ts/"
      chmod 700 "$BACKUP_ROOT" "$BACKUP_ROOT/$ts" )
    # tiene solo gli ultimi KEEP_BACKUPS
    local -a all=()
    mapfile -t all < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d | sort)
    if [ "${#all[@]}" -gt "$KEEP_BACKUPS" ]; then
      local n=$(( ${#all[@]} - KEEP_BACKUPS )) i
      for ((i = 0; i < n; i++)); do rm -rf -- "${all[$i]}"; done
    fi
  else
    echo "    [dry-run] cp -a $dest/. $BACKUP_ROOT/$ts/"
  fi
}

manifest_version() { sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$1" 2>/dev/null | head -n 1; }

do_install() {
  local dest="$PLUGINS_DIR/$PLUGIN_ID" f old new owner="" mode
  old=$(manifest_version "$dest/manifest.json" || true)
  new=$(manifest_version "$SRC_DIR/manifest.json" || true)
  if [ -n "$old" ]; then log "aggiorno XMigrate CT: ${old:-?} -> ${new:-?}"; else log "installo XMigrate CT ${new:-?} in $dest"; fi

  busy_check
  backup_current "$dest"
  install_deps

  # il proprietario dei file e' quello della cartella dei plugin (l'utente del servizio PegaProx)
  if [ "$(id -u)" -eq 0 ]; then owner=$(stat -c '%u:%g' "$PLUGINS_DIR"); fi
  run mkdir -p "$dest"
  [ -z "$owner" ] || run chown "$owner" "$dest"
  for f in "${FILES[@]}"; do
    mode=0644; [ "$f" != xmigrate-ct.sh ] || mode=0755
    if [ "$DRY" -eq 1 ]; then echo "    [dry-run] installa $f -> $dest/$f ($mode)"; continue; fi
    # scrittura su file temporaneo + rename: uno script in esecuzione non viene mai riscritto a meta'
    cp "$SRC_DIR/$f" "$dest/.$f.new.$$"
    chmod "$mode" "$dest/.$f.new.$$"
    [ -z "$owner" ] || chown "$owner" "$dest/.$f.new.$$"
    mv -f "$dest/.$f.new.$$" "$dest/$f"
  done
  run rm -rf "$dest/__pycache__"
  log "file installati in $dest"
  after_install "$old"
}

do_uninstall() {
  local dest="$PLUGINS_DIR/$PLUGIN_ID"
  [ -d "$dest" ] || die "il plugin non e' installato in $dest"
  busy_check
  backup_current "$dest"
  run rm -rf -- "$dest"
  log "plugin rimosso da $dest"
  cat <<EOF

Restano, di proposito:
  - la chiave SSH del plugin (~/.ssh/xmigrate_ct_ed25519 dell'utente che esegue PegaProx):
    se non ti serve piu', cancellala e togli la sua chiave pubblica da
    ~/.ssh/authorized_keys di root sui nodi Proxmox;
  - il backup in $BACKUP_ROOT.
In PegaProx il plugin sparisce dall'elenco dopo "Rescan" (o un riavvio del servizio).
EOF
  restart_service
}

restart_service() {
  [ "$RESTART" -eq 1 ] || return 0
  if [ "$DRY" -eq 1 ]; then echo "    [dry-run] systemctl restart pegaprox"; return 0; fi
  if command -v systemctl >/dev/null 2>&1 && systemctl cat pegaprox >/dev/null 2>&1; then
    log "riavvio il servizio pegaprox"
    systemctl restart pegaprox || warn "riavvio non riuscito: riavvia PegaProx a mano"
  else
    warn "servizio 'pegaprox' non trovato: riavvia PegaProx a mano"
  fi
}

after_install() {
  local old="$1"
  if [ "$RESTART" -eq 1 ]; then restart_service; echo; log "Fatto. Ricarica la pagina di PegaProx (F5)."; return 0; fi
  echo
  if [ -n "$old" ]; then
    log "Fatto. Per caricare la nuova versione: in PegaProx disattiva e riattiva il plugin \"XMigrate CT\" (oppure riavvia il servizio: sudo systemctl restart pegaprox), poi F5."
  else
    log "Fatto. Ora in PegaProx: pagina dei plugin -> \"Rescan\", attiva \"XMigrate CT (privileged)\", poi F5: compare la tab \"XMigrate CT (privileged)\" dentro il cluster."
  fi
  cat <<EOF

Prima di usarlo:
  1. nella tab apri "Chiave SSH del plugin", copia la chiave pubblica e aggiungila a
     ~/.ssh/authorized_keys di root su un nodo di ogni cluster (origine e destinazione);
  2. chi usa il plugin deve avere il permesso "plugins.manage" in PegaProx;
  3. parti sempre con "Solo simulazione" (e' la spunta di default).
Guida completa: https://github.com/$REPO#readme
EOF
}

# ----------------------------------------------------------------------------- main
if [ "$(id -u)" -ne 0 ] && [ "$DRY" -eq 0 ]; then
  warn "non sei root: servono i permessi di scrittura sulla cartella dei plugin (di solito: sudo)"
fi
[ -n "$PLUGINS_DIR" ] || detect_plugins_dir
[ -d "$PLUGINS_DIR" ] || die "cartella dei plugin inesistente: $PLUGINS_DIR"
PLUGINS_DIR=$(readlink -f "$PLUGINS_DIR")
if [ "$DRY" -eq 0 ] && [ ! -w "$PLUGINS_DIR" ]; then die "nessun permesso di scrittura su $PLUGINS_DIR (usa sudo)"; fi
log "cartella dei plugin: $PLUGINS_DIR"

case $ACTION in
  install)   fetch_source; check_source; do_install ;;
  uninstall) do_uninstall ;;
esac

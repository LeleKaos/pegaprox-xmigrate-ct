#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# xmigrate-ct.sh - migrazione cross-cluster di container LXC (anche privilegiati)
# tramite backup/restore eseguiti come root via SSH.
#
# Perche': PDM e PegaProx migrano con un token API, e Proxmox riserva a root@pam
# (utente vero, non token) la modifica di "features" e righe lxc.* dei CT
# privilegiati -> 403 sul comando "config". Il restore lanciato da root via SSH
# non ha questo limite e mantiene la configurazione cosi' com'e'.
#
# Modalita' di backup (--mode):
#   auto   (default) PBS se lo STESSO PBS e' attivo su entrambi i nodi, altrimenti
#          backup locale su storage di staging (default: "local")
#   pbs    forza il PBS (errore se non e' in comune)
#   local  forza il backup locale
#
# Se lo storage PBS usa la cifratura lato client, la destinazione deve avere la
# stessa chiave: in caso di dubbio usa --mode local.
#
# Requisiti sull'host che lancia lo script: bash >= 4.3, ssh con chiave verso i
# due nodi (root). jq viene installato automaticamente se manca (apt/dnf/yum/
# pacman/zypper/apk/brew). Facoltativo: pv (barra di avanzamento).
#
# Cosa succede all'origine a fine lavoro:
#   - se NON la elimini (default) il CT resta spento con onboot=0 e viene marcato
#     "migrato" in automatico con lock: migrate, come fa Proxmox dopo una
#     remote-migrate senza --delete: non parte per sbaglio e si riconosce subito.
#     Per eliminarlo: pct unlock <vmid> && pct destroy <vmid> --purge
#   - --destroy-src lo ELIMINA (pct destroy --purge) ma SOLO se il CT sulla
#     destinazione e' stato avviato e risulta acceso e stabile; con --no-start,
#     con CT spento in origine o con --dry-run non elimina mai nulla; se la
#     verifica o l'eliminazione falliscono, l'origine resta e viene marcata;
#   - --cleanup-src riguarda solo l'archivio temporaneo di backup (modalita' local).
#
# Senza opzioni parte in modalita' guidata. Prima prova sempre con --dry-run.
# =============================================================================
set -Eeuo pipefail

SRC="" DST="" VMID="" TARGET_VMID="" DST_STORAGE="" MODE="auto" STAGING=""
IDENTITY="" ASSUME_YES=0 DRY_RUN=0 NO_START=0 CLEANUP_SRC=0
DESTROY_SRC=0 SRC_DESTROYED=0 DESTROY_FAILED=0 SRC_MARKED=0 DST_STARTED=0
PBS_SRC="" PBS_DST="" STAGE_SRC="" STAGE_DST="" STAGE_AVAIL_SRC=0 STAGE_AVAIL_DST=0
SRC_NODE="" DST_NODE="" CONF="" ALLOC=0 CHOICE=""
SRC_VOLID="" DST_VOLID="" DST_PATH=""
WAS_RUNNING=0 STOPPED_BY_US=0 CHANGES=0 DONE=0 RESTORED=0
INTERACTIVE=0
if [ -t 0 ] && [ -t 1 ]; then INTERACTIVE=1; fi
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o LogLevel=ERROR)

# ----------------------------------------------------------------------------- utilita'
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERRORE:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Uso: xmigrate-ct.sh [opzioni]
Senza opzioni parte in modalita' guidata (interattiva).

  -s, --src HOST         nodo di origine (IP/hostname; utente root se omesso)
  -d, --dst HOST         nodo di destinazione
  -c, --vmid ID          VMID del CT da migrare
  -t, --target-vmid ID   VMID sulla destinazione (default: lo stesso)
      --dst-storage ID   storage della destinazione per il disco del CT
      --mode M           auto (default) | pbs | local
      --staging ID       storage di staging per il backup locale (default: local)
      --no-start         non avviare il CT sulla destinazione
      --destroy-src      a fine lavoro ELIMINA il CT dall'origine (pct destroy --purge),
                         ma solo se il CT sulla destinazione e' stato avviato e risulta
                         acceso (non compatibile con --no-start e con CT spento)
                         Se l'origine NON viene eliminata, a fine lavoro viene marcata
                         in automatico come "migrata" (lock: migrate; pct unlock <vmid>)
      --cleanup-src      elimina SOLO l'archivio temporaneo di backup sull'origine
                         (modalita' local); NON elimina il CT
  -i, --identity FILE    chiave SSH da usare
  -n, --dry-run          solo controlli e piano, nessuna modifica
  -y, --yes              accetta tutte le conferme (obbligatorio senza terminale)
  -h, --help
EOF
}

rsh()        { local h=$1; shift; ssh -n "${SSH_OPTS[@]}" "$h" "$@"; }   # comando remoto
rsh_stream() { local h=$1; shift; ssh "${SSH_OPTS[@]}" "$h" "$@"; }      # con stdin/stdout

in_list() { local n=$1 x; shift; for x in "$@"; do [ "$x" = "$n" ] && return 0; done; return 1; }

need_value() {   # need_value "descrizione" --opzione  -> stampa il valore chiesto
  [ "$INTERACTIVE" -eq 1 ] || die "manca $2 (modalita' non interattiva)"
  local a=""
  while [ -z "$a" ]; do read -r -p "$1: " a || die "input interrotto"; done
  echo "$a"
}

ask() {          # ask "domanda" "default"
  local a=""
  read -r -p "$1 [$2]: " a || die "input interrotto"
  echo "${a:-$2}"
}

confirm() {      # confirm "domanda" [y|n]   (default n)
  local q=$1 d=${2:-n} a
  [ "$ASSUME_YES" -eq 1 ] && return 0
  [ "$INTERACTIVE" -eq 1 ] || return 1
  read -r -p "$q [$([ "$d" = y ] && echo Y/n || echo y/N)] " a || return 1
  a=${a:-$d}
  [[ $a =~ ^[YySs]$ ]]
}

risk() {         # segnala un rischio e chiede se proseguire
  warn "$1"
  confirm "Continuare comunque?" n || die "annullato"
}

choose() {       # choose "titolo" "default" opzioni...  -> imposta CHOICE
  local title=$1 def=$2; shift 2
  local -a opts=("$@"); local i a mark
  [ "${#opts[@]}" -gt 0 ] || die "nessuna opzione disponibile: $title"
  if [ "$INTERACTIVE" -ne 1 ]; then
    if [ -n "$def" ] && in_list "$def" "${opts[@]}"; then CHOICE=$def; return 0; fi
    die "$title: indicalo con un'opzione (modalita' non interattiva)"
  fi
  echo; echo "$title"
  for i in "${!opts[@]}"; do
    mark=""; [ "${opts[$i]}" = "$def" ] && mark="   (default)"
    printf '  %d) %s%s\n' $((i + 1)) "${opts[$i]}" "$mark"
  done
  while :; do
    read -r -p "Scelta: " a || die "input interrotto"
    if [ -z "$a" ] && [ -n "$def" ]; then CHOICE=$def; return 0; fi
    if [[ $a =~ ^[0-9]+$ ]] && [ "$a" -ge 1 ] && [ "$a" -le "${#opts[@]}" ]; then
      CHOICE=${opts[$((a - 1))]}; return 0
    fi
    echo "Scelta non valida."
  done
}

to_bytes() {     # 20G -> byte
  local v=$1 n u
  n=${v%[KMGT]}; u=${v#"$n"}
  [[ $n =~ ^[0-9]+$ ]] || { echo 0; return 0; }
  case $u in
    K) echo $((n * 1024)) ;;
    M) echo $((n * 1024 ** 2)) ;;
    G) echo $((n * 1024 ** 3)) ;;
    T) echo $((n * 1024 ** 4)) ;;
    *) echo "$n" ;;
  esac
}

human() { numfmt --to=iec "$1" 2>/dev/null || echo "$1 B"; }

# Garantisce che jq sia disponibile: se manca, prova a installarlo col package
# manager del sistema (apt/dnf/yum/pacman/zypper/apk/brew). Fallisce con un
# messaggio chiaro se non e' possibile.
ensure_jq() {
  command -v jq >/dev/null 2>&1 && return 0
  log "jq non trovato: tento l'installazione automatica"
  local pm="" sudo=""
  if   command -v apt-get >/dev/null 2>&1; then pm=apt
  elif command -v dnf       >/dev/null 2>&1; then pm=dnf
  elif command -v yum        >/dev/null 2>&1; then pm=yum
  elif command -v microdnf   >/dev/null 2>&1; then pm=microdnf
  elif command -v pacman     >/dev/null 2>&1; then pm=pacman
  elif command -v zypper     >/dev/null 2>&1; then pm=zypper
  elif command -v apk        >/dev/null 2>&1; then pm=apk
  elif command -v brew       >/dev/null 2>&1; then pm=brew
  elif command -v winget     >/dev/null 2>&1; then pm=winget
  elif command -v scoop      >/dev/null 2>&1; then pm=scoop
  elif command -v choco      >/dev/null 2>&1; then pm=choco
  else
    die "jq non trovato e nessun package manager supportato (apt/dnf/yum/microdnf/pacman/zypper/apk/brew). Installa jq manualmente, es. 'apt install jq'."
  fi
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then sudo=sudo
    else
      die "jq non trovato e non sei root: impossibile installare automaticamente (sudo non disponibile). Installa jq con il tuo package manager, es. 'apt install jq'."
    fi
  fi
  case $pm in
    apt)       $sudo apt-get update -qq && $sudo apt-get install -y -qq jq ;;
    dnf)       $sudo dnf install -y jq ;;
    yum)       $sudo yum install -y jq ;;
    microdnf)  $sudo microdnf install -y jq ;;
    pacman)    $sudo pacman -Sy --noconfirm jq ;;
    zypper)    $sudo zypper --non-interactive install jq ;;
    apk)       $sudo apk add --no-cache jq ;;
    brew)      brew install jq ;;
    winget)    winget install --id jqlang.jq --silent --accept-package-agreements --accept-source-agreements ;;
    scoop)     scoop install jq ;;
    choco)     choco install jq -y ;;
  esac || die "installazione di jq fallita: installala manualmente, es. 'apt install jq' (Windows: 'winget install jqlang.jq')"
  command -v jq >/dev/null 2>&1 || die "jq installato ma non nel PATH: riapri il terminale o aggiungi il binario al PATH"
  log "jq installato correttamente"
}

ct_alloc_bytes() {   # somma di size= su rootfs e mpN (esclusi backup=0): stima per eccesso
  local line tot=0 rest
  while IFS= read -r line; do
    [[ $line =~ ^(rootfs|mp[0-9]+):[[:space:]](.*)$ ]] || continue
    rest=${BASH_REMATCH[2]}
    [[ $rest == *backup=0* ]] && continue
    [[ $rest =~ size=([0-9]+[KMGT]?) ]] || continue
    tot=$((tot + $(to_bytes "${BASH_REMATCH[1]}")))
  done <<<"$1"
  echo "$tot"
}

# ----------------------------------------------------------------------------- Proxmox helpers
node_of()      { rsh "$1" hostname -s; }
stor_json()    { local n; n=$(node_of "$1"); rsh "$1" "pvesh get /nodes/$n/storage --content $2 --enabled 1 --output-format json"; }
stor_lines()   { stor_json "$1" "$2" | jq -r '.[] | select(.active == 1) | [.storage, .type, (.avail // 0)] | map(tostring) | join("|")'; }
cluster_of()   { rsh "$1" "pvesh get /cluster/status --output-format json 2>/dev/null" | jq -r '[.[] | select(.type == "cluster") | .name][0] // ""' || true; }
list_backups() { rsh "$1" "pvesm list $2 --content backup --vmid $3" | awk 'NR > 1 {print $1}' | sort; }

# Storage PBS attivi su un nodo: id|server|porta|datastore|namespace|fingerprint
pbs_list() {
  local node cfg st
  node=$(node_of "$1")
  cfg=$(rsh "$1" "pvesh get /storage --type pbs --output-format json")
  st=$(rsh "$1" "pvesh get /nodes/$node/storage --content backup --enabled 1 --output-format json")
  jq -rn --argjson cfg "$cfg" --argjson st "$st" '
    ($st | map(select(.active == 1) | .storage)) as $ok
    | $cfg[] | select(.storage as $s | $ok | index($s))
    | [.storage, .server, (.port // 8007), .datastore, (.namespace // ""), (.fingerprint // "")]
    | map(tostring) | join("|")'
}

# Imposta PBS_SRC / PBS_DST se esiste lo stesso PBS attivo su entrambi i nodi.
# "Stesso" = stessi datastore, namespace e porta + (stesso server oppure stesso fingerprint).
# Criterio conservativo: un falso negativo ricade sul backup locale, che e' sicuro.
find_common_pbs() {
  local a b ida sa pa da na fa idb sb pb db nb fb
  a=$(pbs_list "$SRC"); b=$(pbs_list "$DST")
  while IFS='|' read -r ida sa pa da na fa; do
    [ -n "$ida" ] || continue
    while IFS='|' read -r idb sb pb db nb fb; do
      [ -n "$idb" ] || continue
      [ "$da" = "$db" ] && [ "$na" = "$nb" ] && [ "$pa" = "$pb" ] || continue
      if [ "${sa,,}" = "${sb,,}" ] || { [ -n "$fa" ] && [ "$fa" = "$fb" ]; }; then
        PBS_SRC=$ida; PBS_DST=$idb; return 0
      fi
    done <<<"$b"
  done <<<"$a"
  return 1
}

pick_dst_storage() {
  local lines id type avail src_root def=""
  local -a ids=()
  lines=$(stor_lines "$DST" rootdir)
  while IFS='|' read -r id type avail; do [ -n "$id" ] && ids+=("$id"); done <<<"$lines"
  [ "${#ids[@]}" -gt 0 ] || die "$DST: nessuno storage attivo con content 'rootdir'"
  if [ -n "$DST_STORAGE" ]; then
    in_list "$DST_STORAGE" "${ids[@]}" || die "storage '$DST_STORAGE' non utilizzabile sulla destinazione (disponibili: ${ids[*]})"
  elif [ "${#ids[@]}" -eq 1 ]; then
    DST_STORAGE=${ids[0]}
  else
    src_root=$(sed -nE 's/^rootfs: ([^:]+):.*/\1/p' <<<"$CONF")
    if [ -n "$src_root" ] && in_list "$src_root" "${ids[@]}"; then def=$src_root; fi
    choose "Storage di destinazione per il disco del CT:" "$def" "${ids[@]}"
    DST_STORAGE=$CHOICE
  fi
  avail=$(awk -F'|' -v s="$DST_STORAGE" '$1 == s {print $3}' <<<"$lines")
  if [ "$ALLOC" -gt 0 ] && [ "${avail:-0}" -lt "$ALLOC" ]; then
    risk "spazio libero su '$DST_STORAGE' ($(human "${avail:-0}")) inferiore alla dimensione allocata del CT ($(human "$ALLOC"))"
  fi
}

pick_staging() {   # pick_staging SRC|DST
  local side=$1 host lines id type avail def=""
  local -a ids=()
  local -A av=()
  if [ "$side" = SRC ]; then host=$SRC; else host=$DST; fi
  lines=$(stor_lines "$host" backup | awk -F'|' '$2 != "pbs"')
  while IFS='|' read -r id type avail; do
    [ -n "$id" ] || continue
    ids+=("$id"); av[$id]=$avail
  done <<<"$lines"
  [ "${#ids[@]}" -gt 0 ] || die "$host: nessuno storage di tipo file attivo con content 'backup' (abilita 'Backup' su 'local' oppure aggiungi uno storage)"
  if [ -n "$STAGING" ]; then
    in_list "$STAGING" "${ids[@]}" || die "storage di staging '$STAGING' non utilizzabile su $host (disponibili: ${ids[*]})"
    CHOICE=$STAGING
  elif [ "${#ids[@]}" -eq 1 ]; then
    CHOICE=${ids[0]}
  else
    if in_list local "${ids[@]}"; then def=local; fi
    choose "Storage di staging su $host (content 'backup'):" "$def" "${ids[@]}"
  fi
  if [ "$side" = SRC ]; then
    STAGE_SRC=$CHOICE; STAGE_AVAIL_SRC=${av[$CHOICE]}
  else
    STAGE_DST=$CHOICE; STAGE_AVAIL_DST=${av[$CHOICE]}
  fi
}

ensure_staging() {
  [ -n "$STAGE_SRC" ] || pick_staging SRC
  [ -n "$STAGE_DST" ] || pick_staging DST
  if [ "$ALLOC" -gt 0 ] && { [ "$STAGE_AVAIL_SRC" -lt "$ALLOC" ] || [ "$STAGE_AVAIL_DST" -lt "$ALLOC" ]; }; then
    risk "spazio di staging (origine $(human "$STAGE_AVAIL_SRC"), destinazione $(human "$STAGE_AVAIL_DST")) inferiore alla dimensione allocata del CT ($(human "$ALLOC")); e' una stima per eccesso: l'archivio compresso e' di solito molto piu' piccolo"
  fi
}

select_mode() {
  local found=0
  if [ "$MODE" != local ]; then
    if find_common_pbs; then
      found=1; log "PBS in comune: '$PBS_SRC' (origine) = '$PBS_DST' (destinazione)"
    else
      log "nessun PBS in comune attivo su entrambi i nodi"
    fi
  fi
  case $MODE in
    pbs)  [ "$found" -eq 1 ] || die "--mode pbs richiesto ma non esiste un PBS in comune" ;;
    auto) MODE=local
          if [ "$found" -eq 1 ] && confirm "Uso il PBS in comune per il backup?" y; then MODE=pbs; fi ;;
  esac
  if [ "$MODE" = local ]; then ensure_staging; fi
}

# ----------------------------------------------------------------------------- fasi operative
backup_phase() {
  local store extra="" before after new
  if [ "$MODE" = pbs ]; then store=$PBS_SRC; else store=$STAGE_SRC; extra="--compress zstd"; fi
  before=$(list_backups "$SRC" "$store" "$VMID")
  log "backup del CT $VMID su '$store' (modalita' stop)"
  # shellcheck disable=SC2086
  rsh "$SRC" "vzdump $VMID --storage $store --mode stop $extra"
  after=$(list_backups "$SRC" "$store" "$VMID")
  new=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after"))
  [ -n "$new" ] && [ "$(wc -l <<<"$new")" -eq 1 ] || die "impossibile identificare con certezza il nuovo backup (trovati: ${new:-nessuno})"
  SRC_VOLID=$new
  log "nuovo backup: $SRC_VOLID"
}

transfer_local() {
  local src_path file s1 s2 base
  local -a viewer=(cat)
  command -v pv >/dev/null 2>&1 && viewer=(pv -pterb)
  src_path=$(rsh "$SRC" "pvesm path '$SRC_VOLID'")
  file=${SRC_VOLID#*backup/}
  DST_VOLID="${STAGE_DST}:backup/${file}"
  if ! DST_PATH=$(rsh "$DST" "pvesm path '$DST_VOLID'" 2>/dev/null); then
    base=$(rsh "$DST" "pvesh get /storage/$STAGE_DST --output-format json" | jq -r '.path // empty')
    [ -n "$base" ] || die "impossibile ricavare il percorso dello storage '$STAGE_DST' sulla destinazione"
    DST_PATH="$base/dump/$file"
  fi
  rsh "$DST" "test ! -e '$DST_PATH'" || die "$DST_PATH esiste gia' sulla destinazione"
  log "copia $file: $SRC -> $DST"
  rsh_stream "$SRC" "cat '$src_path'" | "${viewer[@]}" | rsh_stream "$DST" "cat > '$DST_PATH'"
  s1=$(rsh "$SRC" "sha256sum '$src_path'" | awk '{print $1}')
  s2=$(rsh "$DST" "sha256sum '$DST_PATH'" | awk '{print $1}')
  [ -n "$s1" ] && [ "$s1" = "$s2" ] || die "checksum diverso dopo la copia (origine=$s1 destinazione=$s2)"
  log "checksum verificato (sha256 ${s1:0:16}...)"
}

backup_and_stage() {
  backup_phase
  if [ "$MODE" = pbs ]; then
    DST_VOLID="${PBS_DST}:${SRC_VOLID#*:}"
    if grep -qxF -- "$DST_VOLID" <<<"$(list_backups "$DST" "$PBS_DST" "$VMID")"; then
      log "il backup e' visibile dalla destinazione ($DST_VOLID)"
      return 0
    fi
    warn "il backup non e' visibile dalla destinazione (permessi/namespace?): passo al backup locale"
    MODE=local; ensure_staging; backup_phase
  fi
  transfer_local
}

wait_running() {   # wait_running host vmid
  local st="" i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    st=$(rsh "$1" "pct status $2" || true)
    [[ $st == *running* ]] && return 0
    sleep 2
  done
  return 1
}

rollback() {
  local st=""
  warn "operazione interrotta: controllo lo stato dell'origine"
  if [ "$STOPPED_BY_US" -eq 1 ] && [ "$WAS_RUNNING" -eq 1 ]; then
    st=$(rsh "$DST" "pct status $TARGET_VMID" 2>/dev/null || true)
    if [[ $st == *running* ]]; then
      warn "il CT $TARGET_VMID e' acceso sulla destinazione: NON riavvio l'origine"
    elif rsh "$SRC" "pct start $VMID"; then
      log "CT $VMID riavviato sull'origine"
    else
      warn "riavvio non riuscito: esegui 'pct start $VMID' su $SRC"
    fi
  fi
  [ -z "$DST_PATH" ] || warn "eventuale file temporaneo rimasto su $DST: $DST_PATH"
  [ "$RESTORED" -eq 0 ] || warn "il CT $TARGET_VMID ripristinato sulla destinazione e' rimasto (spento): eliminalo con 'pct destroy $TARGET_VMID' su $DST prima di riprovare"
}

on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$DONE" -eq 0 ] && [ "$CHANGES" -eq 1 ]; then rollback || true; fi
  exit "$rc"
}

# Il CT sulla destinazione deve essere acceso DAVVERO: oltre al controllo gia' fatto
# subito dopo l'avvio, ricontrolla dopo una breve pausa (un CT che parte e crolla
# subito non deve far eliminare l'origine). Pausa regolabile con XMIGRATE_SETTLE.
dst_ct_ok() {
  local st=""
  [ "$DST_STARTED" -eq 1 ] || { warn "il CT $TARGET_VMID non e' stato avviato da questo script"; return 1; }
  rsh "$DST" "pct config $TARGET_VMID" >/dev/null 2>&1 || { warn "il CT $TARGET_VMID non risulta sulla destinazione"; return 1; }
  sleep "${XMIGRATE_SETTLE:-5}"
  st=$(rsh "$DST" "pct status $TARGET_VMID" 2>/dev/null || true)
  [[ $st == *running* ]] || { warn "il CT $TARGET_VMID non risulta acceso sulla destinazione"; return 1; }
  return 0
}

# Elimina il CT dall'origine solo se la destinazione e' verificata.
# Imposta SRC_DESTROYED=1 se eliminato, DESTROY_FAILED=1 se richiesto ma non fatto.
destroy_source() {
  local ans=""
  if [ "$DESTROY_SRC" -ne 1 ]; then
    { [ "$INTERACTIVE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ] && [ "$DST_STARTED" -eq 1 ]; } || return 0
  fi
  if ! dst_ct_ok; then
    warn "NON elimino il CT $VMID dall'origine: la destinazione non e' verificata"
    [ "$DESTROY_SRC" -ne 1 ] || DESTROY_FAILED=1
    return 0
  fi
  if [ "$DESTROY_SRC" -ne 1 ]; then
    echo
    read -r -p "Eliminare ORA il CT $VMID dall'origine ($SRC_NODE)? Scrivi il VMID per confermare, invio per lasciarlo: " ans || return 0
    [ "$ans" = "$VMID" ] || return 0
  fi
  log "il CT $TARGET_VMID e' acceso sulla destinazione: elimino il CT $VMID dall'origine ($SRC_NODE)"
  if rsh "$SRC" "pct destroy $VMID --purge"; then
    SRC_DESTROYED=1
  else
    warn "pct destroy non riuscito (protezione attiva? vedi 'pct config $VMID'): eliminalo a mano su $SRC_NODE"
    DESTROY_FAILED=1
  fi
}

# Origine non eliminata -> la marca come "migrata" (lock: migrate), come fa Proxmox
# con remote-migrate senza --delete. Se non riesce non e' un problema: l'origine
# resta comunque spenta con onboot=0.
mark_source() {
  [ "$SRC_DESTROYED" -eq 0 ] || return 0
  if rsh "$SRC" "pct set $VMID --lock migrate"; then
    SRC_MARKED=1
    log "origine marcata come migrata (lock: migrate)"
  else
    warn "impossibile impostare lock: migrate sull'origine: il CT $VMID resta spento con onboot=0, nessun altro effetto"
  fi
}

# ----------------------------------------------------------------------------- main
parse_args() {
  while [ $# -gt 0 ]; do
    case $1 in
      -s|--src)         SRC=${2:?}; shift 2 ;;
      -d|--dst)         DST=${2:?}; shift 2 ;;
      -c|--vmid)        VMID=${2:?}; shift 2 ;;
      -t|--target-vmid) TARGET_VMID=${2:?}; shift 2 ;;
      --dst-storage)    DST_STORAGE=${2:?}; shift 2 ;;
      --mode)           MODE=${2:?}; shift 2 ;;
      --staging)        STAGING=${2:?}; shift 2 ;;
      --no-start)       NO_START=1; shift ;;
      --destroy-src)    DESTROY_SRC=1; shift ;;
      --cleanup-src)    CLEANUP_SRC=1; shift ;;
      -i|--identity)    IDENTITY=${2:?}; shift 2 ;;
      -n|--dry-run)     DRY_RUN=1; shift ;;
      -y|--yes)         ASSUME_YES=1; shift ;;
      -h|--help)        usage; exit 0 ;;
      *) usage >&2; die "opzione sconosciuta: $1" ;;
    esac
  done
  case $MODE in auto|pbs|local) ;; *) die "--mode deve essere auto, pbs o local" ;; esac
  if [ "$DESTROY_SRC" -eq 1 ] && [ "$NO_START" -eq 1 ]; then
    die "--destroy-src non e' compatibile con --no-start: l'origine si elimina solo se il CT e' stato avviato e verificato sulla destinazione"
  fi
}

with_user() { case $1 in *@*) echo "$1" ;; *) echo "root@$1" ;; esac; }

main() {
  parse_args "$@"
  command -v ssh >/dev/null || die "ssh non trovato"
  ensure_jq
  # Host key: di default non blocca su host non ancora in known_hosts (evita
  # "Host key verification failed" sul server che lancia lo script, es. PegaProx).
  # Per produzione impostare XMIGRATE_STRICT_HOSTKEY=1 e fare ssh-keyscan dei nodi.
  if [ "${XMIGRATE_STRICT_HOSTKEY:-0}" != "1" ]; then
    SSH_OPTS+=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
  fi
  # Se non viene indicata una chiave, prova a trovarne una in posizioni standard.
  if [ -z "$IDENTITY" ]; then
    for k in \
      "$HOME/.ssh/xmigrate_ct_ed25519" "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa" "$HOME/.ssh/id_ecdsa" \
      "/opt/pegaprox/.ssh/xmigrate_ct_ed25519" "/opt/pegaprox/.ssh/id_ed25519" "/opt/pegaprox/.ssh/id_rsa" \
      "/var/lib/pegaprox/.ssh/xmigrate_ct_ed25519" "/var/lib/pegaprox/.ssh/id_ed25519" "/var/lib/pegaprox/.ssh/id_rsa"; do
      if [ -f "$k" ]; then IDENTITY="$k"; break; fi
    done
  fi
  [ -z "$IDENTITY" ] || SSH_OPTS+=(-i "$IDENTITY")
  if [ -z "$IDENTITY" ]; then
    die "nessuna chiave SSH trovata: genera una (ssh-keygen) o indica --identity / -i"
  fi

  echo "xmigrate-ct: migrazione CT LXC tra cluster (backup/restore come root)"
  [ -n "$SRC" ] || SRC=$(need_value "Nodo di ORIGINE (IP o hostname)" --src)
  [ -n "$DST" ] || DST=$(need_value "Nodo di DESTINAZIONE (IP o hostname)" --dst)
  SRC=$(with_user "$SRC"); DST=$(with_user "$DST")
  [ "$SRC" != "$DST" ] || die "origine e destinazione coincidono"

  local h
  for h in "$SRC" "$DST"; do
    rsh "$h" true || die "SSH verso $h non riuscito (chiave? usa --identity)"
    rsh "$h" "command -v pct vzdump pvesm pvesh >/dev/null" || die "$h non sembra un nodo Proxmox VE"
  done
  SRC_NODE=$(node_of "$SRC"); DST_NODE=$(node_of "$DST")

  local c1 c2
  c1=$(cluster_of "$SRC"); c2=$(cluster_of "$DST")
  if [ -n "$c1" ] && [ "$c1" = "$c2" ]; then
    risk "origine e destinazione sono nello stesso cluster ('$c1'): usa 'pct migrate', non serve questo script"
  fi

  # --- scelta del CT
  if [ -z "$VMID" ]; then
    local -a cts=()
    mapfile -t cts < <(rsh "$SRC" "pvesh get /nodes/$SRC_NODE/lxc --output-format json" \
      | jq -r 'sort_by(.vmid)[] | "\(.vmid)  \(.name // "-")  [\(.status)]\(if .lock then "  lock:" + .lock else "" end)"')
    [ "${#cts[@]}" -gt 0 ] || die "nessun container LXC su $SRC_NODE"
    choose "Container da migrare (nodo $SRC_NODE):" "" "${cts[@]}"
    VMID=${CHOICE%% *}
  fi
  [[ $VMID =~ ^[0-9]+$ ]] || die "VMID non valido: $VMID"
  CONF=$(rsh "$SRC" "pct config $VMID") || die "CT $VMID non trovato su $SRC_NODE (lo script va puntato sul nodo che lo ospita)"
  if grep -q '^lock: migrate' <<<"$CONF"; then
    die "il CT $VMID ha lock 'migrate' sull'origine: risulta gia' migrato (o e' in corso una migrazione). Verifica e, se vuoi rifare, 'pct unlock $VMID' su $SRC_NODE"
  fi
  if grep -q '^lock:' <<<"$CONF"; then die "il CT $VMID ha un lock attivo sull'origine (pct unlock $VMID dopo aver verificato)"; fi
  local ha
  ha=$(rsh "$SRC" "pvesh get /cluster/ha/resources --output-format json 2>/dev/null || echo '[]'" \
    | jq -r --arg s "ct:$VMID" '[.[] | select(.sid == $s)] | length' || true)
  [ "${ha:-0}" -eq 0 ] || die "il CT $VMID e' una risorsa HA: rimuovila dall'HA prima di migrarlo (altrimenti l'origine verrebbe riavviata)"

  local name priv st
  name=$(sed -nE 's/^hostname: (.*)$/\1/p' <<<"$CONF")
  if grep -q '^unprivileged: 1' <<<"$CONF"; then priv="non privilegiato"; else priv="PRIVILEGIATO"; fi
  st=$(rsh "$SRC" "pct status $VMID" || true)
  if [[ $st == *running* ]]; then WAS_RUNNING=1; fi
  ALLOC=$(ct_alloc_bytes "$CONF")
  if [ "$DESTROY_SRC" -eq 1 ] && [ "$WAS_RUNNING" -eq 0 ]; then
    die "il CT $VMID e' spento sull'origine: sulla destinazione non verrebbe avviato e non potrei verificarne l'avvio, quindi non lo elimino. Togli --destroy-src (e se vuoi eliminalo a mano dopo aver controllato)"
  fi

  # --- VMID di destinazione (controllo sull'intero cluster)
  if [ -z "$TARGET_VMID" ]; then
    if [ "$INTERACTIVE" -eq 1 ]; then TARGET_VMID=$(ask "VMID sulla destinazione" "$VMID"); else TARGET_VMID=$VMID; fi
  fi
  [[ $TARGET_VMID =~ ^[0-9]+$ ]] || die "VMID di destinazione non valido: $TARGET_VMID"
  local used
  used=$(rsh "$DST" "pvesh get /cluster/resources --type vm --output-format json" \
    | jq -r --argjson v "$TARGET_VMID" '[.[] | select(.vmid == $v)] | length')
  [ "${used:-0}" -eq 0 ] || die "il VMID $TARGET_VMID e' gia' in uso nel cluster di destinazione (usa --target-vmid)"

  pick_dst_storage

  # --- controlli sulla configurazione
  local br
  for br in $(grep -oP 'bridge=\K[^,]+' <<<"$CONF" | sort -u); do
    rsh "$DST" "test -d /sys/class/net/$br" || risk "il bridge '$br' non esiste su $DST_NODE: il CT non partira' finche' non lo crei o cambi bridge"
  done
  if grep -qE '^(lxc\.|dev[0-9]+:)' <<<"$CONF" || grep -qE '^mp[0-9]+: /' <<<"$CONF"; then
    warn "il CT usa righe lxc.* raw, device passthrough o bind mount: la config viene mantenuta, ma verifica che hardware e percorsi esistano su $DST_NODE"
  fi

  select_mode

  # --- riepilogo
  echo
  echo "================ RIEPILOGO ================"
  printf '  CT:             %s (%s) - %s - %s\n' "$VMID" "${name:--}" "$priv" "$([ "$WAS_RUNNING" -eq 1 ] && echo acceso || echo spento)"
  printf '  Origine:        %s (nodo %s%s)\n' "$SRC" "$SRC_NODE" "${c1:+, cluster $c1}"
  printf '  Destinazione:   %s (nodo %s%s) - VMID %s - storage %s\n' "$DST" "$DST_NODE" "${c2:+, cluster $c2}" "$TARGET_VMID" "$DST_STORAGE"
  if [ "$MODE" = pbs ]; then
    printf '  Backup:         PBS  %s -> %s\n' "$PBS_SRC" "$PBS_DST"
  else
    printf '  Backup:         locale  staging %s (origine) -> %s (destinazione), con sha256\n' "$STAGE_SRC" "$STAGE_DST"
  fi
  printf '  Dimensione:     %s allocati (stima per eccesso)\n' "$(human "$ALLOC")"
  echo "  Sequenza:       spegni origine -> backup -> trasferimento -> restore come root -> avvio su destinazione"
  if [ "$DESTROY_SRC" -eq 1 ]; then
    echo "  A fine lavoro:  il CT sull'origine viene ELIMINATO (pct destroy --purge) SOLO se il CT $TARGET_VMID"
    echo "                  e' avviato e acceso sulla destinazione; altrimenti l'origine resta com'e' (spenta, onboot=0)"
  else
    echo "  A fine lavoro:  l'origine resta SPENTA con onboot=0 e marcata 'migrata' (lock: migrate); nessuna eliminazione"
  fi
  if [ "$CLEANUP_SRC" -eq 1 ]; then echo "  Pulizia:        --cleanup-src elimina solo l'archivio temporaneo sull'origine, NON il CT"; fi
  echo "==========================================="
  if [ "$DRY_RUN" -eq 1 ]; then log "dry-run: nessuna modifica effettuata"; exit 0; fi
  if ! confirm "Procedo?" n; then
    if [ "$INTERACTIVE" -eq 1 ]; then die "annullato, nessuna modifica effettuata"; fi
    die "annullato: senza terminale serve --yes"
  fi

  # --- esecuzione
  trap on_exit EXIT
  trap 'exit 130' INT TERM
  CHANGES=1
  if [ "$WAS_RUNNING" -eq 1 ]; then
    log "spengo il CT $VMID sull'origine"
    STOPPED_BY_US=1
    rsh "$SRC" "pct shutdown $VMID --forceStop 1 --timeout 60"
  fi
  st=$(rsh "$SRC" "pct status $VMID")
  [[ $st == *stopped* ]] || die "il CT $VMID non risulta spento sull'origine"

  backup_and_stage

  log "restore sulla destinazione come CT $TARGET_VMID (storage $DST_STORAGE)"
  rsh "$DST" "pct restore $TARGET_VMID '$DST_VOLID' --storage '$DST_STORAGE'"
  RESTORED=1

  if [ "$WAS_RUNNING" -eq 1 ] && [ "$NO_START" -eq 0 ]; then
    log "avvio il CT $TARGET_VMID sulla destinazione"
    rsh "$DST" "pct start $TARGET_VMID"
    wait_running "$DST" "$TARGET_VMID" || die "il CT $TARGET_VMID non risulta acceso sulla destinazione"
    DST_STARTED=1
  fi
  DONE=1

  # --- chiusura (da qui in poi niente rollback)
  rsh "$SRC" "pct set $VMID --onboot 0" || warn "impostare onboot=0 sull'origine non e' riuscito: fallo a mano"
  if [ "$MODE" = local ]; then
    rsh "$DST" "pvesm free '$DST_VOLID'" || warn "pulizia di $DST_VOLID sulla destinazione non riuscita"
    if [ "$CLEANUP_SRC" -eq 1 ]; then
      rsh "$SRC" "pvesm free '$SRC_VOLID'" || warn "pulizia di $SRC_VOLID sull'origine non riuscita"
    else
      log "archivio lasciato sull'origine come rete di sicurezza: $SRC_VOLID"
    fi
  else
    log "il backup resta sul PBS: $SRC_VOLID"
    [ "$CLEANUP_SRC" -eq 0 ] || warn "--cleanup-src non ha effetto con il PBS: non c'e' nessun archivio di staging"
  fi

  destroy_source
  mark_source

  echo
  if [ "$SRC_DESTROYED" -eq 1 ]; then
    log "FATTO. CT $VMID migrato su $DST_NODE (come $TARGET_VMID) ed eliminato dall'origine."
  else
    log "FATTO. Verifica il CT $TARGET_VMID su $DST_NODE. Il CT $VMID resta SPENTO sull'origine$([ "$SRC_MARKED" -eq 1 ] && echo " (lock: migrate)"); per eliminarlo:"
    if [ "$SRC_MARKED" -eq 1 ]; then
      echo "      pct unlock $VMID && pct destroy $VMID --purge      # su $SRC_NODE"
    else
      echo "      pct destroy $VMID --purge      # su $SRC_NODE   (oppure rilancia con --destroy-src)"
    fi
  fi
  [ "$DESTROY_FAILED" -eq 0 ] || exit 3
}

main "$@"

#!/usr/bin/env bash
#
# Install the repo's Signal K configuration onto the Pi.
#
# baseDeltas.json, not defaults.json. The two sit side by side in ~/.signalk and
# look interchangeable, and they are not: defaults.json is the legacy shape,
# an object of {"vessels": {"self": {...}}}, while baseDeltas.json is an array
# of deltas. Signal K reads whichever file it is given in whichever shape that
# file expects, and an array in defaults.json is parsed, found to have no
# vessels.self, and discarded without a word. The boat then has no name, no
# dimensions, and a new uuid every restart, which is exactly what happened here.
#
# The repo is the source of truth for signalk/. Secrets are NOT in the repo:
# the plugin config files carry @PLACEHOLDER@ tokens that are filled from .env
# at install time, so the MPPT encryption key never gets committed.
#
#   ./deploy/apply-signalk-config.sh              # install and restart Signal K
#   ./deploy/apply-signalk-config.sh --dry-run    # show what would change
#
# Anything you change in the Signal K admin UI is overwritten by this script -
# copy UI changes back into signalk/ or they are lost. Previous versions are
# kept as <file>.bak-<timestamp> next to the originals.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/signalk"
SIGNALK_HOME="${SIGNALK_HOME:-$HOME/.signalk}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
STAMP="$(date +%Y%m%d%H%M%S)"

# Secrets the templates can carry. None of them is required to install the
# rest: the N2K bus and the GPS are what the alarms depend on, and the MPPT is
# one instrument on the side of that. A boat waiting on a Bluetooth key it can
# only read standing next to the charger should not also be waiting for its
# anchor watch.
SECRET_VARS=(VICTRON_MPPT_MAC VICTRON_MPPT_KEY)

# Which secrets a file cannot be installed without. A file whose secrets are
# missing is skipped and said out loud, not filled in with an empty string -
# a plugin configured with a blank key fails in a much quieter way.
declare -A FILE_NEEDS=(
  ["plugin-config-data/signalk-victron-ble.json"]="VICTRON_MPPT_MAC VICTRON_MPPT_KEY"
)

# Files that end up holding a secret, so must not be world-readable.
declare -A FILE_MODE=(
  ["plugin-config-data/signalk-victron-ble.json"]=600
)

DRY_RUN=0
NO_RESTART=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: deploy/apply-signalk-config.sh [--dry-run] [--no-restart]

  --dry-run     Show the diff against what is installed, change nothing
  --no-restart  Install but do not restart signalk.service

Environment overrides: SIGNALK_HOME (default ~/.signalk), ENV_FILE (default ./.env)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage; die "unknown option: $1" ;;
  esac
  shift
done

command -v jq >/dev/null || die "jq is required (apt install jq)"
[[ -d "$SRC_DIR" ]] || die "no signalk/ directory at $SRC_DIR"

# --------------------------------------------------------------- secrets ----

load_env() {
  [[ -f "$ENV_FILE" ]] || die "no $ENV_FILE - copy .env.example and fill in what you have"

  local perms
  perms="$(stat -c '%a' "$ENV_FILE")"
  if [[ "$perms" != "600" && "$perms" != "400" ]]; then
    warn "$ENV_FILE is mode $perms - it holds secrets, consider chmod 600"
  fi

  # Only take simple KEY=VALUE lines; never execute the file.
  local line key value
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf -v "$key" '%s' "$value"
  done < "$ENV_FILE"

  local var found=0
  for var in "${SECRET_VARS[@]}"; do
    [[ -n "${!var:-}" ]] && found=$((found + 1))
  done
  info "loaded $found of ${#SECRET_VARS[@]} optional secrets from $ENV_FILE"

  # Half a Victron config is a mistake rather than a choice, so say so. Both
  # empty is fine and simply means the MPPT is not set up yet.
  if [[ -n "${VICTRON_MPPT_MAC:-}" || -n "${VICTRON_MPPT_KEY:-}" ]]; then
    [[ -n "${VICTRON_MPPT_MAC:-}" && -n "${VICTRON_MPPT_KEY:-}" ]] \
      || die "set both VICTRON_MPPT_MAC and VICTRON_MPPT_KEY in $ENV_FILE, or neither"

    # Catch the two mistakes that otherwise show up as a silently dead plugin.
    [[ "$VICTRON_MPPT_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] \
      || die "VICTRON_MPPT_MAC is not a MAC address: $VICTRON_MPPT_MAC"
    [[ "$VICTRON_MPPT_KEY" =~ ^[0-9A-Fa-f]{32}$ ]] \
      || die "VICTRON_MPPT_KEY must be 32 hex chars (the advertisement key from VictronConnect)"
  fi
}

# --------------------------------------------------------------- render ----

# Substitute @VAR@ tokens, drop "$comment" keys, and pretty-print. Anything left
# looking like a placeholder is a missing secret, not something to install.
render() {
  local src="$1" out="$2" var
  cp "$src" "$out"

  for var in "${SECRET_VARS[@]}"; do
    local value="${!var:-}"
    value="${value//\\/\\\\}"
    value="${value//|/\\|}"
    sed -i "s|@${var}@|${value}|g" "$out"
  done

  if grep -qE '@[A-Z_][A-Z0-9_]*@' "$out"; then
    local leftover
    leftover="$(grep -oE '@[A-Z_][A-Z0-9_]*@' "$out" | sort -u | tr '\n' ' ')"
    die "$(basename "$src"): unresolved placeholders: $leftover"
  fi

  jq 'del(.. | objects | ."$comment"?)' "$out" > "$out.jq" 2>/dev/null \
    || die "$(basename "$src"): invalid JSON"
  mv "$out.jq" "$out"
}

# Redact secrets so a --dry-run diff can be pasted into a message or a log.
redact() {
  local var out
  out="$(cat)"
  for var in "${SECRET_VARS[@]}"; do
    [[ -n "${!var:-}" ]] || continue
    out="${out//${!var}/<$var>}"
  done
  printf '%s\n' "$out"
}

install_file() {
  # One `local` per line: bash expands every argument of `local` before it
  # performs any of the assignments, so `local a="$1" b="$a"` reads a stale $a.
  local rel="$1"
  local src="$SRC_DIR/$rel"
  local dest="$SIGNALK_HOME/$rel"
  local tmp="$TMPDIR_WORK/$(basename "$rel")"

  # Skip rather than install a half-filled file. The plugin then stays as it
  # was, which is honest, instead of running with a blank key and going quiet.
  local var
  for var in ${FILE_NEEDS[$rel]:-}; do
    if [[ -z "${!var:-}" ]]; then
      info "skipped: $rel (needs $var in $ENV_FILE)"
      return
    fi
  done

  render "$src" "$tmp"

  if [[ -f "$dest" ]] && diff -q "$dest" "$tmp" >/dev/null 2>&1; then
    info "unchanged: $rel"
    return
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    local current=/dev/null
    [[ -f "$dest" ]] && current="$dest"
    log "would install: $rel"
    diff -u --label "installed/$rel" --label "repo/$rel" "$current" "$tmp" | redact || true
    return
  fi

  mkdir -p "$(dirname "$dest")"
  if [[ -f "$dest" ]]; then
    cp -p "$dest" "$dest.bak-$STAMP"
    info "backed up: $rel -> $rel.bak-$STAMP"
  fi

  mv "$tmp" "$dest"
  chmod "${FILE_MODE[$rel]:-644}" "$dest"
  log "installed: $rel (mode ${FILE_MODE[$rel]:-644})"
}

# ----------------------------------------------------------------- main ----

main() {
  load_env

  TMPDIR_WORK="$(mktemp -d)"
  trap 'rm -rf "$TMPDIR_WORK"' EXIT

  [[ $DRY_RUN -eq 1 ]] && log "dry run - nothing will be written"
  log "Installing Signal K config into $SIGNALK_HOME"
  mkdir -p "$SIGNALK_HOME/plugin-config-data"

  local rel src
  for rel in settings.json baseDeltas.json; do
    [[ -f "$SRC_DIR/$rel" ]] && install_file "$rel"
  done
  for src in "$SRC_DIR"/plugin-config-data/*.json; do
    [[ -e "$src" ]] || continue
    install_file "plugin-config-data/$(basename "$src")"
  done

  if [[ $DRY_RUN -eq 1 || $NO_RESTART -eq 1 ]]; then
    [[ $NO_RESTART -eq 1 && $DRY_RUN -eq 0 ]] && info "not restarting (--no-restart)"
    return
  fi

  if systemctl list-unit-files signalk.service >/dev/null 2>&1; then
    log "Restarting signalk.service"
    sudo systemctl restart signalk.service
    sleep 3
    systemctl is-active --quiet signalk.service \
      && info "signalk is up: http://boat-pi.local:3000" \
      || warn "signalk did not come up - journalctl -u signalk -n 50"
  else
    warn "signalk.service not installed - run deploy/install.sh first"
  fi
}

main "$@"

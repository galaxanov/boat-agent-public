#!/usr/bin/env bash
#
# Install signal-cli, the transport for boat alerts.
#
# Two things make this more than "download and untar" on a Raspberry Pi:
#
#   1. signal-cli needs JRE 25.
#   2. The libsignal native library bundled in the release is x86_64 only. On
#      aarch64 it has to be replaced with a matching arm64 build, or every
#      command dies with UnsatisfiedLinkError.
#
# The libsignal version is NOT pinned here on purpose: it comes in transitively
# and changes between signal-cli releases. The script reads the version out of
# the jar it just extracted and fetches the arm64 build to match, so upgrading
# signal-cli does not silently install a mismatched library.
#
#   ./deploy/install-signal-cli.sh
#   ./deploy/install-signal-cli.sh --link      # link to your Signal account
#
set -euo pipefail

SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.7}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt}"
DEST="$INSTALL_ROOT/signal-cli-$SIGNAL_CLI_VERSION"
BIN_LINK=/usr/local/bin/signal-cli
DEVICE_NAME="${DEVICE_NAME:-boat-pi}"

RELEASES=https://github.com/AsamK/signal-cli/releases/download
LIBSIGNAL_RELEASES=https://github.com/exquo/signal-libs-build/releases/download

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: deploy/install-signal-cli.sh [--link] [--check]

  (no args)  install signal-cli and the right libsignal for this architecture
  --link     link to an existing Signal account as a secondary device
  --check    report what is installed and whether it works

Environment: SIGNAL_CLI_VERSION, INSTALL_ROOT, DEVICE_NAME
EOF
}

MODE=install
case "${1:-}" in
  --link)  MODE=link ;;
  --check) MODE=check ;;
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage; die "unknown option: $1" ;;
esac

# ------------------------------------------------------------------- java --

install_java() {
  local current=""
  if command -v java >/dev/null; then
    current="$(java -version 2>&1 | head -1)"
    local major
    major="$(java -version 2>&1 | sed -n 's/.*version "\([0-9]*\).*/\1/p' | head -1)"
    if [[ -n "$major" && "$major" -ge 25 ]]; then
      log "Java $major already installed ($current)"
      return
    fi
    warn "found $current, but signal-cli needs JRE 25 or newer"
  fi

  log "Installing JRE 25"
  sudo apt-get update -qq
  if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y openjdk-25-jre-headless 2>/dev/null; then
    info "installed from Debian"
    return
  fi

  # Older Raspberry Pi OS releases do not carry a JRE this new. Rather than
  # bolt a third-party apt repo onto the boat's Pi behind your back, stop and
  # let you decide.
  die "openjdk-25-jre-headless is not available from apt on this release.
     Install a JRE 25 yourself, e.g. Adoptium Temurin (has arm64 builds):
       https://adoptium.net/installation/linux/
     then re-run this script."
}

# -------------------------------------------------------------- signal-cli --

install_signal_cli() {
  if [[ -x "$DEST/bin/signal-cli" ]]; then
    log "signal-cli $SIGNAL_CLI_VERSION already unpacked at $DEST"
    return
  fi

  local tarball="signal-cli-$SIGNAL_CLI_VERSION.tar.gz"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  log "Downloading signal-cli $SIGNAL_CLI_VERSION"
  curl -fsSL --retry 3 -o "$tmp/$tarball" "$RELEASES/v$SIGNAL_CLI_VERSION/$tarball" \
    || die "download failed"
  info "sha256: $(sha256sum "$tmp/$tarball" | cut -d' ' -f1)"
  info "signature to check by hand if you want: $RELEASES/v$SIGNAL_CLI_VERSION/$tarball.asc"

  log "Installing to $DEST"
  sudo mkdir -p "$INSTALL_ROOT"
  sudo tar -xzf "$tmp/$tarball" -C "$INSTALL_ROOT"
  [[ -x "$DEST/bin/signal-cli" ]] || die "unexpected archive layout, no $DEST/bin/signal-cli"

  sudo ln -sfn "$DEST/bin/signal-cli" "$BIN_LINK"
  info "linked $BIN_LINK -> $DEST/bin/signal-cli"
}

# The bundled libsignal_jni.so is x86_64. On anything else, swap in a build for
# this architecture, matched to the exact libsignal version signal-cli expects.
fix_native_library() {
  local arch
  arch="$(uname -m)"
  if [[ "$arch" == "x86_64" ]]; then
    log "x86_64: the bundled libsignal is correct, nothing to do"
    return
  fi

  local target
  case "$arch" in
    aarch64|arm64) target="aarch64-unknown-linux-gnu" ;;
    armv7l)        target="armv7-unknown-linux-gnueabihf" ;;
    *)             die "no prebuilt libsignal for $arch - see
     https://github.com/AsamK/signal-cli/wiki/Provide-native-lib-for-libsignal" ;;
  esac

  local jar version
  jar="$(sudo find "$DEST/lib" -maxdepth 1 -name 'libsignal-client-*.jar' | head -1)"
  [[ -n "$jar" ]] || die "no libsignal-client jar in $DEST/lib"
  version="$(basename "$jar" .jar | sed 's/^libsignal-client-//')"
  log "Replacing libsignal for $arch (libsignal $version, from the bundled jar)"

  command -v zip >/dev/null || sudo apt-get install -y zip
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  local name="libsignal_jni.so-v$version-$target.tar.gz"
  local url="$LIBSIGNAL_RELEASES/libsignal_v$version/$name"
  if ! curl -fsSL --retry 3 -o "$tmp/$name" "$url"; then
    die "no prebuilt libsignal $version for $target at
       $url
     Either that version has no build yet, or the naming changed. Check
       https://github.com/exquo/signal-libs-build/releases
     and install the matching libsignal_jni.so into $jar by hand."
  fi

  tar -xzf "$tmp/$name" -C "$tmp"
  [[ -f "$tmp/libsignal_jni.so" ]] || die "archive did not contain libsignal_jni.so"

  # Replacing the .so inside the jar is the documented approach.
  sudo cp "$jar" "$jar.bak-x86_64"
  ( cd "$tmp" && sudo zip -q -j "$jar" libsignal_jni.so )
  info "patched $(basename "$jar") (original kept as $(basename "$jar").bak-x86_64)"
}

# -------------------------------------------------------------------- run --

check() {
  log "Checking the installation"
  command -v signal-cli >/dev/null || die "signal-cli is not on PATH"
  info "$(signal-cli --version 2>&1 | head -1)"
  info "java: $(java -version 2>&1 | head -1)"
  info "arch: $(uname -m)"

  # --version does not touch libsignal; listAccounts does, so it is the check
  # that actually proves the native library loads.
  if signal-cli listAccounts >/dev/null 2>&1; then
    info "libsignal loads correctly"
  else
    warn "signal-cli ran but listAccounts failed - if it mentions UnsatisfiedLinkError,"
    warn "the native library is still wrong for this architecture"
    return 1
  fi

  local accounts
  accounts="$(signal-cli listAccounts 2>/dev/null || true)"
  if [[ -z "$accounts" ]]; then
    warn "no account linked yet - run: ./deploy/install-signal-cli.sh --link"
  else
    info "linked account(s): $accounts"
  fi
}

link_account() {
  command -v signal-cli >/dev/null || die "install first, then --link"
  command -v qrencode >/dev/null || sudo apt-get install -y qrencode

  log "Linking as a secondary device to your existing Signal account"
  cat <<EOF

  On your phone: Signal -> Settings -> Linked devices -> +
  Then scan the QR code below.

  This links the Pi to your existing account. It does NOT need a separate
  phone number, and your account keeps working normally on your phone.

EOF
  # signal-cli prints the sgnl:// URI on stdout and then waits for the scan.
  signal-cli link -n "$DEVICE_NAME" | while read -r line; do
    if [[ "$line" == sgnl://* ]]; then
      qrencode -t ANSIUTF8 "$line"
      echo "  (or paste this URI: $line)"
    else
      echo "$line"
    fi
  done

  log "Linked. Check with: signal-cli listAccounts"
  cat <<'EOF'

  Now put the numbers in .env, in international format:
    SIGNAL_ACCOUNT=+1555...   the account you just linked
    SIGNAL_RECIPIENT=+1555...   where alerts should go (can be the same number,
                              or several separated by commas)

EOF
}

case "$MODE" in
  install)
    install_java
    install_signal_cli
    fix_native_library
    check || true
    log "Next: ./deploy/install-signal-cli.sh --link"
    ;;
  link)  link_account ;;
  check) check ;;
esac

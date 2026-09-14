#!/usr/bin/env bash
#
# Make `boat` a command, and stop the agent needing sudo.
#
#   ./deploy/install-command.sh
#
# Two things, both of which can be undone by deleting a file:
#
#   1. Symlinks bin/boat into ~/.local/bin, which is already on PATH on Ubuntu.
#      After this, `boat` from any directory opens the console, and `boat
#      --forecast` and friends work the same way.
#
#   2. Moves the agent from a systemd system unit to a user unit, so restarting
#      it after a code change is `systemctl --user restart boat-agent` with no
#      password, and the console can offer to do it for you.
#
# The second step needs sudo exactly once, to remove the old system unit and
# (if you want it) to let the user unit start before anybody logs in. That is
# the last sudo this ever asks for.
#
#   --no-service    just the command, leave the service alone
#   --no-linger     skip the boot-time question, and with it the sudo

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="${UNIT_DIR:-$HOME/.config/systemd/user}"
VENV="${VENV:-$REPO_DIR/.venv}"
UNIT="boat-agent.service"

DO_SERVICE=1 DO_LINGER=1
for arg in "$@"; do
  case "$arg" in
    --no-service) DO_SERVICE=0 ;;
    --no-linger) DO_LINGER=0 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# ------------------------------------------------------------------ command --

say "The command"
mkdir -p "$BIN_DIR"
ln -sf "$REPO_DIR/bin/boat" "$BIN_DIR/boat"
chmod +x "$REPO_DIR/bin/boat"
note "$BIN_DIR/boat -> $REPO_DIR/bin/boat"

case ":$PATH:" in
  *":$BIN_DIR:"*) note "$BIN_DIR is on PATH already" ;;
  *)
    note "$BIN_DIR is NOT on your PATH. Add it:"
    note "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    ;;
esac

if [ "$DO_SERVICE" -eq 0 ]; then
  say "Done"
  note "Try it:  boat"
  exit 0
fi

# ------------------------------------------------------------------ service --

say "The service"

if [ ! -x "$VENV/bin/python" ]; then
  note "no venv at $VENV - run ./deploy/install.sh --laptop first"
  exit 1
fi

# The old system unit has to go first. Two agents writing one logs/ directory
# would interleave their lines and fight over the anchor file, and the second
# one to start would find the status file already being rewritten by the first.
if systemctl list-unit-files --no-legend "$UNIT" 2>/dev/null | grep -q .; then
  note "removing the system unit (this is the sudo)"
  sudo systemctl disable --now "$UNIT" || true
  sudo rm -f "/etc/systemd/system/$UNIT"
  sudo systemctl daemon-reload
  note "gone"
else
  note "no system unit to remove"
fi

mkdir -p "$UNIT_DIR"
sed -e "s#@REPO_DIR@#$REPO_DIR#g" -e "s#@VENV@#$VENV#g" \
  "$SCRIPT_DIR/boat-agent.user.service" > "$UNIT_DIR/$UNIT"
note "wrote $UNIT_DIR/$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
note "enabled and started, with no password"

# ------------------------------------------------------------------- linger --

if [ "$DO_LINGER" -eq 1 ] && ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
  say "Starting at boot"
  note "A user service starts when you log in. On a boat you probably want it"
  note "running from the moment the laptop is powered, logged in or not."
  read -r -p "  Enable that now? It needs sudo once. [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    sudo loginctl enable-linger "$USER"
    note "on: the agent now starts at boot"
  else
    note "skipped. Turn it on later with: sudo loginctl enable-linger $USER"
  fi
fi

# --------------------------------------------------------------------- done --

say "Done"
note "boat                      the screen, and a letter for each thing you do"
note "boat --forecast           anything else the agent takes"
note "systemctl --user restart boat-agent    no password, ever again"
note ""
note "Try it:  boat"

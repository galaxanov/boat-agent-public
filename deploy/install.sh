#!/usr/bin/env bash
#
# Boat agent - Pi provisioning script.
#
# Installs Node LTS, Signal K server, the plugins we use, udev rules for the
# NGX-1, group membership, and the systemd units for Signal K and the agent.
#
# Idempotent: safe to re-run after a change or a fresh SD/NVMe image.
#
#   ssh pi@boat-pi.local
#   cd ~/boat-agent && ./deploy/install.sh
#
# Run as the normal login user (pi), NOT as root - the script sudos where it
# needs to and everything else must stay owned by that user.

set -euo pipefail

# ---------------------------------------------------------------- settings --

# Node 22 ("Jod") rather than the newest LTS: it is the line Signal K server 2.x
# is tested against and is in maintenance until April 2027. Override if needed.
NODE_MAJOR="${NODE_MAJOR:-22}"

BOAT_USER="${BOAT_USER:-$(id -un)}"
BOAT_HOME="$(getent passwd "$BOAT_USER" | cut -d: -f6)"
SIGNALK_HOME="${SIGNALK_HOME:-$BOAT_HOME/.signalk}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_VENV="$REPO_DIR/.venv"

# NGX-1 udev matching. Leave the serial empty to auto-detect when exactly one
# candidate USB serial adapter is plugged in; set it explicitly once known:
#   NGX1_SERIAL=XXXXXXXX ./deploy/install.sh
NGX1_SERIAL="${NGX1_SERIAL:-}"
NGX1_SYMLINK="${NGX1_SYMLINK:-ngx1}"

# A u-blox receiver reports no serial number of its own, so unlike the NGX-1
# it cannot be pinned by one. Vendor and product are enough while there is one
# receiver on the machine, which there is.
#
# Not gps0: gpsd's own udev rule claims gps%n for every receiver it recognises.
# Our name says which receiver it is, and it is what gpsd is pointed at below,
# so the daemon does not depend on enumeration order either.
GPS_VENDOR="${GPS_VENDOR:-1546}"
GPS_PRODUCT="${GPS_PRODUCT:-01a9}"
GPS_SYMLINK="${GPS_SYMLINK:-ublox}"

# 1-Wire data pin for the DS18B20 probes (locker + house battery).
ONEWIRE_GPIO="${ONEWIRE_GPIO:-4}"

APT_PACKAGES=(
  build-essential
  ca-certificates
  curl
  git
  gnupg
  rsync
  avahi-daemon        # boat-pi.local
  bluetooth
  bluez
  libbluetooth-dev    # noble, used by signalk-victron-ble
  libudev-dev
  python3
  python3-dev
  python3-venv
  jq
)

# Plugins we depend on. Anything in OPTIONAL_PLUGINS is best-effort: a rename or
# an unpublished package must not abort the whole install.
REQUIRED_PLUGINS=(
  signalk-victron-ble               # Victron MPPT over BLE Instant Readout
  signalk-raspberry-pi-monitoring   # CPU temp, disk, throttling
)
OPTIONAL_PLUGINS=(
  signalk-raspberry-pi-1wire        # DS18B20 locker + battery temps
  signalk-starlink                  # Starlink dish stats
  # The cockpit iPad gateway. An AIS on the N2K bus and an iPad that speaks
  # NMEA 0183, so the sentences have to be made: one plugin for other vessels'
  # AIS, one for our own position, depth, wind and heading. Both feed the
  # nmea-tcp interface on 10110, which is already on in settings.json.
  signalk-n2kais-to-nmea0183        # AIS targets from N2K -> AIVDM
  @signalk/signalk-to-nmea0183      # own ship: RMC, DPT, MWV, HDG
)
# NMEA 2000 via the NGX-1 needs no plugin: canboatjs ships inside signalk-server.

STEPS_RUN=()
NOTES=()

# ---------------------------------------------------------------- plumbing --

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }
note() { NOTES+=("$*"); }

usage() {
  cat <<'EOF'
Usage: deploy/install.sh [options]

Options:
  --skip-apt        Skip apt update/install
  --skip-node       Skip Node LTS install
  --skip-signalk    Skip Signal K server + plugin install
  --skip-plugins    Install Signal K server but not the plugins
  --skip-udev       Skip udev rules for the NGX-1
  --skip-agent      Skip Python venv + boat-agent.service
  --laptop          Install on the Ubuntu nav laptop instead of the Pi: no
                    1-wire, no Pi-only plugins, no clock wait
  --force           Run even if this does not look like a Raspberry Pi
  -h, --help        This

Environment overrides:
  NODE_MAJOR=22  BOAT_USER=pi  SIGNALK_HOME=~/.signalk
  NGX1_SERIAL=   NGX1_SYMLINK=ngx1  ONEWIRE_GPIO=4
EOF
}

SKIP_APT=0 SKIP_NODE=0 SKIP_SIGNALK=0 SKIP_PLUGINS=0 SKIP_UDEV=0 SKIP_AGENT=0 FORCE=0 LAPTOP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-apt)     SKIP_APT=1 ;;
    --skip-node)    SKIP_NODE=1 ;;
    --skip-signalk) SKIP_SIGNALK=1 ;;
    --skip-plugins) SKIP_PLUGINS=1 ;;
    --skip-udev)    SKIP_UDEV=1 ;;
    --skip-agent)   SKIP_AGENT=1 ;;
    --laptop)       LAPTOP=1 ;;
    --force)        FORCE=1 ;;
    -h|--help)      usage; exit 0 ;;
    *)              usage; die "unknown option: $1" ;;
  esac
  shift
done

preflight() {
  [[ $EUID -ne 0 ]] || die "run as the login user (e.g. pi), not root - sudo is used per-step"
  command -v sudo >/dev/null || die "sudo not found"
  [[ -n "$BOAT_HOME" && -d "$BOAT_HOME" ]] || die "no home directory for user $BOAT_USER"

  if [[ $LAPTOP -eq 1 ]]; then
    log "Laptop install: the agent will run on this machine, not on a Pi"
  elif ! grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; then
    if [[ $FORCE -eq 1 ]]; then
      warn "not a Raspberry Pi - continuing because --force was given"
    else
      die "this does not look like a Raspberry Pi. Run on the Pi, pass --laptop
     to install on the nav laptop, or --force to override this check"
    fi
  fi

  log "Provisioning for user '$BOAT_USER' (home $BOAT_HOME)"
  info "repo:      $REPO_DIR"
  info "signalk:   $SIGNALK_HOME"

  # Prime sudo once so the long steps do not stall on a password prompt.
  # `sudo -v` is deliberately NOT used: it ignores NOPASSWD and insists on a
  # password even when the user has passwordless sudo, which breaks any
  # non-interactive run (deploy over ssh, cron, first boot).
  if sudo -n true 2>/dev/null; then
    info "sudo: passwordless"
  elif [[ -t 0 ]]; then
    sudo true || die "sudo authentication failed"
  else
    die "sudo needs a password and there is no terminal to ask on.
     Run this from an interactive shell, or give $BOAT_USER passwordless sudo."
  fi
}

# ------------------------------------------------------------------- steps --

# A Pi has no real-time clock. If it boots before the network is up - which on
# the boat is the normal case, since Starlink takes longer to come up than the
# Pi does - its clock is left at whatever the image was built with. apt then
# rejects repository signatures as "not live until <date>", which reads like a
# broken mirror rather than a wrong clock. Wait for NTP before touching apt.
wait_for_clock() {
  if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]]; then
    log "Clock is NTP-synced ($(date))"
    return
  fi

  log "Waiting for the clock to sync (no RTC on a Pi; apt needs a correct date)"
  sudo timedatectl set-ntp true 2>/dev/null || true
  local waited=0
  while [[ $waited -lt 120 ]]; do
    if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]]; then
      info "synced: $(date)"
      STEPS_RUN+=("clock synced")
      return
    fi
    sleep 5
    waited=$((waited + 5))
  done

  warn "clock still not synced after ${waited}s (date is $(date))"
  note "The clock never synced. If apt fails with 'not live until <date>' that is
     why - the Pi thinks it is in the past. Check the network, then re-run."
}

install_apt_packages() {
  log "Installing apt packages"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PACKAGES[@]}"
  STEPS_RUN+=("apt packages")
}

install_node() {
  local have=""
  if command -v node >/dev/null; then
    have="$(node -v)"
  fi

  if [[ "$have" == v"$NODE_MAJOR".* ]]; then
    log "Node $have already installed"
    return
  fi

  if [[ -n "$have" ]]; then
    warn "replacing Node $have with the $NODE_MAJOR.x line"
  fi

  log "Installing Node ${NODE_MAJOR}.x LTS from NodeSource"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash -
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
  info "node $(node -v), npm $(npm -v)"
  STEPS_RUN+=("Node $(node -v)")
}

# noble (used by signalk-victron-ble) scans BLE with a raw socket. Without this
# Signal K would have to run as root to see the MPPT.
grant_node_ble_capability() {
  local node_bin
  node_bin="$(readlink -f "$(command -v node)")" || return 0
  log "Granting cap_net_raw to $node_bin (BLE scanning without root)"
  sudo setcap cap_net_raw+eip "$node_bin"
  STEPS_RUN+=("node cap_net_raw")
}

add_groups() {
  log "Adding $BOAT_USER to hardware groups"
  # audio, so the local alarm can still make a noise when nobody is logged
  # in. The desktop session grants access to /dev/snd by ACL and takes it away
  # again at logout, which is precisely the wrong time to go quiet.
  local groups=(dialout bluetooth gpio i2c spi video plugdev audio) added=()
  for g in "${groups[@]}"; do
    getent group "$g" >/dev/null || continue
    if id -nG "$BOAT_USER" | tr ' ' '\n' | grep -qx "$g"; then
      continue
    fi
    sudo usermod -aG "$g" "$BOAT_USER"
    added+=("$g")
  done

  if [[ ${#added[@]} -gt 0 ]]; then
    info "added: ${added[*]}"
    note "Group changes (${added[*]}) need a re-login or reboot to take effect."
    STEPS_RUN+=("groups: ${added[*]}")
  else
    info "already a member of all of them"
  fi
}

boot_config_path() {
  if [[ -f /boot/firmware/config.txt ]]; then
    echo /boot/firmware/config.txt
  elif [[ -f /boot/config.txt ]]; then
    echo /boot/config.txt
  fi
}

enable_onewire() {
  local cfg overlay="dtoverlay=w1-gpio,gpiopin=${ONEWIRE_GPIO}"
  cfg="$(boot_config_path)"
  if [[ -z "$cfg" ]]; then
    warn "no config.txt found - skipping 1-Wire overlay"
    return
  fi

  if grep -q '^dtoverlay=w1-gpio' "$cfg"; then
    log "1-Wire overlay already enabled in $cfg"
    return
  fi

  log "Enabling 1-Wire on GPIO${ONEWIRE_GPIO} in $cfg"
  printf '\n# DS18B20 probes (locker, house battery) - boat agent\n%s\n' "$overlay" \
    | sudo tee -a "$cfg" >/dev/null
  note "1-Wire overlay added: reboot before the DS18B20 probes appear in /sys/bus/w1."
  STEPS_RUN+=("1-Wire overlay")
}

# The NGX-1 must be in N2K transfer / PC-gateway mode (set from Actisense
# Toolkit on the laptop) or it will not pass raw N2K to canboatjs.
# Every USB serial adapter present, with enough detail for a person to tell
# them apart: device, ids, the strings it reports, and its serial.
detect_ngx1() {
  local dev vendor product serial model maker found=()
  for dev in /dev/ttyUSB* /dev/ttyACM*; do
    [[ -e "$dev" ]] || continue
    vendor="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_VENDOR_ID=//p')"
    product="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_MODEL_ID=//p')"
    serial="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_SERIAL_SHORT=//p')"
    model="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_MODEL=//p')"
    maker="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^ID_VENDOR=//p')"
    [[ -n "$serial" ]] || continue
    found+=("$dev $vendor:$product $serial ${maker//_/ } ${model//_/ }")
  done

  [[ ${#found[@]} -gt 0 ]] || return 1
  printf '%s\n' "${found[@]}"
}

# Does this adapter actually say it is an Actisense? A bare FTDI descriptor
# does not, and that is the whole point.
#
# Learned by getting it wrong: the first install of this ran with one unrelated
# FTDI cable plugged in, auto-detected it as "the only USB serial device", and
# pinned its serial as the NGX-1. Signal K then opened that cable and spoke
# Actisense at it for two days while reporting nothing amiss, because "no depth
# on the bus" and "no gateway at all" look identical from the far end. Plug the
# real NGX-1 in afterwards and it gets a different serial, so it would never
# have taken the name - silently, on a boat, at anchor.
#
# One adapter present is not evidence. Only the descriptor is.
looks_like_gateway() {
  local line="$1"
  shopt -s nocasematch
  [[ "$line" =~ actisense|ngx|ngt|nmea ]]
  local answer=$?
  shopt -u nocasematch
  return $answer
}

write_udev_rules() {
  log "Writing udev rules"
  local rules=/etc/udev/rules.d/98-boat-agent.rules
  local serial="$NGX1_SERIAL" candidates=()

  if [[ -z "$serial" ]]; then
    mapfile -t candidates < <(detect_ngx1 || true)
    local named=()
    local one
    for one in "${candidates[@]}"; do
      looks_like_gateway "$one" && named+=("$one")
    done

    if [[ ${#named[@]} -eq 1 ]]; then
      serial="$(awk '{print $3}' <<<"${named[0]}")"
      info "found a gateway that identifies itself: ${named[0]}"
    elif [[ ${#named[@]} -gt 1 ]]; then
      warn "more than one device says it is a gateway - pick one by serial:"
      printf '      %s\n' "${named[@]}" >&2
    elif [[ ${#candidates[@]} -gt 0 ]]; then
      warn "USB serial adapters are present, but none says it is an Actisense:"
      printf '      %s\n' "${candidates[@]}" >&2
      note "Not guessing. One adapter being present is not evidence that it is
     the NGX-1, and pinning the wrong one fails silently on the boat."
    else
      warn "no USB serial device found (NGX-1 not plugged in yet?)"
    fi
  fi

  if [[ -z "$serial" ]]; then
    note "NGX-1 udev rule NOT written. Plug the NGX-1 in and re-run:
       NGX1_SERIAL=<serial> ./deploy/install.sh --skip-apt --skip-node --skip-signalk
     Find the serial with: udevadm info -q property -n /dev/ttyUSB0 | grep SERIAL_SHORT"
    return
  fi

  if ! detect_ngx1 2>/dev/null | grep -q " $serial "; then
    warn "the serial being pinned ($serial) is not plugged in right now."
    note "That is fine if the NGX-1 is simply elsewhere. It is NOT fine if this
     serial came from some other adapter: check it against the label on the
     gateway before trusting the bus."
  fi

  sudo tee "$rules" >/dev/null <<EOF
# Boat agent - managed by deploy/install.sh, edits will be overwritten.
# Actisense NGX-1, pinned by serial so it survives re-enumeration and extra adapters.
SUBSYSTEM=="tty", ATTRS{serial}=="${serial}", SYMLINK+="${NGX1_SYMLINK}", GROUP="dialout", MODE="0660"
# u-blox GNSS receiver. No serial number to pin to, so vendor and product it is.
SUBSYSTEM=="tty", ATTRS{idVendor}=="${GPS_VENDOR}", ATTRS{idProduct}=="${GPS_PRODUCT}", SYMLINK+="${GPS_SYMLINK}", GROUP="dialout", MODE="0660"
EOF

  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=tty

  if [[ -e "/dev/$NGX1_SYMLINK" ]]; then
    info "/dev/$NGX1_SYMLINK -> $(readlink -f "/dev/$NGX1_SYMLINK")"
  else
    note "/dev/$NGX1_SYMLINK did not appear - unplug and replug the NGX-1."
  fi
  if [[ -e "/dev/$GPS_SYMLINK" ]]; then
    info "/dev/$GPS_SYMLINK -> $(readlink -f "/dev/$GPS_SYMLINK")"
  else
    note "/dev/$GPS_SYMLINK did not appear. If the u-blox is not on this machine
     that is expected: disable the 'ublox' provider in signalk/settings.json so
     Signal K is not left with a source that can only fail."
  fi
  STEPS_RUN+=("udev rules for /dev/$NGX1_SYMLINK and /dev/$GPS_SYMLINK")
}

# One receiver, several things that want it. A serial port has exactly one
# owner, so rather than fight gpsd for it - which is a race the boat loses at
# some point, silently, at night - gpsd is made the owner and everything else
# becomes a client of it: the agent through Signal K, OpenCPN on 2947, and
# whatever gets plugged in next.
#
# -n is the flag that matters. Without it gpsd opens the receiver only when a
# client connects, so every program that starts pays for a cold start before it
# has a position. With it the receiver tracks continuously and a fix is already
# there when something asks.
configure_gpsd() {
  if ! command -v gpsd >/dev/null; then
    note "gpsd is not installed, so nothing owns the GPS receiver and Signal K
     will open it directly. That works until something else wants it too:
       sudo apt install gpsd gpsd-clients && ./deploy/install.sh --skip-apt ..."
    return
  fi

  local conf=/etc/default/gpsd
  log "Pointing gpsd at /dev/$GPS_SYMLINK and keeping it tracking"

  if [[ -f "$conf" ]] && ! grep -q "boat agent" "$conf"; then
    sudo cp -p "$conf" "$conf.bak-$(date +%Y%m%d%H%M%S)"
    info "backed up the previous $conf"
  fi

  sudo tee "$conf" >/dev/null <<EOF
# Boat agent - managed by deploy/install.sh, edits will be overwritten.
# gpsd owns the receiver so that several programs can read it at once.
START_DAEMON="true"
USBAUTO="true"
DEVICES="/dev/${GPS_SYMLINK}"
# -n: poll the receiver without waiting for a client, so a fix is ready when
# OpenCPN or the agent asks instead of starting cold every time.
GPSD_OPTIONS="-n"
EOF

  sudo systemctl enable gpsd.socket gpsd.service >/dev/null 2>&1 || true
  sudo systemctl restart gpsd.socket gpsd.service || warn "gpsd did not restart"
  STEPS_RUN+=("gpsd owns /dev/$GPS_SYMLINK")

  note "OpenCPN. Point it at gpsd rather than the serial port: Options >
     Connections > Add, Network, protocol GPSD, address localhost, port 2947.
     Remove any connection that opens /dev/ttyACM0 or /dev/ublox directly, or
     the two will fight over the receiver and one of them will lose."
}

install_signalk_server() {
  if command -v signalk-server >/dev/null; then
    log "Signal K server present ($(signalk-server --version 2>/dev/null || echo version unknown)) - updating"
  else
    log "Installing Signal K server"
  fi
  sudo npm install -g --unsafe-perm signalk-server
  STEPS_RUN+=("signalk-server")
}

# Plugins live in ~/.signalk/node_modules, which is where the Signal K app store
# puts them too - installing by hand here keeps a fresh Pi reproducible.
# The Pi-only plugins read a Pi's own hardware: /sys thermal zones and the
# 1-wire bus on its GPIO header. On a laptop they install happily and then
# report nothing, which is worse than not being there - a path that never
# arrives looks the same as an instrument that has gone quiet.
drop_pi_plugins() {
  local kept=()
  for plugin in "$@"; do
    case "$plugin" in
      signalk-raspberry-pi-*) continue ;;
      *) kept+=("$plugin") ;;
    esac
  done
  printf '%s\n' "${kept[@]}"
}

install_signalk_plugins() {
  log "Installing Signal K plugins into $SIGNALK_HOME"
  mkdir -p "$SIGNALK_HOME"

  if [[ ! -f "$SIGNALK_HOME/package.json" ]]; then
    cat >"$SIGNALK_HOME/package.json" <<'EOF'
{
  "name": "signalk-server-config",
  "version": "1.0.0",
  "description": "Boat agent Signal K plugins",
  "private": true,
  "dependencies": {}
}
EOF
  fi

  local required=("${REQUIRED_PLUGINS[@]}") optional=("${OPTIONAL_PLUGINS[@]}")
  if [[ $LAPTOP -eq 1 ]]; then
    mapfile -t required < <(drop_pi_plugins "${REQUIRED_PLUGINS[@]}")
    mapfile -t optional < <(drop_pi_plugins "${OPTIONAL_PLUGINS[@]}")
    info "laptop install: skipping the Raspberry Pi hardware plugins"
  fi

  local pkg
  for pkg in "${required[@]}"; do
    info "required: $pkg"
    npm install --prefix "$SIGNALK_HOME" --save "$pkg" \
      || die "failed to install required plugin $pkg"
  done

  for pkg in "${optional[@]}"; do
    info "optional: $pkg"
    if ! npm install --prefix "$SIGNALK_HOME" --save "$pkg" 2>/dev/null; then
      warn "could not install $pkg"
      note "Optional plugin '$pkg' failed to install - check the current package name
     in the Signal K app store (Appstore > Available) and install it there."
    fi
  done

  STEPS_RUN+=("signalk plugins")
}

# The repo's Signal K config (NGX-1 provider, MPPT BLE, NMEA 0183 out). Needs
# .env for the Victron secrets, so it is skipped rather than fatal on a first
# run where .env has not been created yet.
apply_signalk_config() {
  local apply="$SCRIPT_DIR/apply-signalk-config.sh"
  [[ -x "$apply" ]] || { warn "no apply-signalk-config.sh - skipping Signal K config"; return; }

  if [[ ! -f "$REPO_DIR/.env" ]]; then
    info "no .env yet - skipping Signal K config"
    note "Create $REPO_DIR/.env, then: ./deploy/apply-signalk-config.sh"
    return
  fi

  log "Applying Signal K config from the repo"
  # install_signalk_service restarts the server right after this.
  SIGNALK_HOME="$SIGNALK_HOME" "$apply" --no-restart
  STEPS_RUN+=("signalk config")
}

install_signalk_service() {
  log "Installing signalk.service"
  local unit=/etc/systemd/system/signalk.service
  local bin
  bin="$(command -v signalk-server)"

  sudo tee "$unit" >/dev/null <<EOF
# Boat agent - managed by deploy/install.sh, edits will be overwritten.
[Unit]
Description=Signal K Server
Documentation=https://signalk.org/
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
Type=simple
User=${BOAT_USER}
WorkingDirectory=${SIGNALK_HOME}
Environment=EXTERNALPORT=3000
Environment=NODE_ENV=production
ExecStart=${bin} --securityenabled
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=signalk

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable signalk.service
  sudo systemctl restart signalk.service
  STEPS_RUN+=("signalk.service")
  note "First run: open http://boat-pi.local:3000 and create the admin login
     (security is enabled, so the server waits for it before the API is usable)."
}

install_agent_venv() {
  log "Setting up the Python venv for the agent"
  if [[ ! -d "$AGENT_VENV" ]]; then
    python3 -m venv "$AGENT_VENV"
  fi
  "$AGENT_VENV/bin/pip" install --quiet --upgrade pip

  if [[ -f "$REPO_DIR/requirements.txt" ]]; then
    "$AGENT_VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
    info "installed requirements.txt"
  else
    info "no requirements.txt yet - venv created empty"
  fi

  local py
  py="$("$AGENT_VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if [[ "$(printf '%s\n3.11\n' "$py" | sort -V | head -1)" != "3.11" ]]; then
    warn "Python $py is older than the 3.11 the agent targets"
  fi
  STEPS_RUN+=("agent venv (python $py)")
}

install_agent_service() {
  log "Installing boat-agent.service"
  local src="$SCRIPT_DIR/boat-agent.service"
  [[ -f "$src" ]] || die "missing $src"

  # signal-cli's account store. It rewrites session state on every send, so the
  # unit has to be able to write here or no alert ever leaves the boat.
  local signal_data="${XDG_DATA_HOME:-$BOAT_HOME/.local/share}/signal-cli"

  # The unit ships with placeholders so the same file works for any user/path.
  sed -e "s|@USER@|${BOAT_USER}|g" \
      -e "s|@REPO_DIR@|${REPO_DIR}|g" \
      -e "s|@VENV@|${AGENT_VENV}|g" \
      -e "s|@SIGNAL_DATA@|${signal_data}|g" \
      "$src" | sudo tee /etc/systemd/system/boat-agent.service >/dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable boat-agent.service

  if [[ -f "$REPO_DIR/agent/main.py" ]]; then
    sudo systemctl restart boat-agent.service
    info "boat-agent started"
  else
    info "agent/main.py does not exist yet - unit enabled but not started"
    note "Run 'sudo systemctl start boat-agent' once agent/main.py exists."
  fi

  if [[ ! -f "$REPO_DIR/.env" ]]; then
    note "No .env on the Pi yet: cp .env.example .env && chmod 600 .env, then fill in
     VICTRON_MPPT_MAC/KEY and SIGNAL_ACCOUNT/RECIPIENT."
  fi
  note "Alerts need signal-cli: ./deploy/install-signal-cli.sh, then --link.
     Until then the agent logs notifications instead of sending them.
     Link it as ${BOAT_USER}, not root, or its account store lands somewhere
     the agent cannot write and every send fails."

  STEPS_RUN+=("boat-agent.service")
}

# The things that make a laptop a worse monitoring machine than a Pi, and
# what to do about each. Notes rather than actions: changing when someone's
# laptop sleeps, or what its lid does, is not a decision an installer gets to
# make quietly.
laptop_notes() {
  note "The lid. systemd suspends on lid close by default, and a suspended
     laptop raises no alarms - the anchor drags in silence. To keep it awake
     with the lid shut:
       sudo sed -i 's/^#*HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
       sudo systemctl restart systemd-logind
     Reverse it by setting HandleLidSwitch=suspend."
  note "Sleep and idle. Check nothing else suspends the machine:
       systemctl status sleep.target suspend.target
       gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing"
  note "GPS. The agent needs a position and nothing else for the anchor watch,
     so the receiver is the one instrument worth leaving on overnight. gpsd
     owns it and hands it to everyone: Signal K is configured to ask gpsd, and
     OpenCPN should too. Check it with: gpspipe -w -n 5"
  note "Shore power. On battery this machine is the biggest load aboard after
     the fridge. Watch it for a night before trusting it on a long swing."
  note "Notifications. Every alert also lands on this machine's screen through
     notify-send, which needs a desktop session logged in. It is the weakest of
     the three channels and the only one that reaches whoever is already at the
     chart table. AGENT_DESKTOP_NOTIFY=0 turns it off."
  note "The alarm. This machine makes its own noise for anything at alarm level,
     which is the only part of the alerting that does not need Starlink. Hear
     it once before trusting it, with the volume where you sleep:
       .venv/bin/python -m agent.main --test-alarm
     Silence a standing alarm for half an hour with --hush, and end that early
     with --unhush. Both leave the Signal messages and the log alone."
}

summary() {
  echo
  log "Done"
  local s
  for s in "${STEPS_RUN[@]}"; do
    printf '    \033[1;32mok\033[0m  %s\n' "$s"
  done

  if [[ ${#NOTES[@]} -gt 0 ]]; then
    echo
    log "Still to do"
    local n
    for n in "${NOTES[@]}"; do
      printf '  - %s\n' "$n"
    done
  fi

  cat <<EOF

  Signal K admin : http://boat-pi.local:3000
  Signal K logs  : journalctl -u signalk -f
  Agent logs     : journalctl -u boat-agent -f
EOF
}

main() {
  preflight
  # A laptop has a real-time clock and a battery to keep it running, so it
  # never boots with a date apt will reject.
  [[ $LAPTOP -eq 1 ]] || wait_for_clock
  [[ $SKIP_APT     -eq 1 ]] || install_apt_packages
  [[ $SKIP_NODE    -eq 1 ]] || { install_node; grant_node_ble_capability; }
  add_groups
  [[ $LAPTOP -eq 1 ]] || enable_onewire
  if [[ $LAPTOP -eq 1 ]]; then laptop_notes; fi
  [[ $SKIP_UDEV    -eq 1 ]] || { write_udev_rules; configure_gpsd; }
  if [[ $SKIP_SIGNALK -eq 0 ]]; then
    install_signalk_server
    [[ $SKIP_PLUGINS -eq 1 ]] || install_signalk_plugins
    apply_signalk_config
    install_signalk_service
  fi
  if [[ $SKIP_AGENT -eq 0 ]]; then
    install_agent_venv
    install_agent_service
  fi
  summary
}

main "$@"

# Provisioning a Raspberry Pi for the boat agent

Everything here was learned the hard way on a Pi 3 Model B+ test rig running
Raspberry Pi OS Lite 64-bit (Debian 13 trixie, pi-gen 2026-06-18).
It applies to the Pi 5 too — the OS is the same, only the hardware differs.

Read this before imaging the Pi 5. Most of it is about mechanisms that *look*
like they work and silently don't.

## Image

**Raspberry Pi OS Lite, 64-bit.** Not Desktop, not 32-bit.

64-bit is not optional: signal-cli needs the `aarch64` build of `libsignal`, and
[install-signal-cli.sh](../deploy/install-signal-cli.sh) has no 32-bit path.

## What actually configures a fresh card

This is the part that cost an entire evening.

A stock Pi OS image ships **cloud-init templates that are never used**:
`user-data`, `meta-data` and `network-config` sit on the boot partition looking
authoritative. Writing a valid `#cloud-config` into `user-data` — hostname,
users, SSH keys, `ssh_pwauth` — does *nothing at all*, with no error and no log
entry. Do not trust them.

What does work, all on the boot partition (`/boot/firmware`):

| File | Effect | Consumed on first boot? |
|---|---|---|
| `ssh` (empty file) | Enables sshd | yes |
| `userconf.txt` | Creates the user: `name:$6$hash` | yes |
| `firstrun.sh` + a `cmdline.txt` hook | Runs a script **as root** at first boot | yes, if written to self-remove |
| `wpa_supplicant.conf` | **Nothing. Dead since Bookworm.** | never |

Generate the password hash with `openssl passwd -6` — it prompts twice and
prints the `$6$...` string that goes in `userconf.txt`.

### firstrun.sh — running something as root before you have a login

The only way to get root on a card you can't boot into. Append to `cmdline.txt`
(which must stay **one single line**):

```
systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
```

Two things to know:

- `systemd.unit=kernel-command-line.target` boots to a **minimal target with no
  networking and no sshd**. The Pi is unreachable for the whole of that pass. On
  a Pi 3 this takes several minutes, during which it looks completely dead.
  Don't conclude it has hung — wait for the reboot.
- The script must exit 0, or `systemd.run_success_action=reboot` never fires and
  the Pi sits in that minimal target forever. Use `set +e`, end with `exit 0`,
  and have the script strip its own hook out of `cmdline.txt` and delete itself.

`/boot/firmware` *is* mounted by the time it runs, so logging to
`/boot/firmware/firstrun.log` works and is worth doing — it is the only visibility
you get into that boot.

## WiFi on trixie

`wpa_supplicant.conf` on the boot partition is **ignored**. Pi OS uses
NetworkManager. Write a keyfile profile to
`/etc/NetworkManager/system-connections/<name>.nmconnection`, owned `root:root`,
mode `600` — NetworkManager silently refuses profiles with looser permissions.

```ini
[connection]
id=boat-wifi
type=wifi
interface-name=wlan0
autoconnect=true

[wifi]
mode=infrastructure
ssid=77;121;32;66;111;97;116;32;

[wifi-security]
key-mgmt=wpa-psk
psk=<64 hex chars>

[ipv4]
method=auto
[ipv6]
method=auto
```

**Encode the SSID as a decimal byte array**, not a string. An SSID with a
trailing space (the example above is `"My Boat "`) risks having it stripped by
the keyfile parser when written as a plain string, which then fails to
associate with no useful error. Generate the bytes with:

```bash
python3 -c "print(';'.join(str(b) for b in 'YOUR SSID'.encode()) + ';')"
```

Get the hashed PSK without putting the passphrase in a file:

```bash
wpa_passphrase "YOUR SSID" > /tmp/wpa.txt   # prompts on stdin, prints no prompt
```

It gives no prompt and looks like it has hung. That is normal — type the
passphrase and press enter. Use the `psk=` line, discard the `#psk=` comment
line, which is the plaintext.

## The clock, and why apt fails

**A Pi has no real-time clock.** If it boots before the network is up, its date
is whatever the image was built with — months in the past. apt then rejects
repository signatures with:

```
Sub-process /usr/bin/sqv returned an error code (1), error message is:
Verifying signature: Not live until 2026-01-01T12:00:00Z
```

That reads like a broken mirror. It is a wrong clock. On the boat this is the
**normal** case, because Starlink takes longer to come up than the Pi does.

`install.sh` now waits for NTP sync before touching apt. Manually:

```bash
sudo timedatectl set-ntp true
timedatectl show -p NTPSynchronized --value   # want "yes"
```

## sudo -v ignores NOPASSWD

`sudo -v` demands a password **even with `NOPASSWD: ALL`**, which breaks any
non-interactive run — deploy over ssh, cron, first boot. Use `sudo -n true` to
test for usable sudo. `install.sh` had this bug and it only surfaced when run
detached over ssh.

## Finding the Pi on the network

`.local` mDNS names may not resolve from the laptop (multicast DNS is not
enabled in its `systemd-resolved`), so `boat-pi.local` can fail even when
the Pi is up and healthy. Find it by MAC instead — Raspberry Pi OUIs:

```
b8:27:eb   Pi 3 and earlier
dc:a6:32 / e4:5f:01 / 28:cd:c1 / d8:3a:dd   Pi 4, Pi 5, CM4
```

Sweep and match:

```bash
for i in $(seq 1 254); do ping -c1 -W1 -n 192.168.1.$i >/dev/null 2>&1 & done; wait
ip neigh | grep -iE "b8:27:eb|dc:a6:32|e4:5f:01|28:cd:c1|d8:3a:dd"
```

### Direct laptop-to-Pi cable

Useful as a rescue path, with two traps:

- There is **no DHCP server** on a direct link, so the Pi gets no IPv4 address
  from your router and will not appear in any scan of the normal LAN.
- Discovery works over **IPv6 link-local**, which every Linux host answers:

```bash
ping6 -c3 -I <iface> ff02::1
ip -6 neigh show dev <iface> | grep -i b8:27:eb
ssh "pi@fe80::xxxx%<iface>"      # the %interface zone is required
```

If that returns nothing, check the interface has IPv6 at all —
`/proc/sys/net/ipv6/conf/<iface>/disable_ipv6`. A netplan/NetworkManager profile
with `ipv6.method: ignore` will disable it and the probe finds nothing, including
your own machine, which is the tell.

A direct cable gives the Pi **no internet**, so `install.sh` cannot run over it.
It is for rescue and configuration only.

## SSH stalling during key exchange

A modern client (OpenSSH 10) offers post-quantum `mlkem768x25519-sha256` first.
On a Pi 3's Cortex-A53 that key exchange is slow enough that connections stall
and time out during KEX, while TCP connects and the banner exchanges fine — so
it looks like a network fault when it is CPU cost. Ping is clean throughout,
which is the tell.

```bash
ssh -o KexAlgorithms=curve25519-sha256 pi@...
```

Worth remembering for the boat: the same symptom would appear over a slow or
high-latency Starlink link, and it is not a broken connection.

## LED codes on a Pi

- **Red solid** — power good. Red off/flickering means an inadequate supply. A Pi
  3 wants a genuine 2.5A, and the cable matters as much as the brick.
- **Green** — SD card activity. It should flicker hard for the first 30–60s.
  **Green completely dark = the card is never read.** Nothing on the card matters
  at that point; it is power, seating, the card, or the board.
- Once boot settles, green goes idle-dark. That is not a failure.

## Never pull a mounted card

Pulling the card while the host still has it mounted loses unflushed data.
The failure mode is nasty: files appear **with correct name, owner and mode but
zero length**. An empty `/etc/sudoers.d/` drop-in grants nothing and looks
correct in `ls -l`. Always:

```bash
sync && udisksctl unmount -b /dev/mmcblk0p1 && udisksctl unmount -b /dev/mmcblk0p2
```

and confirm before removing.

## Confirmed working on trixie / aarch64

Verified end to end on the Pi 3B+ test rig:

- **`openjdk-25-jre-headless` is in the trixie archive** (`25.0.4.1+1-1~deb13u1`,
  from `trixie-security`). signal-cli's JRE 25 requirement needs no third-party
  repo, no Adoptium, no manual install.
- **The arm64 `libsignal` swap works.** `install-signal-cli.sh` read the required
  version out of the jar it had just unpacked (`libsignal-client-0.99.1.jar`),
  fetched the matching `aarch64-unknown-linux-gnu` build, and patched it in.
  `signal-cli listAccounts` then ran clean — which is the real test, since
  `--version` never touches the native library and passes even when it is wrong.
- Reading the version from the jar rather than hardcoding it matters: libsignal
  comes in transitively via `com.github.turasa:signal-network` and is not pinned
  anywhere you can look it up, so a hardcoded version would silently rot on the
  next signal-cli release.
- `install.sh` completes on a Pi 3B+ in roughly fifteen minutes and both
  `signalk.service` and `boat-agent.service` come up. The agent connects to
  Signal K 2.31.1, subscribes to 32 paths, and runs with `NRestarts=0`.
- **Signal K allows readonly websocket access with no token**, so `SIGNALK_TOKEN`
  is not needed for the agent.

Still untested, both needing hardware or a phone: the NGX-1 udev rule, the
Victron MPPT BLE connection, and `install-signal-cli.sh --link`.

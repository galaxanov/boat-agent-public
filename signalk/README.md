# Signal K configuration

The repo is the source of truth for Signal K's config. `deploy/apply-signalk-config.sh`
renders these files (filling `@PLACEHOLDER@` tokens from `.env`) and installs them into
`~/.signalk` on the Pi.

```bash
./deploy/apply-signalk-config.sh --dry-run
```

**Changes made in the Signal K admin UI are overwritten by the next apply.** If you
change something in the UI and want to keep it, copy it back into this directory. The
script backs up whatever it replaces as `<file>.bak-<timestamp>` in `~/.signalk`.

## Files

| File | Installed to | What it does |
|---|---|---|
| `settings.json` | `~/.signalk/settings.json` | NGX-1 N2K provider, NMEA 0183 TCP output |
| `baseDeltas.json` | `~/.signalk/baseDeltas.json` | Vessel identity and dimensions |
| `plugin-config-data/signalk-victron-ble.json` | same path under `~/.signalk` | Victron MPPT over BLE |
| `plugin-config-data/sk-to-nmea0183.json` | same path under `~/.signalk` | Signal K → NMEA 0183 for OpenCPN |

`$comment` keys are stripped on install, so they document the repo copy without ending
up in the live config. Secrets are never in the repo: `signalk-victron-ble.json` holds
`@VICTRON_MPPT_MAC@` / `@VICTRON_MPPT_KEY@` and is installed mode 600.

## NMEA 2000 — Actisense NGX-1

`settings.json` defines one piped provider, `ngx1`:

```
type: NMEA2000  →  subOptions.type: ngt-1-canboatjs, device /dev/ngx1, 115200 baud
```

`ngt-1-canboatjs` is right for the NGX-1: it speaks the Actisense binary protocol, and
canboatjs is bundled inside signalk-server, so no plugin is needed. `/dev/ngx1` is the
udev symlink pinned by serial number in `/etc/udev/rules.d/98-boat-agent.rules`.

The gateway **must be in N2K transfer / PC-gateway mode**, set once from Actisense
Toolkit on the laptop. In converter mode it does not pass raw N2K and this provider
sits there connected but silent — no error, just no data.

To capture raw N2K for the pytest fixtures, flip `"logging": false` to `true` in the
provider; the server writes to `~/.signalk/logs`. Turn it back off afterwards, it is
not something to leave running on the NVMe.

## Victron MPPT over BLE

`signalk-victron-ble` reads Instant Readout advertisements — no pairing, no VE.Direct
cable. The MAC and the 32-hex advertisement key come from VictronConnect
(device → ⚙ → Product info → Instant readout via Bluetooth), and live in `.env`.

The configured device `id` becomes the Signal K path segment, so `"id": "mppt"` gives
exactly the paths CLAUDE.md expects:

```
electrical.solar.mppt.panelPower      W
electrical.solar.mppt.yieldToday      J
electrical.solar.mppt.chargingMode
electrical.solar.mppt.voltage         V   battery-side voltage
electrical.solar.mppt.current         A
```

**Gap worth knowing about:** CLAUDE.md lists `electrical.batteries.house.voltage` and
`.current` as coming "from MPPT", but this plugin publishes them under
`electrical.solar.mppt.*`, not under `electrical.batteries.house.*`. Nothing writes
`electrical.batteries.house.*` without a battery monitor such as a SmartShunt. The agent should read
the MPPT paths and treat them as a rough house-bank proxy — and it is only a proxy:
the MPPT reports what it sees at its own terminals while charging, which reads high
under load-free sun and tells you nothing at night when the panels are asleep.

BLE notes: the plugin needs `python3-venv` (installed by `deploy/install.sh`), and node
needs `cap_net_raw` to scan without root (also handled). Pi 5 shares one radio between
WiFi and Bluetooth — if readings get patchy while WiFi is busy, a USB BLE dongle is
the fix.

Older Victron battery monitors without VE.Direct or Instant Readout cannot be read this
way.

## NMEA 0183 out for OpenCPN

`"interfaces": { "nmea-tcp": true }` turns on the server's NMEA 0183 TCP listener on
port **10110** (fixed unless `NMEA0183PORT` is set in the environment). It broadcasts
whatever lands on the `nmea0183out` bus, which is filled by the bundled
`@signalk/signalk-to-nmea0183` plugin — that is what `sk-to-nmea0183.json` configures.

Sentences enabled: `RMC` `VTG` `DPT` `DBT` `MWVR` `MWVT` `MWD` `HDM` `VHW` `VLW` `MTW`
`ZDA`, each throttled so a phone on the far end of the boat is not fed 10 Hz updates.
`DBT` is there alongside `DPT` only because some older apps ignore `DPT`.

In OpenCPN: Options → Connections → Add → Network → TCP,
`boat-pi.local` port `10110`.

**Two GPS sources:** with a u-blox on USB and an AIS on the N2K bus, `RMC` here carries
position from the AIS's internal GPS while OpenCPN may also read the u-blox. Feeding both into
OpenCPN means it picks between them by priority. If that turns into fights over
position, either drop `RMC`/`VTG` from the conversions list and keep this feed for
depth/wind/log only, or set the u-blox to a higher priority in OpenCPN.

## Fill in for your boat

- `baseDeltas.json` ships with placeholder name, length, draft and displacement. Set your
  own. `environment.depth.transducerToKeel` defaults to the full draft, which assumes the
  transducer is at the waterline, so it understates clearance and alarms early. Measure
  how far below the waterline the transducer sits and set the offset to
  `draft − that` — see the depth section in the top-level README.
- Beam and air height are left out: do not guess at numbers that end up in an alarm.
- Add `{"path": "mmsi", "value": "..."}` to the empty-path value object once you know it;
  without one the vessel is identified by its UUID. Generate a fresh UUID for your boat.

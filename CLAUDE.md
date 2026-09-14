# Boat Agent

Monitoring and status agent for a sailing boat. Reads boat data from Signal K,
maintains a state model, raises alerts over Signal and on this machine's own
screen and speakers, fetches a forecast, and writes a ship's log. It is built to
run on a Linux laptop aboard (Ubuntu), in the repo, under systemd, or on a
Raspberry Pi next to the Signal K server.

Read [README.md](README.md) before changing behaviour: it holds the reasoning
for the thresholds, the severity ladder, and every design decision that is not
obvious from the code.

## Hardware it is written for
Nothing here is required; every rule stays silent when its instrument is absent.
- NMEA 2000 backbone read through an Actisense NGX-1 (or NGT-1) USB gateway, in N2K
  transfer / PC gateway mode, 115200 baud, pinned to /dev/ngx1 by a udev rule
- Depth, speed-through-water, log, wind and heading instruments on the N2K bus
- An AIS transponder on the bus, for position and for other vessels
- A USB GPS (u-blox) owned by gpsd on /dev/ublox, serving every client on 2947:
  Signal K, OpenCPN, anything else. gpsd runs with -n so the receiver tracks
  continuously and a fix is ready before anything asks for one
- A Victron MPPT solar charger read over Bluetooth LE Instant Readout. Encryption key
  and MAC from VictronConnect, stored in .env
- Optional on a Pi: DS18B20 probes (locker, house battery) on GPIO 1-Wire; bilge
  float/pump sense on GPIO; Pi CPU/disk monitoring
- Optional: a Starlink dish, for link state
- Machine: Ubuntu laptop (`deploy/install.sh --laptop`) or Raspberry Pi 5 running
  Raspberry Pi OS Lite 64-bit (`deploy/install.sh`), hostname `boat-pi` by default

## Services on the machine that runs it
Localhost when the agent runs beside Signal K; substitute boat-pi.local from elsewhere.
- Signal K server: http://localhost:3000 (admin UI)
- WebSocket stream: ws://localhost:3000/signalk/v1/stream
- REST: http://localhost:3000/signalk/v1/api/vessels/self
- NMEA 0183 out: tcp://<host>:10110 - a tablet nav app and OpenCPN can read this
- gpsd: localhost:2947, owns /dev/ublox, serves every client
- Agent: systemd unit `boat-agent.service`, logs via `journalctl -u boat-agent`
- Signal K needs a read token unless security is off: `--request-access`
- Optional, not installed: InfluxDB + Grafana for history

## Signal K connections
Configured in signalk/ and signalk/plugin-config-data/:
- NMEA 2000: canboatjs, Actisense NGX-1, /dev/ngx1. No plugin needed, it ships in the server
- u-blox GPS: Signal K's native gpsd stream (NMEA0183 over localhost:2947), never the
  serial port directly - a serial port has one owner and several things want this one
- Victron MPPT: signalk-victron-ble (Instant Readout)
- Tablet over WiFi: signalk-n2kais-to-nmea0183 (AIS targets) + @signalk/signalk-to-nmea0183
  (own ship) out to the nmea-tcp interface on 10110

Installed best-effort by deploy/install.sh:
- Starlink: signalk-starlink
- Pi health: signalk-raspberry-pi-monitoring (CPU temp, disk) - Pi only
- GPIO sensors: signalk-raspberry-pi-1wire (DS18B20) - Pi only

## Signal K paths that matter
- navigation.position, navigation.speedOverGround, navigation.courseOverGroundTrue
- navigation.headingMagnetic, navigation.speedThroughWater, navigation.log
- navigation.state (agent-derived: moored / anchored / underway-sail / underway-motor)
- environment.depth.belowTransducer, environment.water.temperature
- environment.wind.speedApparent, environment.wind.angleApparent, environment.wind.speedTrue, environment.wind.angleTrueWater
- steering.autopilot.state, steering.autopilot.target.headingMagnetic
- electrical.batteries.house.voltage, .current (from MPPT)
- electrical.solar.mppt.panelPower, .yieldToday, .chargingMode
- electrical.batteries.house.capacity.stateOfCharge (needs a battery monitor such as a SmartShunt)
- environment.inside.locker.temperature, electrical.batteries.house.temperature (DS18B20)
- notifications.bilge (GPIO)
- communication.starlink.* (link state, obstruction, uptime)
- environment.rpi.cpu.temperature, environment.rpi.disk.free
- environment.forecast.* (agent-derived from Open-Meteo; the spec defines no forecast
  paths): wind.speed, wind.gust, wind.directionTrue, wind.speedMax, wind.gustMax,
  pressure, airTemperature, waves.significantHeight, waves.maxHeight
All Signal K values are SI: m, m/s, radians, Kelvin, ratios 0-1, W, A, V. Convert for display only.

## Agent design
- Python 3.11+, asyncio, subscribes to the Signal K WebSocket with delta subscription for the paths above
- state.py: rolling state model, latest value per path with its age. A plain value store
  and nothing more - the transition detection lives in derived.py, NOT here, because
  folding heuristics into the store makes both harder to test
- derived.py: what the boat is doing (anchored / stopped / underway-sail / underway-motor),
  each conclusion carrying a confidence and the reason it was reached. Motoring is only
  claimed when sailing is physically impossible; engine_running is None, never False
- rules.py: twelve rules, thresholds at the top of the file so they can be argued with in
  one place. The README's Rules table gives the numbers and the debounces:
  - `anchor_drag` - position leaves the set circle plus a 10 m margin. ALARM
  - `anchor_watch_blind` - anchor set but no usable position, from a silent GPS or a
    no-fix 0,0. Warn at 2 min, ALARM at 15. A watch you believe in but do not have is the
    worst case this project knows about, so the drag rule is itself watched
  - `shallow_water` - clearance UNDER THE KEEL, not below the transducer, and only above
    about 1 kn. The transducer offset must be measured; the default alarms early
  - `house_voltage` - from the MPPT, a rough proxy without a battery monitor
  - `battery_temperature` - LiFePO4 range, and never charge below freezing
  - `wind_forecast` - F6, or gusts over 33 kn, inside 12 h; 18 h once the anchor is set,
    because at anchor the question is the whole night. Capped below alarm level on
    purpose: it reaches a phone and a screen but never sounds the siren
  - `bus_silent` - Signal K is delivering something but no N2K instrument has said
    anything for 15 min. Every other rule goes quiet with its own instrument; if they all
    go quiet at once the boat is disconnected, not calm. Found by a udev rule that pinned
    the wrong USB adapter as the gateway and failed silently for two days
  - `locker_temperature`, `bilge_cycling`, `starlink_down`, `cpu_temperature`,
    `disk_free` - wired and tested, and silent until something publishes their paths
- notify.py: alerts over Signal, via signal-cli linked as a secondary device. Every
  message goes through a persistent outbox first, so an alarm raised during a link
  outage still arrives, marked with how late it is. Not Telegram, not Pushover
- desktop.py: every alert on the screen of the machine the agent runs on, via
  notify-send. Alarms stay up until dismissed; no outbox, because a notification
  that arrives late reads as now
- sound.py: the alarm the machine makes itself, over the speakers, for alarm level and
  above. The only part of the alerting that does not depend on an internet link. Repeats
  until the alert clears; --hush buys a quiet period with an expiry on it, speaker only
- silence.py: the off switch, and it is not a bigger hush. --silence stops the speaker,
  Signal and the screen TOGETHER and does NOT expire, because it is granted for a
  situation (hauled out, alongside, laid up) rather than in the middle of an event. The
  price of not expiring is that it nags: journal twice an hour, a red banner on the
  console above everything including "the agent is not running", a banner on the page,
  the first line of every Signal status reply, a failing --drill, and a line in the
  ship's log. She keeps watching and keeps her log throughout; only the three ways out
  are shut, in one place in rules_loop and nowhere else. Lifting it replays whatever is
  still standing through Signal and the screen, because those two only ever see
  transitions and the transition happened while nobody was listening. Cannot be set by
  text and cannot be set by env var - both are off switches nobody can see
- weather.py: forecast for the boat's own position from Open-Meteo, once an hour, stdlib
  only. Fed into the state model as environment.forecast.* deltas the way anchor.py does,
  so both snapshots and the ship's log see it. Not on the alarm path: no link
  means no forecast and the rule goes quiet. See the module docstring for why
  Open-Meteo. No tides yet
- console.py: `--console`, the one command. Draws logs/status.json and takes a letter.
  Reads a file rather than the bus, so it opens instantly, competes with nothing, and
  works over an SSH link too poor for a websocket; the age of the picture is always on
  the screen. Its actions go through ui.Actions, so there is one mechanism and not two.
  The one thing it reads from a different file is the silence, straight from
  logs/silence.json, so the banner is right on the screen that is up when the agent is
  NOT running, and appears the instant the key is pressed rather than a tick later
- ui.py + ui.html: build_payload (shared with the console, published to status.json every
  tick) and a page for the phone, served by the agent itself. Built around one question -
  is the boat where I left it - so the hero is a swing plot, not a figure: anchor at the
  centre, watch circle, the drag rule's 10 m margin, and the last half hour of track
  fading old to new. A boat lying quietly traces an arc; one that has dragged traces a
  line. Wind is a 24 h trace with the F6 line marked, the dark hours shaded and
  the sea riding along the bottom on its own scale; wave
  height is given with its period, because two metres at five seconds and at nine are
  different nights. The readout carries the furthest the boat has been since the hook
  went down, and how long the dark has left to run. Positions
  in degrees and decimal minutes. Three lights the crew picks: chart paper, instrument,
  red - the console, the ship and HAL. Alte Haas Grotesk, subset and embedded as woff2 in
  agent/fonts/, because at anchor there is often no internet; it has tabular figures so it
  carries the readouts too. Rebuild it with fonttools if the character set ever grows.
  Deliberately NO scanlines, glow, bevels or gradients: the film is austere and the
  restraint is the whole difference between it and pastiche. Off by default,
  localhost when on, token required before binding it to the boat's WiFi. Three rules:
  the file is still the interface (a tap writes logs/anchor.json, byte for byte what
  --anchor-down writes, so there is one mechanism and not two); it runs on its own thread
  and reads only a finished dict the agent publishes each tick, so it can never slow an
  alarm down; and it can never put a value on the bus
- inbox.py: the other direction. Text the crew group and the boat answers - status,
  weather, anchor, hush. Polls signal-cli once a minute rather than running a daemon, and
  answers from logs/status.json so it can never slow a rule down. Reading only: it will
  not arm or weigh an anchor by text. NOTE the owner's own messages arrive as
  syncMessage.sentMessage, not dataMessage, because signal-cli is a linked secondary
  device - dropping syncs drops the skipper and nobody else. Own replies are excluded by
  requiring a recognised command word on syncs, which no reply has - so BOAT_NAME must
  not be a command word, since every reply starts with it
- digest.py: the ship's log. One markdown entry a day, newest first, built by reading
  back the last 24 h of the daily log rather than watching instruments, so every figure
  can be checked against the lines it came from
- logbook.py / anchor.py / access.py / geo.py / paths.py / units.py / signalk.py /
  config.py: daily JSON-lines log with rotation, the anchor file the crew writes, the
  Signal K token request, distance maths, the subscription list, SI-to-human conversion
  for display only, the WebSocket client with backoff, and the env-var config
- Daily log: logs/YYYY-MM-DD.jsonl, one JSON object per line, UTC, SI throughout.
  Types: snapshot, alert, state, event, forecast, ships_log. The `silenced`
  event is written on every nag rather than only when the switch is thrown, so a silence
  that outlasts a 24 h window still leaves a mark inside it and digest.py can measure it

## Repo layout
- agent/            the Python agent, one concern per module (see Agent design)
- tests/            pytest; tests/fixtures/ holds recorded Signal K frames, malformed ones included
- signalk/          exported Signal K settings and plugin config, applied by deploy/apply-signalk-config.sh
- deploy/           install.sh, install-signal-cli.sh, boat-agent.service, udev rules
- docs/             provisioning notes
- logs/             written at runtime, gitignored: daily logs, ships-log.md, anchor.json, outbox.json, hush.json, silence.json

## Using it

Everything runs from the repo on the machine the agent is on. `.venv/bin/python` is the
interpreter; there is no separate install step once `deploy/install.sh --laptop` has run.

First time, in this order. `.env` comes first: install.sh fills the MPPT secrets into
the Signal K plugin config from it, and silently skips that step if the file is not
there yet. Set your boat's name, dimensions and draft in `signalk/baseDeltas.json` and
`BOAT_NAME` in `.env`.
```bash
cp .env.example .env && chmod 600 .env  # then edit: SIGNAL_RECIPIENT at minimum
./deploy/install.sh --laptop            # Node, Signal K, plugins, config, venv, systemd
./deploy/install-signal-cli.sh --link   # scan the QR with the phone's Signal app
.venv/bin/python -m agent.main --request-access   # Signal K token, approve in its UI
.venv/bin/python -m agent.main --test-alarm       # hear it before trusting it
.venv/bin/python -m agent.main --drill            # test every channel end to end
./deploy/install-command.sh             # `boat` on PATH, user service, last sudo
```
If `.env` did not exist when install.sh ran, apply the Signal K config by hand
afterwards: `./deploy/apply-signalk-config.sh`. That script is also how any change under
`signalk/` reaches the server - the repo is the source of truth, and anything edited in
the Signal K admin UI is overwritten by it.

**Day to day there is one command.** `./deploy/install-command.sh` puts `boat` in
~/.local/bin and moves the agent to a systemd USER unit, after which nothing about
running this boat needs sudo again - restarting included. Then `boat` draws what the agent can see and waits for a letter: `a` anchor down, `u`
anchor up, `h` hush the speaker half an hour, `s` let it sound again, `w` the hourly
forecast, `z` silence every alert (or turn them back on - the label says which, and
arming it asks for a y), `x` restart the agent, `r` redraw, `q` quit. It reads `logs/status.json`,
which the agent rewrites
every rule tick, so it opens instantly and needs no connection of its own. If the agent
is not running there is no file and it says so.

The flags below still exist and are what the console calls underneath. Reach for them in
scripts, over a bad SSH link, or when the answer is not one of the letters.
```bash
journalctl -u boat-agent -f                       # what it is doing
systemctl --user restart boat-agent               # after editing .env or the code
cat logs/ships-log.md                             # last night, and the nights before
.venv/bin/python -m agent.main --forecast         # the forecast, hour by hour
.venv/bin/python -m agent.main --drill            # prove the alarms reach somebody
.venv/bin/python -m agent.main --anchor-down --radius 40   # arm here, check tonight
.venv/bin/python -m agent.main --anchor-down --at 36.83,10.30
.venv/bin/python -m agent.main --anchor-up                 # hook is aboard
.venv/bin/python -m agent.main --hush 30    # speaker only, 30 min, always expires
.venv/bin/python -m agent.main --unhush     # end it early
.venv/bin/python -m agent.main --silence "hauled out"   # ALL channels, no expiry
.venv/bin/python -m agent.main --unsilence  # everything back on
```
The watch lives in `logs/anchor.json` and survives a reboot, deliberately. The running
agent picks the file up within one rule tick; nothing needs restarting. A hush silences
the speaker and nothing else: Signal messages, the screen and the log all carry on.

A silence is the other switch and it is wider: `logs/silence.json`, no expiry, and it
stops the speaker, Signal and the screen together. She keeps watching and keeps her log
the whole time - only the ways out are shut - and the agent nags about it everywhere a
person looks until `--unsilence`. Lifting it re-announces anything still standing. It
cannot be set by text from the crew group or by an env var; both would be off switches
nobody can see. See "Silencing her" in the README for the reasoning.

There is also a page for a phone, off by default. Set `AGENT_UI=1`, and to reach it from
the cockpit also set `AGENT_UI_BIND=0.0.0.0` and `AGENT_UI_TOKEN`, then open
`http://<the boat's laptop>:8375/?t=<token>`. Same actions, same files underneath.

Checking and debugging:
```bash
.venv/bin/python -m agent.main --once        # connect, collect 10s, print, exit.
                                             # Non-zero if nothing arrived: a link test
.venv/bin/python -m agent.main --discover    # subscribe to every path, report what the
                                             # bus actually publishes
.venv/bin/python -m agent.main --ships-log   # write today's entry now, from logs on disk
```

Developing, on any machine with a copy of the repo:
```bash
python3 -m venv ~/.venvs/boat-agent         # NOT .venv in the repo if the repo is on a
~/.venvs/boat-agent/bin/pip install -r requirements-dev.txt   # shared drive: a venv
~/.venvs/boat-agent/bin/pytest && ~/.venvs/boat-agent/bin/ruff check .   # is per-machine
SIGNALK_HOST=boat-pi.local .venv/bin/python -m agent.main --once   # point at the boat
```

## Deploy
- On a laptop the agent runs where the repo is: no rsync, no ssh.
  `./deploy/install.sh --laptop`, then `systemctl --user restart boat-agent`
- **It is a systemd USER unit**, so restarting needs no password. `deploy/install-command.sh`
  sets that up and puts `boat` on PATH
- Keep the working copy on a local disk. An SMB share cannot do chmod (which makes a
  saved Signal K token look like a failure) and when the mount drops it takes the shell's
  working directory with it
- On a Pi: `ssh pi@boat-pi.local` (key auth), and deploy with
  `rsync -av --exclude .git --exclude .env ./ pi@boat-pi.local:~/boat-agent/ && ssh pi@boat-pi.local 'sudo systemctl restart boat-agent'`

## Conventions
- Type hints, ruff for lint (line length 100), pytest for rules with recorded Signal K samples
- Secrets in `.env` on the machine that runs it, never committed: SIGNAL_ACCOUNT,
  SIGNAL_RECIPIENT, SIGNALK_TOKEN, VICTRON_MPPT_MAC, VICTRON_MPPT_KEY.
  `.env.example` documents every variable and why it exists
- Times are UTC everywhere they are stored and local everywhere they are shown, converted
  in units.py on the way out and never without the zone name. AGENT_TIMEZONE overrides the
  machine's zone. Never write a local time into the logbook, the anchor file or the ship's
  log figures - a boat crosses zones and a log in local time cannot be compared with itself
- Nothing outside display code converts out of SI. Values enter and are stored as the bus
  sends them: m, m/s, radians, Kelvin, Pa, ratios 0-1
- Missing data is never a reading. Every rule checks staleness and stays silent rather
  than treating absence as zero
- Severity ladder: normal < alert < warn < alarm < emergency. Signal and the screen fire
  at `alert`; the speaker fires at `alarm` and nothing below it. Adding an alarm-level
  rule means committing to waking somebody at 0300 - be sure it is worth it
- Agent must survive Signal K restarts, serial drops, WiFi drops: reconnect with backoff
- Never send write/control commands to the N2K bus without an explicit confirm flag
- Prefer existing Signal K plugins over custom parsing
- Keep it usable offline: no feature should hard-fail without internet
- Test data never carries real phone numbers, names, positions or vessel ids

## Known limitations
- No engine data without an engine gateway (e.g. Actisense EMU-1): motoring is inferred
- No real state of charge without a battery monitor; the MPPT voltage is a rough proxy
- Depth: the rule assumes the transducer sits at the waterline until
  environment.depth.transducerToKeel is set in signalk/baseDeltas.json, so it understates
  clearance and alarms early. Check the depth instrument's own offset first: if it
  already shows depth below keel, the agent subtracts twice
- AIS neighbour watch is not built: the state model drops every vessel that is not us
- Passage logging is not built: departures, arrivals and distances are not yet written
  into the ship's log

## Gotchas
- Add the agent's user to the `dialout` and `bluetooth` groups
- Pin /dev/ttyUSB names with udev rules by serial number, and NEVER accept an
  auto-detected one. install.sh refuses to pin an adapter whose USB descriptor does
  not say Actisense: one adapter being present is not evidence that it is the gateway,
  and pinning the wrong serial fails silently at anchor
- NGX-1 in converter mode will NOT pass raw N2K; must be transfer mode (set in Actisense Toolkit first)
- Pi 5 NVMe: may need PCIE_PROBE=1 / pciex1 in config.txt depending on HAT
- Reserve the machine's IP in the router so tablets and SSH keep finding it
- Onboard Bluetooth and WiFi share the radio; if BLE readings drop, add a USB BLE dongle
- signal-cli is a linked device: the owner's messages arrive as syncMessage, not
  dataMessage. Test against captured envelopes, never invented ones

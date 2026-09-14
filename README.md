# Boat agent

Monitoring and status agent for a sailing boat. See [CLAUDE.md](CLAUDE.md) for the
hardware it expects and the design; this file is just how to run it.

## Layout

```
agent/       the Python agent
signalk/     Signal K server config (see signalk/README.md)
deploy/      install.sh, apply-signalk-config.sh, systemd units
tests/       pytest, with recorded Signal K frames in tests/fixtures/
docs/        wiring notes, photos, path reference
```

## On the Pi

```bash
ssh pi@boat-pi.local
cd ~/boat-agent && ./deploy/install.sh
```

Then create `.env` (copy `.env.example`, `chmod 600`), apply the Signal K config, and
start the agent:

```bash
./deploy/apply-signalk-config.sh && sudo systemctl start boat-agent
```

Deploy a change from the laptop:

```bash
rsync -av --exclude .git --exclude .env --exclude .venv ./ pi@boat-pi.local:~/boat-agent/ && ssh pi@boat-pi.local 'sudo systemctl restart boat-agent'
```

## Running it by hand

```bash
python -m agent.main                  # what the service runs
python -m agent.main --once           # connect, collect 10s, print what arrived, exit
python -m agent.main --discover       # subscribe to every path, report what the bus has
python -m agent.main --forecast       # print the forecast for the boat's position, exit
```

`--once` exits non-zero if nothing arrived, so it doubles as a connectivity check.
`--discover` is how to find out what your instruments actually publish, rather than
what they are supposed to publish.

From the laptop, point it at the boat:

```bash
SIGNALK_HOST=boat-pi.local python -m agent.main --once
```

## Configuration

All optional; the defaults assume the agent runs on the Pi next to Signal K.

| Variable | Default | |
|---|---|---|
| `SIGNALK_HOST` / `SIGNALK_PORT` | `localhost` / `3000` | |
| `SIGNALK_TOKEN` | unset | only if readonly access is off |
| `BOAT_NAME` | `Seabird` | heads every message and the page; must not be a command word like `status` or `boat` |
| `AGENT_SNAPSHOT_INTERVAL` | `60` | seconds between logbook snapshots |
| `AGENT_STALE_AFTER` | `300` | seconds before a path is flagged stale |
| `AGENT_LOG_DIR` | `./logs` | |
| `AGENT_LOG_LEVEL` | `INFO` | |
| `AGENT_DISCOVER` | off | same as `--discover` |
| `AGENT_WEATHER` | on | fetch a forecast from Open-Meteo; needs no key |
| `AGENT_WEATHER_INTERVAL` | `3600` | seconds between fetches |
| `AGENT_WEATHER_OUTLOOK_HOURS` | `12` | how far ahead the forecast rule looks |
| `AGENT_ANCHOR_OUTLOOK_HOURS` | `18` | how far ahead it looks at anchor, and at arming |

## Tests

```bash
pip install -r requirements-dev.txt
pytest && ruff check .
```

**The `.venv` belongs to one machine.** It holds absolute paths to the interpreter that
created it, so a venv built on one machine is unusable on another —
they simply overwrite each other's when the repo lives on a shared drive. Build your own
outside the repo and point at it, rather than sharing the one in the tree:

```bash
python3 -m venv ~/.venvs/boat-agent
~/.venvs/boat-agent/bin/pip install -r requirements-dev.txt
~/.venvs/boat-agent/bin/pytest
```

`deploy/install.sh` still makes `.venv` in the repo, which is correct on the machine that
runs the agent: there is exactly one there, and systemd needs to find it.

`tests/fixtures/deltas.jsonl` is a recorded Signal K stream, including the malformed
frames — a delta with a broken timestamp, one with junk in the values array, and an AIS
target — because those are what actually break things. Record more with the provider's
`logging` flag (see [signalk/README.md](signalk/README.md)) and add them here.

## One command

Four flags is three too many to remember at 0200 on a pitching boat.

```bash
boat() { (cd ~/boat-agent && .venv/bin/python -m agent.main --console "$@"); }
```

Then `boat` draws what the agent can see and waits for a letter:

```
  Seabird  unknown    01:24 UTC
  guess · no speed data

  GPS     36.83120N 10.30340E
  Anchor  40 m circle · 12 m off · set 01:24 UTC
  Weather next 18 h: up to 14 kn from the NNW, gusting 34 kn
  Speaker will sound if an alarm is raised

  Speed   -          Course  -          Depth   -
  Wind    -          Battery -          Solar   -

  [a] anchor down   [u] anchor up   [w] weather
  [h] hush 30 min   [s] let it sound   [r] refresh   [q] quit
  [z] silence every alert   [x] restart the agent

  >
```

`a` asks for a circle in metres and arms the watch where the boat is. `u` stands it down.
`h` quiets the speaker for half an hour and nothing else. `w` fetches the forecast hour
by hour. `z` turns every alert channel off until you turn them back on, and asks you to
confirm before it does — see [Silencing her](#silencing-her). `x` restarts the agent.

The `z` key never toggles blind: its label says which way it will move, because the
banner above it always says which state the boat is in. When she is silenced the whole
screen is topped by a red line saying so, and the key reads `TURN THE ALARMS BACK ON`.

**It reads a file, not the bus.** The agent writes what it can see to `logs/status.json`
every rule tick. The console reads that, so it opens instantly, never opens a Signal K
connection of its own, cannot compete with the agent for the one it has, and works over
an SSH link too poor to hold a websocket open. The price is a picture up to one tick old,
so the age is always on screen — and a stale one says the agent may be down rather than
sitting there looking calm. No file at all means the agent is not running, and it says so.

**The GPS gets its own line, and three answers rather than two.** A receiver that has
never reported, one that has stopped reporting and says how long ago, and one still
reporting with no fix are different faults with different fixes: a cable, a crashed
daemon, a view of the sky. A stale fix is not a position — it is not shown as one, the
distance from the anchor is not computed from it, and a new watch cannot be armed on it.
A boat that dragged half a mile fifteen minutes ago would otherwise look like a boat
sitting quietly where it was.

**Every letter writes the file the flags write.** `a` writes the same `logs/anchor.json`
that `--anchor-down` writes. There is one mechanism for arming a watch on this boat, and
the console is another doorway to it rather than another implementation of it.

### And a page, if you would rather tap

`AGENT_UI=1` serves the same thing over HTTP, for a phone in the cockpit. It is off by
default and bound to localhost when on; to reach it from the boat's WiFi set
`AGENT_UI_BIND=0.0.0.0` and `AGENT_UI_TOKEN`, then open
`http://<the boat's laptop>:8375/?t=<token>`. The controls arm and clear an anchor watch,
and an anchorage is not a place to leave those open, so it refuses everything without the
token.

It runs on its own thread and reads only the finished dictionary the agent publishes,
never a live object, so no phone on a bad connection can delay an alarm by a tick. If the
port is taken it says so and the agent carries on without it.

**The page is built around one question**, the one anybody opens it to ask at 0300: is
the boat where I left it? A figure cannot answer that as fast as a shape can, so the hero
is a plot rather than a number — the anchor at the centre, the watch circle, the ten
metres of margin the drag rule allows before it calls it dragging, and the last half hour
of the boat's own track fading from old to new. A boat lying quietly traces a short arc
back and forth. A boat that has dragged traces a line. You can tell them apart across a
cockpit without reading anything.

The scale is set by whichever is larger, the alarm ring or the boat, so a drag can never
run off the edge of the picture.

**The wind is drawn, not summarised.** Twenty-four hours of forecast as a trace, gusts
dashed above it, the force 6 line the rule speaks up at marked and labelled, and the
hours that will be dark shaded behind. When a blow lands matters as much as how hard it
blows: thirty knots at noon is a decision, the same thirty knots at 0300 is a decision
taken half asleep with a torch in your teeth.

**The sea rides along the bottom of the same frame**, on its own scale and without a line
of its own: wave height follows the wind closely enough that a second trace would mostly
repeat the first. What is worth the ink is underneath in words, and it is the period that
carries it. Two metres at nine seconds is a swell you sleep through; two metres at five
is what empties a cove at three in the morning, so a sea steeper than about height over
period squared is called short and steep rather than left as a number.

**How far the boat has actually been** since the hook went down is in the readout under
the plot, with how long it has been watching. It is the number that says whether the
circle is big enough, and no half hour of track can show it. It resets when the anchor is
re-laid, because a furthest measured from where the hook used to be is worse than none.

**How long the dark has left to run** sits with the forecast, from the model's own
sunrise rather than guessed from latitude. It is what decides whether you sit a blow out
or move while you can still see the other boats.

**Three lights, chosen by the crew.** Chart paper by day, a lit instrument at night, and
a red mode for when night vision is worth protecting — you lose it in a second and get it
back one minute at a time. Night is the default. The choice is remembered on the phone,
and it follows the light on deck rather than a setting in an operating system.

**Positions read the way a plotter shows them**, in degrees and decimal minutes. Decimal
degrees is a convenience for machines and looks wrong on a chart table. The raw floats
stay in the daily log.

**Discovery One, more or less.** The displays in 2001 are pure black with thin coloured
geometry floating on them and every label is a short word in widely spaced capitals. What
is deliberately absent is the whole vocabulary of screen pastiche: no scanlines, no glow,
no chromatic fringing, no bevels, no rounded corners, no gradients. Kubrick's futurism is
austere, and that restraint is the entire difference between the film and every homage to
it. Three worlds, and all three are in the picture: the console is black, the ship is
white, and HAL is red.

**One typeface, embedded.** Alte Haas Grotesk, subset to the characters the page can draw
and inlined as woff2, about twenty kilobytes a weight. At anchor there is frequently no
internet, and a page whose type only sometimes arrives is worse than one that always
looks the same. It has tabular figures, so it carries the readouts as well as the words
and the page needs no second family.

**On a laptop it stops being a column.** Past about 62rem wide and 34rem tall it becomes
two panels that fit the window with nothing to scroll: the plot takes the whole left side
and grows with the screen, and everything with words in it stacks down the right. The
phone layout is untouched below that. It is sized so the mode switch is still on screen
at 1366 by 768, the shortest laptop worth caring about, because an unreachable red button
is the same as no red button at the one moment it matters.

## Rules

Twelve rules, all in [agent/rules.py](agent/rules.py) with the thresholds at the top of
the file so they can be argued with in one place.

| Rule | Fires when | Debounce |
|---|---|---|
| `shallow_water` | < 2 m (warn) / < 1 m (alarm) **under the keel**, only above ~1 kn | 10s |
| `anchor_drag` | Outside the anchor circle + 10 m margin | 30s |
| `anchor_watch_blind` | Anchor set, but no usable position. Alarm after 15 min | 120s |
| `house_voltage` | < 12.0 V, < 11.8 V, or > 14.8 V | 120s |
| `battery_temperature` | > 45 °C, < −10 °C, or charging below 0 °C | 120s |
| `locker_temperature` | > 40 °C (the MPPT derates), > 55 °C alarm | 300s |
| `wind_forecast` | F6, or gusts over 33 kn, forecast inside 12 h — 18 h at anchor | 300s |
| `bus_silent` | Signal K is talking but no instrument has, for 15 min | 60s |
| `bilge_cycling` | 6 pump starts in an hour | — |
| `starlink_down` | Offline, or silent for 10 min | 60s |
| `cpu_temperature` | > 78 °C, > 84 °C alarm | 120s |
| `disk_free` | < 2 GB, < 512 MB alarm | 60s |

Three properties every rule holds to:

- **Missing data is not a reading.** A depth sensor that stopped reporting never reads
  as "0 m, aground" — rules check staleness and stay silent. A GPS with no fix reports
  0,0, which is fresh, frequent and useless, so that is thrown away too rather than
  measured against the anchor.
- **A silent bus is news.** Every rule goes quiet when its own instrument does, which is
  right. But if *every* instrument is quiet at once the boat is not becalmed, it is
  disconnected, and `bus_silent` says so. It only speaks when Signal K is delivering
  something else, so it cannot double up with the startup complaint about a server that
  is refusing to talk at all.
- **Silence gets checked.** A rule that goes quiet because it has nothing to work with
  is the failure this project fears most, so `anchor_watch_blind` watches the drag rule
  and says when the watch you believe you have has stopped watching.
- **Nothing fires on one sample.** A condition holds for its debounce first.
- **A forecast is never an alarm.** `wind_forecast` is capped below alarm level on
  purpose, so it reaches a phone and a screen but never sounds the siren. A siren at
  0200 for a wind that arrives at 1100 is how a crew learns to switch the siren off.
- **Clearing is slower than raising**, and thresholds have hysteresis, so a value on the
  threshold does not flap.

Alerts appear in the journal and as `"type": "alert"` lines in the logbook. `since` is
when the condition started, `ts` when it actually raised — the gap is the debounce.

The anchor watch uses `navigation.anchor.*` from the bus if something publishes it (the
Signal K anchor alarm plugin does), otherwise `AnchorDragRule.set_anchor()`. With no
anchor set the rule stays silent rather than guessing where the hook went down. When an
anchor **is** set and the position goes away, `anchor_watch_blind` speaks up instead:
a warning after two minutes, an alarm after fifteen. It tells the two cases apart,
because they read differently in the log even though they mean the same thing to a
sleeping crew — a receiver that has stopped reporting, and one still reporting with no
fix to report.

### Depth and the keel

The example draft is 2.0 m; set your own in `signalk/baseDeltas.json`. The instrument reports depth below the **transducer**, so the rule
subtracts an offset to get water under the keel, and reports that — it is the number
that decides whether the boat stops.

The offset defaults to the full 2.0 m draft, which assumes the transducer sits at the
waterline. It does not; it is some way below. So the default **understates** clearance by
however deep the transducer actually is, and the alarm fires early rather than late.
Measure the transducer depth and set `environment.depth.transducerToKeel` in
[signalk/baseDeltas.json](signalk/baseDeltas.json) to `2.0 − transducer depth` to recover the
difference. A live value from Signal K always wins over the built-in constant, and
`environment.depth.belowKeel` wins over both if anything publishes it.

**Check the depth instrument first.** Most depth displays have their own offset setting,
and it may already be configured to show depth below keel or below surface rather than
below transducer. If it is, the agent would subtract the offset a second time and read 2 m
shallower than reality — safe, but it would alarm constantly. Compare the instrument's display
against `environment.depth.belowTransducer` in the Signal K data browser at a known
depth before trusting either.

House voltage comes from the MPPT and is a rough proxy — see
[signalk/README.md](signalk/README.md).

## Derived state

[agent/derived.py](agent/derived.py) infers what the boat is doing and writes it into
the snapshots as `navigation.state`: `anchored`, `stopped`, `underway-sail`,
`underway-motor`, or `unknown`. Every conclusion carries a confidence and the reason it
was reached — `"making 2.9 m/s in 0.8 m/s of true wind"` — so a reader can weigh it
instead of taking an inference for a measurement.

**Motoring is inferred, not measured.** There is no engine data without an engine
gateway such as an Actisense EMU-1. The rule only claims motoring when sailing is *physically impossible*: making way
in under ~4 kn of true wind, or making way pointing closer than 30° off the true wind.
Everything else reads as sailing, so a motorsail under main will say `underway-sail`.
That is the safe direction to be wrong in — the agent never asserts an engine is running
when it might not be, and `engine_running` is `None` rather than `false` when unknown.

**`moored` is never inferred.** Moored and anchored both look like "stopped" from
position and speed alone. The state is `anchored` only when an anchor is genuinely set,
`stopped` otherwise. Telling them apart from swing behaviour would need half an hour of
heading history; it is not attempted.

State changes must hold for two minutes before they count, so a wind lull does not flip
the boat between sailing and motoring. The exception is the first state after startup,
which is adopted immediately. Instruments going quiet yields `unknown` and never
generates a transition — losing data is not the boat doing something.

This lives outside `state.py`, unlike the layout in CLAUDE.md: `BoatState` is a plain
value store that either has a reading or does not, and folding heuristics into it would
make both harder to test.

## A tablet in the cockpit: AIS over WiFi

An AIS on the N2K backbone and an iPad that speaks NMEA 0183 need the
sentences manufactured. Signal K does it with two plugins, both
installed by `deploy/install.sh` and configured from `signalk/plugin-config-data`:

- `signalk-n2kais-to-nmea0183` turns the AIS PGNs from the bus back into
  `!AIVDM` sentences. That is the targets.
- `@signalk/signalk-to-nmea0183` builds our own position, course, depth, wind,
  heading and log from the Signal K model: RMC, DPT, MWV, HDM and the rest.

Both feed the server's `nmea-tcp` interface, which is already on in
`signalk/settings.json` and listens on **10110**. Check it from the laptop with
the NGX-1 plugged in and the instruments awake:

```bash
nc localhost 10110 | head -20     # expect $GPRMC..., $SDDPT..., !AIVDM...
```

On the iPad, join the boat's WiFi and point the app at the laptop's address on
port 10110 over TCP. Navionics, iSailor, Aqua Map and iNavX all take a plain
NMEA 0183 TCP feed. Two things make it stick:

- **Give the laptop a fixed address.** Reserve it in the router, or the
  iPad's saved connection breaks every time the lease moves.
- **Open the port if the firewall is up.** `sudo ufw status`, and if it is
  active, `sudo ufw allow 10110/tcp`.

If you would rather not configure an app at all, the iPad can open Freeboard-SK
in Safari at `http://<laptop>:3000` and see the same AIS targets on a chart,
with no NMEA conversion in the path.

## Running it on a laptop

The agent can live on an Ubuntu laptop aboard rather than a Pi. A laptop that
is already aboard, with the NGX-1 and a USB GPS plugged in, costs nothing to
wire in:

```bash
./deploy/install.sh --laptop
```

That skips what only exists on a Pi: the 1-wire GPIO bus, the Pi-only Signal K
plugins, and the wait for a clock that a laptop already has. Everything else is
the same install, and the agent, the rules and the ship's log do not know the
difference.

Three things a laptop needs that a Pi does not, printed as notes at the end of
the install rather than done for you, because when someone's laptop sleeps is
not an installer's decision:

- **The lid.** systemd suspends on lid close, and a suspended laptop raises no
  alarms. `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf` keeps it awake.
- **Idle sleep.** Check nothing else suspends it, in GNOME settings or
  `sleep.target`.
- **The GPS.** The anchor watch needs a position and nothing else, so the
  instruments can stay off overnight. gpsd owns the receiver and serves it to
  everyone, including OpenCPN. See below.

## Signal K security, and the token the agent needs

The installer starts Signal K with `--securityenabled`, which is right on a boat
carrying a dish and a WiFi network other people can see. It has one consequence
worth understanding, because it is silent:

**An agent with no token is not refused loudly. It connects, subscribes, and is
told nothing.** No data, no rules firing, no alarms, and a log full of snapshots
of an empty state model. The agent now probes the REST API at startup and says
so in the journal if it is being refused, so this cannot go unnoticed:

```
ERROR agent: Signal K is refusing to let this agent read anything (401). It will
connect and see nothing, and no rule can fire.
```

The fix is a device token, and the agent can ask for one itself:

```bash
python -m agent.main --request-access
```

It registers as a device, prints where to approve it, waits for a human to say
yes in the admin UI under Security then Access Requests, and writes the returned
token into `.env` as `SIGNALK_TOKEN` with the file left at mode 600. Restart the
agent and it can read the bus. The token does not expire, and the request uses a
client id derived from the hostname, so running the command twice does not queue
a second request.

**The one thing that cannot be automated** is the very first admin login. Signal
K refuses everything until an admin user exists, and creating it means choosing
a password, so open `http://boat-pi.local:3000` in a browser and make it
once. Then approve the request.

If you would rather not run security at all on the boat's own network, drop
`--securityenabled` from `deploy/install.sh` and leave `SIGNALK_TOKEN` empty.
That is a real choice, not an oversight, but make it deliberately.

## Setting the anchor watch

The drag alarm needs to know where the anchor is. Signal K's anchor plugin can
say so; until that is running, the crew says so:

```bash
python -m agent.main --anchor-down                  # here, default circle
python -m agent.main --anchor-down --radius 45      # a bigger circle
python -m agent.main --anchor-down --at 36.83,10.30 # somewhere else
python -m agent.main --anchor-up                    # anchor is aboard
```

`--anchor-down` reads the boat's current position from Signal K and writes it to
`logs/anchor.json`. The running agent picks the file up within one rule tick and
feeds it into the state model exactly as the plugin would, so the drag rule, the
derived state, the snapshots and the ship's log all see one anchor rather than
two. Nothing needs restarting.

The file is the interface, and it is four numbers you can read. It is also why
the watch survives a reboot, which matters more here than anywhere else in the
agent: an anchor alarm that quietly forgets itself when the Pi restarts at 0300
is worse than no alarm at all, because you believe you have one. For the same
reason `--anchor-up` does not merely delete the file. The drag rule never
expires an anchor, so the agent publishes a zero radius to stand the watch down
explicitly.

### What the night looks like

`--anchor-down` also checks the forecast where the hook just went down, and says what
the next eighteen hours hold. It is printed at arming because that is the last cheap
moment to do anything about the answer: more chain, a different cove, or leave while
there is light.

```
anchor watch set at 36.83000N 10.30000E, circle 40 m.
next 18 h: up to 14 kn from the NNW, gusting 34 kn, sea to 1.5 m, peaking 15:00 UTC
that is gusts over 33 kn before morning. Worth checking the scope, the swinging room,
and where the shore is if the wind veers.
```

It names which of the two crossed the line. A night of 14 knots gusting 35 is a
different night from a steady 30, and saying "F6 or more" when the sustained wind never
gets near F6 is the small inaccuracy that teaches people to discount the next one.

A quiet night gets the same line without the warning, and a forecast that cannot be
fetched is reported as not fetched. Believing somebody checked the weather is worse than
knowing nobody did. None of it can stop the watch being set.

The running agent records the same assessment in the logbook as an `anchor_forecast`
event, so the ship's log shows what was known at the moment the crew committed to the
spot. That is the record; the alerting is the `wind_forecast` rule below, which keeps
looking and speaks up again if the model changes its mind at midnight.

## The ship's log

Once a day, at `AGENT_SHIPS_LOG_HOUR` UTC, the agent adds an entry to
`logs/ships-log.md`, newest first. It is built by reading back the last 24 hours of the
agent's own daily log, not by watching the instruments, so every figure in it can be
checked against the JSON lines it came from, and a restart in the middle of the night
costs nothing.

```bash
cat logs/ships-log.md                                             # read it
.venv/bin/python -m agent.main --ships-log                        # write one now
ssh pi@boat-pi.local 'cat ~/boat-agent/logs/ships-log.md'   # if it runs on a Pi
```

A night at anchor reads like this:

```markdown
## 2026-09-08 06:00 UTC

| reading | low | high |
| --- | --- | --- |
| House voltage | 12.87 V | 13.90 V |
| Solar | 0 W | 505 W |
| Depth below transducer | 8.1 m | 8.7 m |
| Pi CPU | 51.5 C | 51.5 C |

**Anchored**
- 36.83120N 10.30340E from 17:00 to 06:00 (13.0 h), swinging up to 33 m

**Alarms**
- 01:00 raised - At anchor. Wind building: 25 kn from the NNW gusting 34 kn from 02:00 UTC, in 2 h
- 05:00 cleared - At anchor. Wind building: 25 kn from the NNW gusting 34 kn from 02:00 UTC, in 2 h

**State**
- 17:00 underway-sail to anchored (stopped inside the anchor circle)

**Forecast**
- next 12 h: up to 14 kn from the NNW, gusting 30 kn, sea to 1.5 m

14 snapshots.
```

An entry carries the range each instrument covered, the alarms with the times they were
raised and cleared, the state changes, where the boat lay at anchor and how far it swung
there, and how many miles it ran.

**Ranges, not averages.** "The bank sat between 12.87 and 13.90 volts" is a fact about
the night; a mean would hide both the sag before dawn and the absorption voltage at
midday, which are the two numbers worth having.

**Distance carries its source.** The instruments' trip log is a measurement and is labelled as
one. Without it the distance is estimated by joining position fixes, counted only while
the boat said it was making way, so a night swinging to an anchor does not read as a
passage. It is labelled `estimated from fixes` in the entry, and it undercounts a day of
tacking, which is why it is never allowed to look like a log reading.

**An instrument that said nothing all night gets no row**, rather than a zero.

**Anything still alarming at the time of writing is called out in the heading**, so a bad
night is visible without reading the entry.

**The forecast comes last, under its own heading.** Everything above it was measured and
it was not, and an entry read over coffee should not be able to blur the two. It is the
forecast the boat was holding when the entry was written, not a claim about the night
that has passed.

`--ships-log` reads only the log directory, so it runs on a laptop against a copy of the
logs as happily as on the boat. Entries fall off the bottom at `AGENT_SHIPS_LOG_ENTRIES`,
matching the log retention by default, since an entry should not outlive the log lines it
was computed from. The file is written beside and renamed over, so a power cut mid-write
leaves yesterday's file rather than half of today's.

## Alerts over Signal

Alerts go out through `signal-cli` linked to your existing Signal account as a secondary
device — no separate phone number, and your phone keeps working normally.

```bash
./deploy/install-signal-cli.sh          # JRE 25, signal-cli, correct libsignal
./deploy/install-signal-cli.sh --link   # QR code, scan from Signal on your phone
./deploy/install-signal-cli.sh --check  # confirm the native library actually loads
```

Then set `SIGNAL_ACCOUNT` and `SIGNAL_RECIPIENT` in `.env`. With `SIGNAL_ACCOUNT` unset
the agent logs notifications instead of sending them, so it runs fine unconfigured.

`SIGNAL_RECIPIENT` takes more than one number, separated by commas — a second phone
aboard, or someone ashore keeping half an eye on the boat. Each gets its own send, so a
number that is wrong or no longer on Signal cannot stop the others hearing the alarm; the
log says who missed it. Beyond two or three phones use `SIGNAL_GROUP` instead, which is
one send however many people are in it.

**On a Pi this needs two things it does not need on a laptop.** signal-cli requires
JRE 25, and the `libsignal` native library bundled in the release is x86_64 only — on
aarch64 every command dies with `UnsatisfiedLinkError` until it is replaced with an arm64
build. The installer handles it, reading the required libsignal version out of the jar it
just unpacked rather than hardcoding one, so a signal-cli upgrade cannot silently install
a mismatched library. `--check` runs `listAccounts` rather than `--version`, because only
the former actually touches the native library.

**Sending is built to fail.** Every alert goes into a persistent outbox
(`logs/outbox.json`) before a send is attempted. If Starlink is down it stays there,
survives an agent restart, and goes out when the link returns — stamped with how late it
is, so a drag alarm from twenty minutes ago does not arrive looking current. Messages
older than six hours are dropped rather than delivered as stale news. Nothing in the
notification path can raise into the agent loop: losing Signal must not stop monitoring.

## One receiver, several readers

A serial port has exactly one owner. More than one thing aboard wants the
u-blox: the agent needs a position for the anchor watch, OpenCPN needs one to
plot, and there will be a third eventually. Letting them open the device
directly means a race, and the loser is quiet about it.

So gpsd owns the receiver and everything else is a client of it. That is the
whole reason gpsd exists, and it is already installed on Ubuntu.

```bash
gpspipe -w -n 5              # is gpsd serving anything?
systemctl status gpsd        # is it holding the receiver?
```

Signal K speaks gpsd's protocol natively, so its connection is a gpsd input on
`localhost:2947` rather than a serial port. **OpenCPN should read the same
daemon**: Options, Connections, Add, Network, protocol GPSD, address
`localhost`, port `2947`. Delete any connection it still has to `/dev/ttyACM0`,
or it will go back to fighting Signal K for the device.

Reading gpsd directly rather than through Signal K means OpenCPN's position does
not depend on the agent or the server being up, and it is one hop shorter.

**The flag that makes it fast is `-n`.** Without it gpsd opens the receiver only
when a client connects, so every program that starts pays for a cold start
before it has a fix, which on a GPS is tens of seconds and on a cold receiver
can be minutes. With it the receiver tracks continuously and a fix is already
there the moment anything asks. The installer sets it in `/etc/default/gpsd`
along with the device, and backs up whatever was there before.

The cost is a receiver that is always powered and a daemon always running, which
on a boat with a few hundred watts of solar is not a cost worth thinking about.

## Alerts on the screen

Every alert also appears as a desktop notification on the machine the agent runs
on, at the same threshold as the Signal messages. It reaches the one person
Signal does not: whoever is already sitting at the chart table with the laptop
open, whose phone is in a locker somewhere.

Three details:

- **An alarm stays on the screen until it is dismissed.** Anything below alarm
  level fades after thirty seconds. A sticky note about the locker being warm is
  how people learn to swipe notifications away without reading them.
- **One rule keeps one notification.** A rule that raises, worsens and then
  clears replaces its own notification each time rather than leaving three on
  the screen disagreeing about the state of the boat.
- **Nothing is ever retried.** There is no outbox here, unlike Signal. A
  notification that turns up twenty minutes late is worse than one that never
  came, because the screen cannot say "this happened a while ago" and it will be
  read as now.

The words are the same as the Signal message, split at the first line: headline
in bold, the rest underneath. One formatting rule for both channels, so they
cannot drift apart.

This is the weakest of the three channels — it needs a session, a notification
daemon and someone looking at the screen — which is exactly why it is a third
channel and not a replacement for either of the others. Signal needs the sky,
the speaker needs the volume up, the screen needs somebody in front of it. Set
`AGENT_DESKTOP_NOTIFY=0` to turn it off.

## Asking the boat a question

Everything else here is one-way: the boat decides something is worth saying and
says it. This is the other direction. Text the crew group and she answers.

```
status    where she is, what is holding, what the night looks like
weather   the forecast, the sea state, how long the dark has left to run
anchor    the watch, how far off, and the furthest she has been
hush 30   quiet the speaker for half an hour
sound     let it sound again
help      the list
```

An answer arrives within a minute. signal-cli takes about four seconds to start
a JVM, so this polls rather than running a daemon: a daemon is another process
to supervise, restart, and discover has been dead since Tuesday. Nothing here is
an alarm, and the alarms do not come this way. Polling also keeps the Signal
session healthy, which signal-cli warns about and which the sending side quietly
depends on.

**The bank is always reported, even when there is nothing to report.** A status
message with no battery line reads as "the bank is fine" and means "I have no
idea", which is the wrong way round for the one number you cannot see from
ashore. With the MPPT talking it reads `Bank 13.82 V (MPPT, rough), charging at
18.4 A, 505 W from the panels, 1300 Wh today.`; without it, `no reading from the
MPPT over Bluetooth`, which says where to go looking.

The `(MPPT, rough)` is not modesty. The only voltage available is the charger's
own battery-side reading: charger output while the sun is up and absent at
night. It is good for "something is badly wrong" and useless as state of charge,
and there is no real SOC without a battery monitor such as a SmartShunt. A number that looked
authoritative would be worse than one that says what it is.

**It answers from `logs/status.json`**, the same file `--console` reads, rather
than reaching into the running state model. One picture of the boat, one place it
is built, and the text cannot disagree with the screen. It also means this can
never slow a rule down, because it reads a file.

**It will not touch the anchor.** Arming a watch by text arms it at a position
nobody has checked; clearing one disables an alarm from a bar. Hushing is allowed
because a hush always expires and only silences the speaker.

**Two shapes of message arrive, and missing the second answered nobody at all.**
signal-cli is a linked *secondary* device. Another person texting the group
arrives as a `dataMessage`. The *owner* texting from their own phone arrives as
a `syncMessage`, because linked devices are told what the primary sent and never
see it as incoming mail. Dropping syncs to avoid loops therefore drops the
skipper and nobody else.

They still cannot be treated alike, since the agent's own replies go out through
the same account. A sync counts as a question only when it opens with a word the
agent understands, and no reply ever does. Anyone else is answered whatever they
type, including a typo.

## The drill

```bash
boat --drill
```

Sends one real alert down every channel and reports what got through:

```
  OK    Signal   delivered to the crew group
  OK    Screen   notification shown on this machine
  OK    Speaker  played with pw-play

A drill proves the boat can reach each channel. It cannot prove a message woke
you, that anyone looked at the screen, or that the noise was audible from a
bunk. Mute a Signal group or turn the volume down and this still passes.
```

`--test-alarm` proves the speaker and nothing else. This proves the chain, using
the same code a real drag alarm uses rather than a rehearsal of it. You test the
flares and the liferaft before you need them.

**It exits non-zero when a configured channel fails**, so it can be run from a
script or before a passage. A channel that is off by choice is reported as `OFF`
and is not a failure, with one exception: every channel off is a failure, because
a boat that cannot raise the alarm at all should not report a pass.

**The caveat is the most important line it prints.** The drill establishes that
the boat can reach each transport, and nothing beyond that. Whether a message
wakes a sleeping crew is a question about a phone's notification settings, and
this cannot answer it.

The result goes in the daily log as an `event`, never as an `alert`, so a drill
can never appear among the night's real alarms in the ship's log.

## The alarm this machine makes itself

Everything in the section above depends on Starlink being up, on the dish having
sky, and on a phone being somewhere it can ring. On the night it matters none of
that is certain. So the machine running the agent also makes a noise of its own,
which is the one thing the nav laptop does better than the Pi would have: it has
speakers, and the crew is asleep six feet from them.

```bash
.venv/bin/python -m agent.main --test-alarm   # make the noise now, say what played it
.venv/bin/python -m agent.main --hush         # 30 minutes of quiet
.venv/bin/python -m agent.main --hush 120     # or as long as you need
.venv/bin/python -m agent.main --unhush       # end it early
```

It sounds for anything at **alarm** level or above and repeats every 20 seconds
until the alert clears, then stops on its own. The threshold is deliberately
higher than the one for Signal messages: a machine that beeps at every warning
is a machine somebody turns the volume down on, and then the drag alarm is
silent too. Everything is settable in `.env`.

Three details worth knowing:

- **The noise comes first.** In the rule loop the alarm is started before the
  logbook write and before Signal. `signal-cli` starts a JVM per message and a
  send with no link waits for its timeout, so an alarm queued behind that can be
  minutes late. Playing happens in a background task, so the rules keep running
  while the saloon is being woken up.
- **A hush always expires.** An alarm that cannot be stopped gets its power
  pulled, and then nothing works for the rest of the season, so `--hush` is a
  quiet period with an end on it rather than an off switch. It silences the
  speaker only: the Signal message still goes out and the logbook still records
  it. An alarm raised during a hush sounds the moment the hush runs out. The
  off switch is a separate thing — see [Silencing her](#silencing-her).
- **Test it, then test it again in the spring.** An alarm nobody has heard is a
  guess, and the ways it fails are all quiet ones: the volume down, the output
  set to an HDMI socket with nothing plugged into it, a service with no route to
  the sound server. `--test-alarm` says which player worked.

The sound is generated rather than shipped — a `.wav` in the repo is one more
thing to lose. It is written once to `logs/alarm.wav` and reused. Players are
tried in order: `pw-play`, `paplay`, `aplay`, `ffplay`, and whichever works is
tried first next time. If none of them will play it, the agent says so once and
carries on with Signal alone.

The installer adds the user to the `audio` group, which matters more than it
looks: a desktop session grants access to `/dev/snd` by ACL and takes it away
again at logout, which is exactly the wrong moment to go quiet.

## Silencing her

A hush is the right answer to "it is going off and I am already dealing with it".
It is the wrong answer to the other thing that happens on a boat: she is on the
hard at the yard, or alongside with the instruments live, or the depth rule is
firing every ten minutes because the transducer offset is still unverified and
nothing is going to be done about that tonight. What is wanted then is not a
quieter speaker. It is every channel off until somebody says otherwise.

```bash
boat --silence                        # speaker, Signal and the screen, all off
boat --silence "hauled out at the yard"  # with a reason, shown until it is lifted
boat --unsilence                      # everything back on
```

Or press `z` in the console, or tap the button on the page. All three write the
same `logs/silence.json`, which is one line a person can read and which survives
a reboot. The running agent picks it up within a rule tick; nothing needs
restarting.

**It stops all three channels together.** A silence that left Signal running
would put twelve alarms on a phone overnight, and a crew that mutes the crew
group in August is a crew that misses the real one in October. Off means off.

**It does not expire.** This is the whole difference from a hush, and it is
deliberate. A hush is granted in the middle of an event, and the alarm somebody
switched off at 0300 and forgot is the one that was going to save them. A
silence is granted for a *situation* — hauled out, alongside, laid up for the
winter — that outlasts any timer worth setting. One that came back on its own at
dawn would just be re-applied every morning until somebody set
`AGENT_ALARM_SOUND=0` in `.env` and lost the alarm for the season. An honest
switch that stays where it is put is safer than a timer that teaches people to
disable the real thing.

**So it nags, and that is the price.** Because it does not expire it has to be
impossible to leave on by accident:

- a line in the journal the moment it is set and twice an hour after that
- a red banner across the top of the console, above everything including "the
  agent is not running", and the speaker line reads *silenced, along with Signal
  and this screen*
- a red banner on the page, above the masthead, with the button to lift it
- the first line of every `status` reply in the crew group
- `--drill` **fails** while she is silenced. A green table under a standing
  silence is the most misleading thing that command could print, so it is
  treated the same as every channel being off
- and a line at the top of the next morning's ship's log entry saying how many
  hours of the window went unheard, with `ALERTS SILENCED` in the heading if she
  still is

**She keeps watching the whole time.** The rules run, alerts are raised and
cleared exactly as they would be, every line reaches the daily log, and the
console and the page still show what is standing in red. Only the three ways she
has of reaching a person are held.

**Lifting it re-announces whatever is still standing.** Signal and the screen
only ever see transitions, and an alert raised during a silence had its
transition while nobody was listening. Without this, turning the alarms back on
would leave a drag alarm sitting in the log and in no place a person would
actually meet it — which is the exact failure the silence was supposed not to
cause. The speaker needs no help: it sounds on what is standing rather than on
what changed, so it starts again by itself.

**It cannot be set by text.** The crew group will turn the alarms back on from
anywhere — that direction can only ever make the boat louder, and somebody who
realises over dinner that they left her silenced should be able to fix it from
where they are. It will not silence her: that switch does not expire, and the
one place it must never be thrown from is somewhere you cannot see what you are
turning off.

## The forecast

[agent/weather.py](agent/weather.py) fetches a forecast for wherever the boat actually
is, once an hour, from [Open-Meteo](https://open-meteo.com). No key, no account, no
dependency beyond the standard library, and about four kilobytes on the wire. See it
now, without starting the agent:

```bash
.venv/bin/python -m agent.main --forecast              # for the boat's position
.venv/bin/python -m agent.main --forecast --at 36.83,10.30
```

```
forecast for 36.83000N 10.30000E
now 9 kn NNW, sea 1.4 m, peak 13 kn at 09:00 UTC, gusts to 30 kn, grid 6 km away
  UTC               wind  gust   dir    sea
  Mon 07 21:00       9kn  24kn   NNW   1.4m
  Tue 08 09:00      13kn  30kn   NNW   1.5m
```

The forecast goes into the state model as `environment.forecast.*` deltas, exactly as
the anchor file does, so the snapshots record it and the ship's log can quote it,
without either knowing where it came from. Those paths are
agent-derived: the Signal K specification defines no forecast paths at all.

Everything is converted to SI on the way in — m/s, radians, Pascals, Kelvin — and back
to knots and compass points only for display.

**It looks further ahead once the anchor is down.** Underway the useful question is the
next twelve hours, because the boat is already moving and can keep moving. At anchor the
question is the whole night, so the horizon goes out to eighteen hours — a boat
anchoring at 1500 wants to know about 0300, and a twelve-hour window does not reach it.
The thresholds do not change with it. A night in a windy summer anchorage is routinely
F6, and a lower bar here would fire most nights of the season, which is how an alert
stops being read.

**It is not on the alarm path, in either direction.** No link means no forecast, and the
one rule that reads it goes quiet rather than guessing. A failed fetch keeps the
previous forecast rather than leaving a hole, backs off to five minutes, and says so
once instead of filling the journal for the week the sky is blocked. Anything older
than two hours stops counting as evidence about tonight.

### Why Open-Meteo

No key, no account, a few kilobytes on the wire, and it serves the same global models
most regional wave forecasts are built on. Services with finer local wave grids tend to
want credentials or ship NetCDF over OPeNDAP, which does not fit a machine that must
install over a satellite link. See the docstring in [agent/weather.py](agent/weather.py).
If a better source ever matters, `fetch_forecast()` returns a `Forecast` and nothing
downstream cares where it came from.

## What works so far

Subscribe, hold state, derive what the boat is doing, evaluate rules, write a daily log,
send alerts over Signal, put them on the screen of the machine the agent runs on, sound
a local alarm there for the ones that matter, fetch a forecast for wherever the boat is,
and write one ship's log entry a day — in plain English where there is an API key for
it, and as the figures alone where there is not.
The agent is strictly read-only and never writes to the N2K bus.

All three channels can be switched off together and left off — see
[Silencing her](#silencing-her) — which the boat will not let anyone forget she is.

**A message is still not an alarm**, and now it is not the only thing there is. The
local alarm covers the case Signal cannot: the link down and nobody's phone ringing.
It does not cover the case of nobody aboard, so both matter. On a Pi with no speakers
the fallback is still a piezo on a GPIO, which costs very little and does not
care about the sky.

Logs are `logs/YYYY-MM-DD.jsonl`, one JSON object per line, UTC, SI units throughout:

```json
{"ts": "2026-08-22T12:33:58+00:00", "type": "snapshot", "values": {"environment.depth.belowTransducer": {"value": 10.9, "age_s": 1.5, "src": "ngx1.35"}}, "counts": {...}}
{"ts": "2026-08-22T12:34:01+00:00", "type": "event", "event": "signalk_disconnected", "reason": "closed"}
```

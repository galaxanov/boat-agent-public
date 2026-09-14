"""A page for the phone in your pocket, served by the agent itself.

Everything the agent does is already reachable from a command line, and a
command line is the wrong instrument for the moment this is actually needed:
standing on the foredeck at dusk with the anchor just down, one hand on a
phone. This serves one small page over the boat's own network so that arming
the watch, seeing what the night is forecast to do, and shutting the speaker up
are each one tap.

Three rules shape it, and they are the same three the rest of the agent lives by.

**The file is still the interface.** Arming the watch from the page writes
`logs/anchor.json`, byte for byte what `--anchor-down` writes, and the running
agent picks it up on its next rule tick exactly as it would from the crew at
the nav station. The page never reaches into the rule engine, never touches the
state model, and cannot put a value on the bus. Nothing here is a second way of
doing anything - it is a second way of *asking*, and there is one mechanism
underneath. A UI that had its own path into the alarms would be a UI that could
break them.

**It cannot slow the alarms down.** It runs on its own thread, in the standard
library's HTTP server, and it never touches a live object. Once a tick the
agent hands it a finished dictionary; the thread serves that and nothing else.
A phone on a bad connection, ten phones, or a page left open all night cannot
delay a drag alarm by so much as one tick, because the two never share anything
mutable. The worst a wedged request can do is serve a snapshot a few seconds
old, and the page says how old it is.

**It is shut by default.** It listens on localhost only until somebody says
otherwise, and binding it to the boat's network requires a token in the URL.
The controls here arm and disarm an anchor watch, and the boat's WiFi is not a
place to leave those open to whoever is anchored alongside.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .anchor import DEFAULT_RADIUS_M, AnchorFile, AnchorFix
from .geo import as_position, bearing_deg, distance_m, offsets_m
from .rules import FORECAST_GUST_WARN_MS, FORECAST_WIND_WARN_MS
from .silence import SilenceFile, silence_now
from .sound import HushFile, hush_until
from .units import (
    cardinal,
    celsius,
    compass,
    format_position,
    format_position_ddm,
    hhmm,
    knots,
    span,
)

log = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parent / "ui.html"

# Anything the crew might plausibly want as a watch circle. Bounds rather than
# a fixed list, because scope is a judgement about depth, bottom and room.
MIN_RADIUS_M = 5.0
MAX_RADIUS_M = 500.0

# A hush always expires. Half a night is the most this will hand out in one go.
MAX_HUSH_MINUTES = 720.0

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# How much of the wind forecast the graph draws. A day: long enough to show a
# blow arriving and passing, short enough that every hour is still worth
# believing.
GRAPH_HOURS = 24.0

# The swing track. Half an hour is one or two full sweeps of a boat sailing
# about its anchor, which is what tells a veer from a drag; twenty seconds
# between points keeps the shape without drawing GPS noise.
TRACK_WINDOW_S = 1800.0
TRACK_GAP_S = 20.0
TRACK_CAP = 200


class Track:
    """Where the boat has been, for the swing plot.

    Kept as positions rather than offsets, because the anchor can move: the
    crew re-lays it, or the plugin publishes a corrected one, and a track
    stored as offsets would then be a track drawn around the wrong point.
    """

    def __init__(
        self,
        window_s: float = TRACK_WINDOW_S,
        gap_s: float = TRACK_GAP_S,
        cap: int = TRACK_CAP,
    ) -> None:
        self.window_s = window_s
        self.gap_s = gap_s
        self._points: deque[tuple[datetime, float, float]] = deque(maxlen=cap)
        self._watch_key: tuple[Any, ...] | None = None
        self._watch_since: datetime | None = None
        self.furthest_m = 0.0

    def watch(
        self,
        anchor: tuple[float, float] | None,
        set_at: datetime | None,
        position: tuple[float, float] | None,
        now: datetime,
    ) -> None:
        """Keep the furthest the boat has been since this anchor went down.

        The single most useful number for deciding whether the circle is big
        enough, and it cannot be read off a plot that only holds half an hour.
        Reset when the anchor changes, because a furthest measured from where
        the hook used to be is worse than no figure at all.
        """
        key = None if anchor is None or set_at is None else (*anchor, set_at)
        if key != self._watch_key:
            self._watch_key = key
            self._watch_since = now if key is not None else None
            self.furthest_m = 0.0
        if anchor is not None and position is not None:
            self.furthest_m = max(self.furthest_m, distance_m(anchor, position))

    @property
    def watching_since(self) -> datetime | None:
        return self._watch_since

    def add(self, position: tuple[float, float] | None, now: datetime) -> None:
        """One fix, if it is worth keeping. Absent positions leave a gap."""
        if position is None:
            return
        if self._points and (now - self._points[-1][0]).total_seconds() < self.gap_s:
            return
        self._points.append((now, position[0], position[1]))
        while self._points and (now - self._points[0][0]).total_seconds() > self.window_s:
            self._points.popleft()

    def offsets(self, anchor: tuple[float, float] | None, now: datetime) -> list[list[float]]:
        """Metres east and north of the anchor, oldest first, with each age.

        The age travels with the point so the plot can fade the older end: a
        track that is all one weight says nothing about which way it was going.
        """
        if anchor is None:
            return []
        return [
            [
                round(east, 1),
                round(north, 1),
                round((now - when).total_seconds()),
            ]
            for when, lat, lon in self._points
            for east, north in [offsets_m(anchor, (lat, lon))]
        ]


# ------------------------------------------------------------------ payload --


def _round(value: float | None) -> float | None:
    """A whole degree. The receiver does not know the tenths and nor do we."""
    return None if value is None else round(value)


def _reading(state: Any, path: str) -> float | None:
    value = state.value(path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def build_payload(
    state: Any,
    derived: Any,
    alerts: list[Any],
    anchor: AnchorFix | None,
    weather: Any,
    hush: Any,
    config: Any,
    now: datetime | None = None,
    track: Track | None = None,
    silence: Any = None,
) -> dict[str, Any]:
    """Everything the page shows, converted for a person, computed once a tick.

    Built here on the agent's own thread rather than in the request handler, so
    the HTTP side never reads a live object. What it gets is this dictionary,
    finished and immutable in practice.

    Absent readings stay absent. A dash on the page is the honest rendering of
    an instrument that has said nothing, and is not the same as a zero.
    """
    now = now or datetime.now(UTC)
    # A stale fix is not a position. Everything below that uses one - the
    # distance from the anchor, and the fix a tap arms a new watch with - would
    # otherwise be computed from where the boat was, not where it is.
    position = (
        None
        if state.is_stale("navigation.position", config.stale_after)
        else as_position(state.value("navigation.position"))
    )

    readings: list[dict[str, Any]] = []

    def add(label: str, value: float | None, unit: str, places: int = 1) -> None:
        readings.append(
            {
                "label": label,
                "value": None if value is None else f"{value:.{places}f}",
                "unit": unit,
            }
        )

    add("Speed", knots(_reading(state, "navigation.speedOverGround")), "kn")
    add("Course", compass(_reading(state, "navigation.courseOverGroundTrue")), "°T", 0)
    add("Depth", _reading(state, "environment.depth.belowTransducer"), "m")
    add("Wind", knots(_reading(state, "environment.wind.speedApparent")), "kn app")
    add("Battery", _reading(state, "electrical.solar.mppt.voltage"), "V", 2)
    add("Solar", _reading(state, "electrical.solar.mppt.panelPower"), "W", 0)
    add("Sea", celsius(_reading(state, "environment.water.temperature")), "°C")
    log_m = _reading(state, "navigation.log")
    add("Log", None if log_m is None else log_m / 1852.0, "NM")

    anchored: dict[str, Any] = {"set": anchor is not None}
    if anchor is not None:
        anchored.update(
            {
                "position": format_position(
                    {"latitude": anchor.latitude, "longitude": anchor.longitude}
                ),
                "radius_m": round(anchor.radius_m),
                "set_at": hhmm(anchor.set_at),
                # None, not zero: with no fix there is no distance to report,
                # and a zero would read as "right on top of it".
                "distance_m": (
                    None
                    if position is None
                    else round(distance_m((anchor.latitude, anchor.longitude), position))
                ),
                "distance": (
                    None
                    if position is None
                    else _distance(distance_m((anchor.latitude, anchor.longitude), position))
                ),
                # Which way the boat is lying. After "how far" it is the first
                # thing anyone asks, because it says what the wind has done.
                "bearing_deg": (
                    None
                    if position is None
                    else _round(bearing_deg((anchor.latitude, anchor.longitude), position))
                ),
                "east_north": (
                    None
                    if position is None
                    else [
                        round(value, 1)
                        for value in offsets_m((anchor.latitude, anchor.longitude), position)
                    ]
                ),
                "track": (
                    []
                    if track is None
                    else track.offsets((anchor.latitude, anchor.longitude), now)
                ),
                "furthest": (
                    None
                    if track is None or track.furthest_m <= 0
                    else _distance(track.furthest_m)
                ),
                "watching": (
                    None
                    if track is None or track.watching_since is None
                    else span((now - track.watching_since).total_seconds())
                ),
            }
        )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        # The same moment a person would read off it, in their own zone. The
        # ISO stamp above stays UTC, because that is what ages are measured on.
        "clock": hhmm(now),
        "boat": config.boat_name,
        "state": str(derived.vessel),
        "confidence": str(derived.confidence),
        "reason": derived.reason,
        "position": (
            None if position is None else format_position(state.value("navigation.position"))
        ),
        # Degrees and decimal minutes, which is what a plotter shows and what
        # goes in a paper log. The raw floats stay in the daily log.
        "position_ddm": (
            None if position is None else format_position_ddm(state.value("navigation.position"))
        ),
        # Not just the position but which silence this is when there is not one.
        "gps": _gps(state, config),
        # The house bank, and an honest word about how little of it is measured.
        "battery": _battery(state),
        # The raw pair as well as the pretty string: whatever arms a watch
        # needs numbers, and it must be the same fix the page is showing.
        "fix": None if position is None else [position[0], position[1]],
        "readings": readings,
        "anchor": anchored,
        "alerts": [
            {
                "rule": a.rule_id,
                "severity": str(a.severity),
                "message": a.message,
                "since": hhmm(a.since),
            }
            for a in alerts
        ],
        "forecast": _forecast(weather, config, now),
        "hush": None if hush is None else hhmm(hush.until),
        # The one thing on this page that overrides everything above it. A
        # silenced boat still draws a calm plot and a green verdict, and
        # without this the picture would be a lie by omission.
        "silence": (
            None
            if silence is None
            else {
                "since": hhmm(silence.since),
                "held_for": silence.held_for(now),
                "note": silence.note,
                "text": silence.describe(now),
            }
        ),
        "paths": len(state),
        "deltas": state.deltas_seen,
    }


def _distance(metres: float) -> str:
    """A distance a person reads at a glance.

    Metres out to a kilometre, because that is the range an anchor watch lives
    in and every metre of it matters. Past that the number has stopped being
    about the anchor and started being about something being wrong, and
    "8123 km" says that where "8123456 m" just looks like line noise.
    """
    if metres < 1000:
        return f"{metres:.0f} m"
    if metres < 100_000:
        return f"{metres / 1000:.1f} km"
    return f"{metres / 1000:.0f} km"


# Steepness, roughly. A wave gets dangerous by being short for its height, not
# by being tall: the same two metres is a swell at nine seconds and a wall at
# four. This is the ratio a sailor already carries in their head, named.
STEEP_RATIO = 0.055


def _sea(forecast: Any, config: Any, now: datetime) -> dict[str, Any] | None:
    """The worst of the sea in the outlook, and whether it will be steep."""
    outlook = forecast.outlook(config.anchor_outlook_hours, now)
    worst = outlook.roughest
    if worst is None or worst.wave_m is None:
        return None

    period = worst.wave_period_s
    steep = period is not None and period > 0 and worst.wave_m / (period * period) > STEEP_RATIO
    return {
        "height_m": round(worst.wave_m, 1),
        "period_s": None if period is None else round(period, 1),
        "at": hhmm(worst.time),
        "steep": steep,
    }


# Above this the bank is being charged rather than merely floating. Matches
# CHARGING_CURRENT_A in rules.py, and for the same reason: a tenth of an amp is
# noise, not a charge.
CHARGING_A = 1.0
PANEL_ACTIVE_W = 5.0
JOULES_PER_WH = 3600.0


def _battery(state: Any) -> dict[str, Any]:
    """The house bank, as far as this boat can currently see it.

    Which is not very far, and the wording has to carry that. The only voltage
    available is the MPPT's own battery-side reading: it is charger output
    while the sun is up and absent at night, so it is a good proxy for
    "something is badly wrong" and a poor one for state of charge. There is
    no real SOC without a battery monitor such as a SmartShunt, and a number that looks
    authoritative would be worse than one that says what it is.

    An absent reading is reported rather than omitted. A status message with no
    battery line reads as "the bank is fine", and it means "I have no idea".
    """
    volts = _reading(state, "electrical.solar.mppt.voltage")
    amps = _reading(state, "electrical.solar.mppt.current")
    solar = _reading(state, "electrical.solar.mppt.panelPower")
    made = _reading(state, "electrical.solar.mppt.yieldToday")
    mode = state.value("electrical.solar.mppt.chargingMode")

    block: dict[str, Any] = {
        "volts": None if volts is None else round(volts, 2),
        "amps": None if amps is None else round(amps, 1),
        "solar_w": None if solar is None else round(solar),
        "yield_wh": None if made is None else round(made / JOULES_PER_WH),
        "mode": mode if isinstance(mode, str) else None,
    }

    if volts is None:
        block["text"] = "no reading from the MPPT over Bluetooth"
        block["charging"] = None
        return block

    charging = (amps is not None and amps > CHARGING_A) or (
        solar is not None and solar > PANEL_ACTIVE_W
    )
    block["charging"] = charging

    said = f"Bank {volts:.2f} V (MPPT, rough)"
    if charging:
        parts = []
        if amps is not None:
            parts.append(f"{amps:.1f} A")
        if solar is not None:
            parts.append(f"{solar:.0f} W from the panels")
        said += ", charging" + (" at " + ", ".join(parts) if parts else "")
    else:
        said += ", not charging"
    if block["yield_wh"]:
        said += f", {block['yield_wh']} Wh today"
    block["text"] = said + "."
    return block


def _gps(state: Any, config: Any) -> dict[str, Any]:
    """Where the boat is, or which kind of not-knowing this is.

    Three answers, not two. A receiver that has never said anything, one that
    has stopped saying anything, and one still talking with no fix to report
    are different faults with different fixes - a cable, a crashed daemon, a
    view of the sky - and collapsing them into "no position" throws away the
    only clue on offer. The same distinction anchor_watch_blind draws, in the
    same words, because they should not disagree.
    """
    age = state.age("navigation.position")
    if age is None:
        return {"fix": False, "text": "no GPS position: nothing has ever reported one"}

    # Staleness before validity, and it has to be this way round. A fix from
    # fifteen minutes ago is a real position and reads like a current one, and
    # showing it as current is how a boat that has dragged half a mile looks
    # like a boat sitting quietly on its anchor.
    if age > config.stale_after:
        return {
            "fix": False,
            "text": f"no GPS position: the receiver stopped reporting {age / 60:.0f} min ago",
        }

    raw = state.value("navigation.position")
    if as_position(raw) is None:
        return {"fix": False, "text": "no GPS position: the receiver is reporting, but has no fix"}
    # Degrees and decimal minutes, the same as everywhere else a person reads a
    # position here. Two formats for one number is how a plotter and a log book
    # end up disagreeing at the one moment it matters.
    return {"fix": True, "text": format_position_ddm(raw), "age_s": round(age)}


def _forecast(weather: Any, config: Any, now: datetime) -> dict[str, Any]:
    """The outlook, or an honest word about why there is not one."""
    if weather is None:
        return {"summary": "the forecast is turned off", "hours": None}

    missing = weather.explain(7200.0, now)
    if missing is not None:
        return {"summary": missing, "hours": None}

    forecast = weather.fresh(7200.0, now)
    outlook = forecast.outlook(config.anchor_outlook_hours, now)
    current = forecast.current(now)
    return {
        "summary": outlook.summary(),
        "hours": outlook.hours,
        # Everything the graph draws: the trace, the hours that will be dark,
        # and the lines the forecast rule would speak up at. The thresholds
        # travel with the data so the page never carries its own copy of a
        # number that lives in rules.py.
        "series": forecast.series(GRAPH_HOURS, now),
        "dark": forecast.dark_spans(GRAPH_HOURS, now),
        "graph_hours": GRAPH_HOURS,
        "warn_wind": FORECAST_WIND_WARN_MS,
        "warn_gust": FORECAST_GUST_WARN_MS,
        "sea": _sea(forecast, config, now),
        "dark_for": forecast.dark_for(now),
        "now": (
            None
            if current is None or current.wind_ms is None
            else f"{knots(current.wind_ms):.0f} kn "
            f"{cardinal(current.direction_rad) or ''}".strip()
        ),
        "age_min": round(forecast.age(now) / 60),
    }


def write_status(path: Path, payload: dict[str, Any]) -> None:
    """Publish the payload to a file, for anything that is not this process.

    The console reads this. It means `boat` shows the boat instantly, with no
    Signal K connection of its own and nothing to wait for, and it works over
    SSH on a link too poor to hold a websocket open.

    Written beside and renamed over, so a reader never catches half a file, and
    never fatal: a status file that cannot be written is not a reason to stop
    monitoring.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, default=str), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        log.debug("could not write the status file at %s: %s", path, exc)


def read_status(path: Path) -> dict[str, Any] | None:
    """The last payload the agent published, or None if there is not one."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# ------------------------------------------------------------------ actions --


class Actions:
    """What a tap can do. Each one writes the same file the CLI writes.

    Returns (ok, message) rather than raising, because every one of these is
    reported straight back to somebody standing on a foredeck.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.anchor = AnchorFile(config.anchor_file)
        self.hush = HushFile(config.hush_file)
        self.silence = SilenceFile(config.silence_file)

    def anchor_down(
        self,
        position: tuple[float, float] | None,
        radius_m: float,
        note: str = "set from the page",
    ) -> tuple[bool, str]:
        if position is None:
            return False, "no position, so the anchor cannot be set from here"
        if not MIN_RADIUS_M <= radius_m <= MAX_RADIUS_M:
            return False, f"the circle must be between {MIN_RADIUS_M:.0f} and {MAX_RADIUS_M:.0f} m"

        written = self.anchor.write(
            AnchorFix(
                latitude=position[0],
                longitude=position[1],
                radius_m=radius_m,
                set_at=datetime.now(UTC),
                # Which doorway it came through, because the anchor file is
                # read by a person as often as by the agent.
                note=note,
            )
        )
        if not written:
            return False, "could not write the anchor file"
        return True, f"watch set, {radius_m:.0f} m circle"

    def anchor_up(self) -> tuple[bool, str]:
        if self.anchor.read() is None:
            return True, "no watch was set"
        if not self.anchor.write(None):
            return False, "could not clear the anchor file"
        return True, "watch cleared"

    def hush_for(self, minutes: float) -> tuple[bool, str]:
        if not 0 < minutes <= MAX_HUSH_MINUTES:
            return False, f"a hush runs from 1 to {MAX_HUSH_MINUTES:.0f} minutes"
        entry = hush_until(minutes)
        if not self.hush.write(entry):
            return False, "could not write the hush file"
        return True, f"speaker quiet until {hhmm(entry.until)}"

    def unhush(self) -> tuple[bool, str]:
        if self.hush.active() is None:
            return True, "the speaker was not hushed"
        if not self.hush.write(None):
            return False, "could not clear the hush file"
        return True, "the speaker may sound again"

    def silence_all(self, note: str = "silenced from the page") -> tuple[bool, str]:
        """Every channel off, until somebody turns them back on.

        The message says the whole truth rather than "done", because this is
        the one action here that leaves the boat unable to tell anybody
        anything, and it is read by somebody who is about to walk away.
        """
        already = self.silence.active()
        if already is not None:
            return True, f"already silenced, {already.held_for()} ago"
        if not self.silence.write(silence_now(note)):
            return False, "could not write the silence file"
        return True, "silenced: no alarm will reach you until you turn them back on"

    def unsilence(self) -> tuple[bool, str]:
        held = self.silence.active()
        if held is None:
            return True, "the alarms were already on"
        if not self.silence.write(None):
            return False, "could not clear the silence file"
        # Whatever is standing gets announced again by the agent on its next
        # tick, so this promise is one the boat actually keeps.
        return True, f"alarms back on after {held.held_for()}"


# ------------------------------------------------------------------- server --


class Dashboard:
    """The one thing the agent and the HTTP thread share: a finished dict.

    Swapped by reference on every rule tick, which is atomic, so the handler
    never sees a half-built payload and never holds a lock the agent could
    block on.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.actions = Actions(config)
        self._payload: dict[str, Any] = {"state": "starting", "readings": [], "alerts": []}
        self.position: tuple[float, float] | None = None

    def publish(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        fix = payload.get("fix")
        self.position = (float(fix[0]), float(fix[1])) if fix else None

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload


class _Handler(BaseHTTPRequestHandler):
    server_version = "boat-agent"
    dashboard: Dashboard

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler writes to stderr by default, which on a
        # systemd unit means every poll from every phone in the journal.
        log.debug("ui: " + fmt, *args)

    # ---------------------------------------------------------------- auth --

    def _authorised(self) -> bool:
        token = self.dashboard.config.ui_token
        if not token:
            return True
        given = self.headers.get("X-Boat-Token") or ""
        if not given and "?" in self.path:
            # The link people scan carries it in the URL; fetch() then sends the
            # header. Both work, so a bookmark keeps working.
            given = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
        # Constant time, because this is reachable from the boat's network.
        return secrets.compare_digest(given, token)

    # ------------------------------------------------------------- replies --

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    # --------------------------------------------------------------- routes --

    def do_GET(self) -> None:  # the stdlib chooses this name
        path = self.path.split("?", 1)[0]
        if not self._authorised():
            self._json(401, {"error": "a token is needed"})
            return

        if path == "/":
            try:
                body = PAGE.read_bytes()
            except OSError as exc:
                self._json(500, {"error": f"the page is missing: {exc}"})
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, self.dashboard.payload)
        else:
            self._json(404, {"error": "no such page"})

    def do_POST(self) -> None:  # the stdlib chooses this name
        path = self.path.split("?", 1)[0]
        if not self._authorised():
            self._json(401, {"error": "a token is needed"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._json(400, {"error": "that was not JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "that was not JSON"})
            return

        actions = self.dashboard.actions
        try:
            if path == "/api/anchor/down":
                radius = float(body.get("radius_m", DEFAULT_RADIUS_M))
                ok, message = actions.anchor_down(self.dashboard.position, radius)
            elif path == "/api/anchor/up":
                ok, message = actions.anchor_up()
            elif path == "/api/hush":
                ok, message = actions.hush_for(float(body.get("minutes", 30)))
            elif path == "/api/unhush":
                ok, message = actions.unhush()
            elif path == "/api/silence":
                ok, message = actions.silence_all()
            elif path == "/api/unsilence":
                ok, message = actions.unsilence()
            else:
                self._json(404, {"error": "no such action"})
                return
        except (TypeError, ValueError):
            self._json(400, {"error": "that is not a number"})
            return
        except Exception as exc:  # pragma: no cover - a bug must not kill the thread
            log.exception("ui action %s failed", path)
            self._json(500, {"error": str(exc)})
            return

        self._json(200 if ok else 400, {"ok": ok, "message": message})


class UIServer:
    """The HTTP server on its own daemon thread. Never fatal."""

    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard
        self.config = dashboard.config
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = self.config.ui_bind
        shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
        suffix = f"/?t={self.config.ui_token}" if self.config.ui_token else "/"
        return f"http://{shown}:{self.config.ui_port}{suffix}"

    def start(self) -> bool:
        handler = type("_BoundHandler", (_Handler,), {"dashboard": self.dashboard})
        try:
            self._server = ThreadingHTTPServer((self.config.ui_bind, self.config.ui_port), handler)
        except OSError as exc:
            log.error(
                "the page could not start on %s:%s (%s). Everything else carries on.",
                self.config.ui_bind,
                self.config.ui_port,
                exc,
            )
            return False

        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def build_ui(config: Any) -> tuple[Dashboard, UIServer] | None:
    """Make the dashboard and its server, or None if it is turned off."""
    if not getattr(config, "ui", False):
        return None
    dashboard = Dashboard(config)
    return dashboard, UIServer(dashboard)


def warn_if_open(config: Any) -> None:
    """Say something if this is reachable from the network with no token."""
    if config.ui_bind in LOCAL_HOSTS or config.ui_token:
        return
    log.warning(
        "the page is bound to %s with no AGENT_UI_TOKEN set, so anyone on this "
        "network can arm and clear the anchor watch. Set a token.",
        config.ui_bind,
    )


# Left to the caller rather than generated here, so the token in .env is the
# only one there is and it survives a restart. A token that changed every boot
# would mean re-scanning a QR code every time the boat rebooted.
def suggest_token() -> str:
    return secrets.token_urlsafe(12)

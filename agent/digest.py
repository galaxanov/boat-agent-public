"""The day's recap, read back out of the logbook and kept as markdown.

The hourly summary in llm.py describes this moment: what the instruments say
now. This is the other thing - what happened while you were asleep. It reads
the last 24 hours of daily log, works out the figures that matter on a boat
(how low the bank got, how much the sun made, how far the boat swung, what
alarmed and whether it cleared), and adds one dated entry to a markdown file.

Newest entry first, because the file is opened to see last night rather than
last spring. Old entries fall off the bottom on the same retention as the logs
they were built from.

Three rules shape it:

1. It is built from the log, not from live state. Anything it claims can be
   checked against the file it came from, and a restart mid-window costs
   nothing because the record is on disk.
2. The figures are the whole product. Everything in an entry is computed
   from the log, so the boat writes its own record with no key, no account
   and nothing to reach over a satellite link.
3. Writing the file never raises, and never destroys what is already there:
   it is written beside and renamed over, so a power cut mid-write leaves
   yesterday's file rather than half of today's.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .derived import MOVING_SPEED_MS
from .geo import as_position, distance_m
from .units import cardinal, format_position, hhmm, knots, span, stamp

log = logging.getLogger(__name__)

KELVIN = 273.15
WINDOW_HOURS = 24.0

# Entries to keep in the markdown file. Matches the default log retention:
# the file should not outlive the logs its entries were computed from.
KEEP_ENTRIES = 90

TITLE = "# Ship's log"
ENTRY_RE = re.compile(r"^## ", re.MULTILINE)

# Paths worth a line in the recap, and how to say them.
VOLTAGE = "electrical.solar.mppt.voltage"
SOLAR_W = "electrical.solar.mppt.panelPower"
LOCKER_T = "environment.inside.locker.temperature"
CPU_T = "environment.rpi.cpu.temperature"
DEPTH = "environment.depth.belowTransducer"
POSITION = "navigation.position"
SOG = "navigation.speedOverGround"
STW = "navigation.speedThroughWater"
TRIP_LOG = "navigation.log"
ANCHOR = "navigation.anchor.position"

METRES_PER_NM = 1852.0

# Two fixes a minute apart while stopped differ by a few metres of GPS noise,
# and a boat swinging to its anchor moves for real without going anywhere.
# Legs are only counted when the boat says it is making way, and short ones
# are dropped, so a night at anchor does not accumulate a passage.
MIN_LEG_M = 15.0


def _parse(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _at(value: Any) -> datetime | None:
    """An ISO stamp out of the log, as an aware datetime. None if unreadable."""
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _when(record: dict[str, Any]) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(record["ts"]))
    except (KeyError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def read_window(log_dir: Path, now: datetime, hours: float = WINDOW_HOURS) -> list[dict[str, Any]]:
    """Every record from the last `hours`, oldest first.

    Reads both the plain and the gzipped form, because rotation compresses a
    day the moment the next one opens and a 0700 digest always spans midnight.
    """
    start = now - timedelta(hours=hours)
    records: list[dict[str, Any]] = []

    day = start.date()
    while day <= now.date():
        stem = day.strftime("%Y-%m-%d")
        for path, opener in (
            (Path(log_dir) / f"{stem}.jsonl", open),
            (Path(log_dir) / f"{stem}.jsonl.gz", gzip.open),
        ):
            if not path.is_file():
                continue
            try:
                with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
                    for line in handle:
                        record = _parse(line)
                        when = _when(record) if record else None
                        if record is not None and when is not None and start <= when <= now:
                            records.append(record)
            except OSError as exc:
                log.warning("could not read %s for the digest: %s", path.name, exc)
        day += timedelta(days=1)

    records.sort(key=lambda r: str(r.get("ts", "")))
    return records


@dataclass
class Extremes:
    """Lowest and highest a reading got, and when."""

    low: float | None = None
    high: float | None = None
    low_at: datetime | None = None
    high_at: datetime | None = None

    def add(self, value: float, when: datetime | None) -> None:
        if self.low is None or value < self.low:
            self.low, self.low_at = value, when
        if self.high is None or value > self.high:
            self.high, self.high_at = value, when

    @property
    def seen(self) -> bool:
        return self.low is not None


@dataclass
class Anchorage:
    """One spell at anchor, from the log's point of view."""

    position: tuple[float, float] | None = None
    since: datetime | None = None
    until: datetime | None = None
    swing_m: float = 0.0

    @property
    def hours(self) -> float | None:
        if self.since is None or self.until is None:
            return None
        return (self.until - self.since).total_seconds() / 3600.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": (
                format_position({"latitude": self.position[0], "longitude": self.position[1]})
                if self.position
                else None
            ),
            "from": self.since.isoformat(timespec="seconds") if self.since else None,
            "to": self.until.isoformat(timespec="seconds") if self.until else None,
            "hours": round(self.hours, 1) if self.hours is not None else None,
            "swing_m": round(self.swing_m, 1),
        }


@dataclass
class DayFacts:
    """Everything the recap is allowed to say, computed from the log."""

    start: datetime
    end: datetime
    snapshots: int = 0
    restarts: int = 0
    disconnects: int = 0
    voltage: Extremes = field(default_factory=Extremes)
    solar_w: Extremes = field(default_factory=Extremes)
    locker_c: Extremes = field(default_factory=Extremes)
    cpu_c: Extremes = field(default_factory=Extremes)
    depth_m: Extremes = field(default_factory=Extremes)
    swing_m: float | None = None
    travelled_m: float = 0.0
    trip_log: Extremes = field(default_factory=Extremes)
    anchorages: list[Anchorage] = field(default_factory=list)
    states: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    # How much of the window had every alert channel switched off, and whether
    # it still is. The one figure here that is about the boat's crew rather
    # than the boat, and it belongs in the entry because it is the only thing
    # that can make every alarm line above it something nobody ever saw.
    silenced_s: float = 0.0
    still_silenced: bool = False
    silence_notes: list[str] = field(default_factory=list)
    # The last forecast fetched in the window. The only thing in this class
    # that is not a fact about the past, and it is kept apart from the rest for
    # exactly that reason: everything else here was measured, and this was not.
    forecast: dict[str, Any] | None = None

    @property
    def distance_nm(self) -> tuple[float, str] | None:
        """How far the boat went, and how confident that number is.

        The instrument trip log is a measurement and wins when it is on the bus. The
        fix-to-fix sum is an estimate: it undercounts a tacking passage, since
        it joins hourly positions with straight lines.
        """
        if self.trip_log.seen and (self.trip_log.high or 0) > (self.trip_log.low or 0):
            run = (self.trip_log.high or 0) - (self.trip_log.low or 0)
            return run / METRES_PER_NM, "trip log"
        if self.travelled_m >= MIN_LEG_M:
            return self.travelled_m / METRES_PER_NM, "estimated from fixes"
        return None

    @property
    def has_data(self) -> bool:
        return self.snapshots > 0 or bool(self.alerts) or bool(self.states)

    @property
    def unresolved(self) -> list[dict[str, Any]]:
        """Alarms that were raised in the window and never cleared in it."""
        cleared = {a["rule"] for a in self.alerts if a["event"] == "alert_cleared"}
        seen: dict[str, dict[str, Any]] = {}
        for entry in self.alerts:
            if entry["event"] != "alert_cleared" and entry["rule"] not in cleared:
                seen.setdefault(entry["rule"], entry)
        return list(seen.values())

    def as_dict(self) -> dict[str, Any]:
        """The brief handed to the model. SI in, plain names out."""

        def span(extremes: Extremes) -> dict[str, Any] | None:
            if not extremes.seen:
                return None
            return {"low": round(extremes.low or 0, 2), "high": round(extremes.high or 0, 2)}

        return {
            "window": {
                "from": self.start.isoformat(timespec="seconds"),
                "to": self.end.isoformat(timespec="seconds"),
            },
            "note": (
                "Computed from the boat's own log, not from live instruments. "
                "Absent figures mean the instrument reported nothing all window, "
                "not that the value was zero. Volts and watts as given, "
                "temperatures already converted to Celsius, distances in metres. "
                "forecast_for_the_hours_ahead is the exception: a forecast for "
                "what is coming, in SI, not a measurement of what happened."
            ),
            "snapshots": self.snapshots,
            "agent_restarts": self.restarts,
            "signalk_disconnects": self.disconnects,
            "house_voltage_v": span(self.voltage),
            "solar_w": span(self.solar_w),
            "locker_c": span(self.locker_c),
            "pi_cpu_c": span(self.cpu_c),
            "depth_below_transducer_m": span(self.depth_m),
            "furthest_from_first_fix_m": (
                round(self.swing_m, 1) if self.swing_m is not None else None
            ),
            "distance_run_nm": (
                {"value": round(self.distance_nm[0], 1), "source": self.distance_nm[1]}
                if self.distance_nm
                else None
            ),
            "anchorages": [a.as_dict() for a in self.anchorages],
            "state_changes": self.states,
            "alerts": self.alerts,
            "alerts_still_active": [a["rule"] for a in self.unresolved],
            # Say it plainly enough that a summary cannot describe a quiet
            # night when what actually happened is that nobody was told.
            "alerts_silenced_seconds": round(self.silenced_s),
            "alerts_still_silenced": self.still_silenced,
            "why_silenced": self.silence_notes,
            # Named so it cannot be mistaken for a reading. It is a model's
            # opinion about the hours after this entry, not something that
            # happened during the window.
            "forecast_for_the_hours_ahead": self.forecast,
        }


def _reading(record: dict[str, Any], path: str) -> float | None:
    entry = record.get("values", {}).get(path)
    if not isinstance(entry, dict) or entry.get("stale"):
        return None
    value = entry.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def summarise_day(records: list[dict[str, Any]], start: datetime, end: datetime) -> DayFacts:
    """Fold the log into the figures worth reporting."""
    facts = DayFacts(start=start, end=end)
    first_fix: tuple[float, float] | None = None
    previous_fix: tuple[float, float] | None = None
    riding: Anchorage | None = None
    # When the current run of silence began, clamped into the window. A silence
    # that started last week counts from the start of this window, because that
    # is all this entry is entitled to claim.
    silent_since: datetime | None = None

    for record in records:
        kind = record.get("type")
        when = _when(record)

        if kind == "event":
            event = record.get("event")
            if event == "agent_started":
                facts.restarts += 1
            elif event == "signalk_disconnected":
                facts.disconnects += 1
            elif event == "silenced":
                # Written repeatedly while it stands, not only when it is set,
                # so a silence that outlasts a whole window still leaves a mark
                # inside it and cannot go unreported.
                if silent_since is None:
                    began = _at(record.get("since")) or when
                    silent_since = max(began, start) if began else start
                note = str(record.get("note") or "").strip()
                if note and note not in facts.silence_notes:
                    facts.silence_notes.append(note)
            elif event == "unsilenced" and silent_since is not None:
                facts.silenced_s += ((when or end) - silent_since).total_seconds()
                silent_since = None

        elif kind == "state":
            facts.states.append(
                {
                    "at": str(record.get("ts")),
                    "from": record.get("from"),
                    "to": record.get("to"),
                    "reason": record.get("reason"),
                }
            )

        elif kind == "alert":
            facts.alerts.append(
                {
                    "at": str(record.get("ts")),
                    "event": record.get("event"),
                    "rule": record.get("rule"),
                    "severity": record.get("severity"),
                    "message": record.get("message"),
                }
            )

        elif kind == "forecast":
            # The last one in the window wins: an entry written at 0600 should
            # quote the forecast the boat holds at 0600, not the one it held a
            # day ago.
            facts.forecast = {
                key: value
                for key, value in record.items()
                if key not in ("type", "ts", "requested", "grid", "hours")
            }

        elif kind == "snapshot":
            facts.snapshots += 1
            for path, target, scale in (
                (VOLTAGE, facts.voltage, None),
                (SOLAR_W, facts.solar_w, None),
                (DEPTH, facts.depth_m, None),
                (TRIP_LOG, facts.trip_log, None),
                (LOCKER_T, facts.locker_c, KELVIN),
                (CPU_T, facts.cpu_c, KELVIN),
            ):
                value = _reading(record, path)
                if value is not None:
                    target.add(value - scale if scale else value, when)

            fix = _position(record, POSITION)
            speed = _reading(record, SOG)
            if speed is None:
                speed = _reading(record, STW)

            if fix is not None:
                if first_fix is None:
                    first_fix = fix
                else:
                    moved = distance_m(first_fix, fix)
                    facts.swing_m = moved if facts.swing_m is None else max(facts.swing_m, moved)

                # Only count ground covered while the boat says it is moving.
                if previous_fix is not None and speed is not None and speed >= MOVING_SPEED_MS:
                    leg = distance_m(previous_fix, fix)
                    if leg >= MIN_LEG_M:
                        facts.travelled_m += leg
                previous_fix = fix

            riding = _track_anchorage(record, fix, when, riding, facts)

    if riding is not None:
        facts.anchorages.append(riding)
    # Still off when the window closed: count it to the end and say so, which
    # is the case that matters most - it means it is off right now too.
    if silent_since is not None:
        facts.silenced_s += (end - silent_since).total_seconds()
        facts.still_silenced = True
    return facts


def _position(record: dict[str, Any], path: str) -> tuple[float, float] | None:
    entry = record.get("values", {}).get(path)
    return as_position(entry.get("value")) if isinstance(entry, dict) else None


def _track_anchorage(
    record: dict[str, Any],
    fix: tuple[float, float] | None,
    when: datetime | None,
    riding: Anchorage | None,
    facts: DayFacts,
) -> Anchorage | None:
    """Open, extend or close the current spell at anchor.

    Anchored means the state model said so, or the anchor plugin is publishing
    a position - either is enough, and neither is guessed at from speed alone.
    """
    anchor = _position(record, ANCHOR)
    derived = record.get("derived")
    state = derived.get("state") if isinstance(derived, dict) else None
    anchored = anchor is not None or state == "anchored"

    if not anchored:
        if riding is not None:
            facts.anchorages.append(riding)
        return None

    if riding is None:
        riding = Anchorage(position=anchor or fix, since=when, until=when)
    if riding.position is None:
        riding.position = anchor or fix
    riding.until = when or riding.until
    if riding.position is not None and fix is not None:
        riding.swing_m = max(riding.swing_m, distance_m(riding.position, fix))
    return riding


# ------------------------------------------------------------------ output --


def _anchorage_line(spot: Anchorage) -> str:
    """Where the boat lay, for how long, and how far it wandered while there."""
    where = (
        format_position({"latitude": spot.position[0], "longitude": spot.position[1]})
        if spot.position
        else "position not recorded"
    )
    when = ""
    if spot.since and spot.until:
        hours = spot.hours or 0.0
        when = f" from {hhmm(spot.since)} to {hhmm(spot.until)}"
        when += f" ({hours:.1f} h)" if hours >= 0.1 else ""
    swing = f", swinging up to {spot.swing_m:.0f} m" if spot.swing_m >= 1 else ""
    return f"{where}{when}{swing}"


def _plural(count: int, noun: str) -> str:
    """A heading a person reads should not say "1 alarm(s)"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _row(label: str, extremes: Extremes, unit: str, places: int = 1) -> str | None:
    """A table row, or nothing at all if that instrument never reported."""
    if not extremes.seen:
        return None
    low, high = extremes.low or 0.0, extremes.high or 0.0
    return f"| {label} | {low:.{places}f} {unit} | {high:.{places}f} {unit} |"


def _silence_line(facts: DayFacts) -> str | None:
    """One sentence about the hours the boat could not reach anybody."""
    if facts.silenced_s <= 0:
        return None
    held = span(facts.silenced_s)
    why = f" ({'; '.join(facts.silence_notes)})" if facts.silence_notes else ""
    if facts.still_silenced:
        return (
            f"**Alerts were silenced for {held} of this window and still are**{why}. "
            "Anything raised in that time reached nobody. Turn them back on with "
            "`boat --unsilence`."
        )
    return (
        f"**Alerts were silenced for {held} of this window**{why}. Anything raised "
        "in that time reached nobody, though it is all still logged below."
    )


def _forecast_line(forecast: dict[str, Any]) -> str | None:
    """What is coming, in one sentence, or nothing if there is no forecast.

    Kept in the future tense and given its own heading, because everything else
    in the entry is a measurement and this is not.
    """
    peak = forecast.get("max_wind") or forecast.get("now")
    if not isinstance(peak, dict):
        return None

    wind = peak.get("wind_ms")
    if not isinstance(wind, (int, float)):
        return None

    point = cardinal(peak.get("direction_rad"))
    lead = f"up to {knots(float(wind)):.0f} kn"
    parts = [f"{lead} from the {point}" if point else lead]

    gust_hour = forecast.get("max_gust") or peak
    gust = gust_hour.get("gust_ms") if isinstance(gust_hour, dict) else None
    if isinstance(gust, (int, float)):
        parts.append(f"gusting {knots(float(gust)):.0f} kn")

    wave_hour = forecast.get("max_wave")
    wave = wave_hour.get("wave_m") if isinstance(wave_hour, dict) else None
    if isinstance(wave, (int, float)):
        parts.append(f"sea to {float(wave):.1f} m")

    hours = forecast.get("outlook_h")
    ahead = f"next {float(hours):.0f} h" if isinstance(hours, (int, float)) else "hours ahead"
    return f"{ahead}: " + ", ".join(parts)


def render_entry(facts: DayFacts) -> str:
    """One dated markdown section, newest-first when prepended to the file."""
    # The heading is read, not parsed - _split_entries only looks for "## " -
    # so it can be local. The figures underneath were computed from a log that
    # is UTC end to end, and stay comparable whatever zone the boat is in.
    heading = stamp(facts.end)
    unresolved = facts.unresolved
    if unresolved:
        heading += f" - {_plural(len(unresolved), 'alarm')} still active"
    if facts.still_silenced:
        heading += " - ALERTS SILENCED"
    lines = [f"## {heading}", ""]

    if not facts.has_data:
        lines.append(
            "Nothing in the log for the last 24 hours. Either the agent was not "
            "running, or it was running with nothing reaching it."
        )
        return "\n".join(lines) + "\n"

    # Above the figures, because it changes what all of them mean. An entry
    # that lists three alarms without saying nobody was told about them is a
    # record of the boat and not of the watch.
    banner = _silence_line(facts)
    if banner:
        lines += [banner, ""]

    rows = [
        _row("House voltage", facts.voltage, "V", 2),
        _row("Solar", facts.solar_w, "W", 0),
        _row("Depth below transducer", facts.depth_m, "m"),
        _row("Nav locker", facts.locker_c, "C"),
        _row("Pi CPU", facts.cpu_c, "C"),
    ]
    rows = [r for r in rows if r]
    if rows:
        lines += ["| reading | low | high |", "| --- | --- | --- |", *rows, ""]

    run = facts.distance_nm
    if run is not None:
        lines += [f"Distance run: {run[0]:.1f} NM ({run[1]})", ""]

    if facts.anchorages:
        lines.append("**Anchored**")
        for spot in facts.anchorages:
            lines.append(f"- {_anchorage_line(spot)}")
        lines.append("")
    elif facts.swing_m is not None:
        lines += [f"Furthest from the first fix of the window: {facts.swing_m:.0f} m", ""]

    if facts.alerts:
        lines.append("**Alarms**")
        lines += [
            f"- {hhmm(a['at'])} {str(a['event']).replace('alert_', '')} - {a['message']}"
            for a in facts.alerts
        ]
        lines.append("")
    else:
        lines += ["No alarms.", ""]

    if facts.states:
        lines.append("**State**")
        lines += [
            f"- {hhmm(c['at'])} {c['from']} to {c['to']} ({c['reason']})"
            for c in facts.states
        ]
        lines.append("")

    # Last, and under a heading of its own. Everything above it was measured
    # and this was not, and an entry read at breakfast should not be able to
    # blur the two.
    if facts.forecast:
        line = _forecast_line(facts.forecast)
        if line:
            lines += ["**Forecast**", f"- {line}", ""]

    housekeeping = [_plural(facts.snapshots, "snapshot")]
    if facts.restarts > 1:
        housekeeping.append(f"{facts.restarts} agent starts")
    if facts.disconnects:
        housekeeping.append(_plural(facts.disconnects, "Signal K disconnect"))
    lines.append(", ".join(housekeeping) + ".")

    return "\n".join(lines) + "\n"


def _split_entries(text: str) -> list[str]:
    """Existing entries, newest first, without the file header."""
    marks = [m.start() for m in ENTRY_RE.finditer(text)]
    if not marks:  # a new file, or one with only the header
        return []
    bounds = zip(marks, [*marks[1:], len(text)], strict=True)
    return [text[a:b].rstrip() + "\n" for a, b in bounds]


def update_file(
    path: Path,
    entry: str,
    keep: int = KEEP_ENTRIES,
    header: str = "",
) -> bool:
    """Put a new entry at the top of the markdown file. Never raises.

    The whole file is rewritten each time rather than appended to, because the
    newest entry belongs at the top and old ones have to fall off the bottom.
    It is small: ninety entries of a few hundred bytes.
    """
    path = Path(path)
    header = header or (
        f"{TITLE}\n\nWritten by the boat agent, once a day, from its own log. "
        "Newest entry first.\n"
    )

    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as exc:
        log.warning("cannot read %s, starting a fresh one: %s", path, exc)
        existing = ""

    entries = [entry.rstrip() + "\n", *_split_entries(existing)]
    body = header.rstrip() + "\n\n" + "\n".join(entries[:keep])

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        log.error("could not write the ship's log at %s: %s", path, exc)
        return False

    log.info("ship's log updated: %s", path)
    return True


def build_digest(
    log_dir: Path,
    now: datetime | None = None,
    hours: float = WINDOW_HOURS,
) -> DayFacts:
    now = now or datetime.now(UTC)
    start = now - timedelta(hours=hours)
    return summarise_day(read_window(log_dir, now, hours), start, now)

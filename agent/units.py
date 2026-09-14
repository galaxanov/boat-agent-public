"""SI to human, for display only.

Nothing in the state model or the logs is converted - Signal K is SI end to end
and it stays that way. This module exists so the line in journalctl is readable
by a person standing at the nav station.

Time works the same way, and for the same reason. Everything stored is UTC:
the daily log, the anchor file, the timestamps a rule reasons about. A boat
crosses time zones and lays up in another country, and a log in local time is a
log you cannot compare with itself six months later.

But nobody stands a watch in UTC. "The wind gets up at 03:00" has to mean three
in the morning where the boat actually is, or the crew does arithmetic at the
one moment they should not have to. So every time a person reads is converted
here, on the way out, and always carries its zone: a bare "03:00" on a boat
that has just sailed from Spain to Portugal is a trap.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MS_TO_KNOTS = 1.9438444924406046


def knots(ms: float | None) -> float | None:
    return None if ms is None else ms * MS_TO_KNOTS


def degrees(radians: float | None) -> float | None:
    return None if radians is None else math.degrees(radians)


def compass(radians: float | None) -> float | None:
    """Radians to a 0-360 bearing."""
    deg = degrees(radians)
    return None if deg is None else deg % 360


def relative_degrees(radians: float | None) -> float | None:
    """Radians to -180..180, for wind angles where the sign is the useful bit."""
    deg = degrees(radians)
    return None if deg is None else (deg + 180) % 360 - 180


def celsius(kelvin: float | None) -> float | None:
    return None if kelvin is None else kelvin - 273.15


COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip


def cardinal(radians: float | None) -> str | None:
    """Radians to a compass point, to sixteenths.

    For sentences rather than numbers. "from the NNE" is what a forecast means
    to somebody deciding whether the cove is a lee shore tonight; 27 degrees is
    a precision the model does not have anyway.
    """
    deg = compass(radians)
    if deg is None:
        return None
    return COMPASS_POINTS[int((deg + 11.25) % 360 // 22.5)]


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def format_position(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    lat, lon = _number(value.get("latitude")), _number(value.get("longitude"))
    if lat is None or lon is None:
        return None
    return f"{abs(lat):.5f}{'N' if lat >= 0 else 'S'} {abs(lon):.5f}{'E' if lon >= 0 else 'W'}"


def format_position_ddm(value: Any) -> str | None:
    """Degrees and decimal minutes, which is what a boat actually reads.

    36 49.872\'N 10 18.204\'E. Every plotter, every almanac and every entry in
    a paper log is written this way; decimal degrees is a convenience for
    machines and looks wrong on a chart table. The raw floats stay in the log.
    """
    if not isinstance(value, dict):
        return None
    lat, lon = _number(value.get("latitude")), _number(value.get("longitude"))
    if lat is None or lon is None:
        return None

    def part(figure: float, positive: str, negative: str, width: int) -> str:
        degrees_whole = int(abs(figure))
        minutes = (abs(figure) - degrees_whole) * 60
        hemisphere = positive if figure >= 0 else negative
        return f"{degrees_whole:0{width}d}\u00b0 {minutes:06.3f}\u2032 {hemisphere}"

    return f"{part(lat, 'N', 'S', 2)}  {part(lon, 'E', 'W', 3)}"


def format_status(state: Any) -> str:
    """A one-line summary of whatever the boat is currently reporting.

    Only shows paths that have actually produced a value, so on a bench with
    nothing connected it says so rather than printing a row of dashes.
    """
    parts: list[str] = []

    def add(label: str, value: float | None, fmt: str, suffix: str) -> None:
        if value is not None:
            parts.append(f"{label} {value:{fmt}}{suffix}")

    position = format_position(state.value("navigation.position"))
    if position:
        parts.append(position)

    add("SOG", knots(_number(state.value("navigation.speedOverGround"))), ".1f", "kn")
    add("COG", compass(_number(state.value("navigation.courseOverGroundTrue"))), ".0f", "°")
    add("HDG", compass(_number(state.value("navigation.headingMagnetic"))), ".0f", "°M")
    add("STW", knots(_number(state.value("navigation.speedThroughWater"))), ".1f", "kn")
    add("DBT", _number(state.value("environment.depth.belowTransducer")), ".1f", "m")
    add("AWS", knots(_number(state.value("environment.wind.speedApparent"))), ".1f", "kn")
    add(
        "AWA",
        relative_degrees(_number(state.value("environment.wind.angleApparent"))),
        "+.0f",
        "°",
    )
    add("SEA", celsius(_number(state.value("environment.water.temperature"))), ".1f", "°C")
    add("PV", _number(state.value("electrical.solar.mppt.panelPower")), ".0f", "W")
    add("BATT", _number(state.value("electrical.solar.mppt.voltage")), ".2f", "V")
    locker = _number(state.value("environment.inside.locker.temperature"))
    add("LOCKER", celsius(locker), ".1f", "°C")
    add("CPU", celsius(_number(state.value("environment.rpi.cpu.temperature"))), ".1f", "°C")

    if not parts:
        return "no data yet"
    return "  ".join(parts)


# ------------------------------------------------------------------- clocks --

# None means the machine's own zone, which on the nav laptop is boat time and
# is what the crew's watches are set to. AGENT_TIMEZONE overrides it, for the
# laptop that never got its zone changed after the delivery.
_zone: tzinfo | None = None


def set_display_timezone(name: str = "") -> str:
    """Choose the zone times are shown in. Returns what it settled on.

    Never raises. A name the system does not know is a typo in a config file,
    and a typo in a config file must not stop a boat being monitored - it falls
    back to the machine's own zone and says so.
    """
    global _zone
    if not name or name.strip().lower() in ("", "local", "system"):
        _zone = None
        return zone_name()
    try:
        _zone = ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _zone = None
        return ""
    return zone_name()


def zone_name(when: datetime | None = None) -> str:
    """What to call the zone right now, like EDT or EEST."""
    moment = local(when or datetime.now(UTC))
    return moment.strftime("%Z") if moment else ""


def local(when: Any, tz: tzinfo | None = None) -> datetime | None:
    """Anything time-shaped, in the zone a person is reading in.

    Accepts a datetime or an ISO string, because the ship's log reads its
    figures back out of a JSON file. A naive datetime is treated as UTC, which
    is what everything in this agent stamps.
    """
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return None
    if not isinstance(when, datetime):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(tz if tz is not None else _zone)


def hhmm(when: Any, tz: tzinfo | None = None) -> str:
    """"14:05 EEST". The time of day, and never without its zone."""
    moment = local(when, tz)
    return "" if moment is None else moment.strftime("%H:%M %Z").strip()


def dayhhmm(when: Any, tz: tzinfo | None = None) -> str:
    """"Tue 08 03:00" - for a column of hours, where the zone is in the header."""
    moment = local(when, tz)
    return "" if moment is None else moment.strftime("%a %d %H:%M")


def span(seconds: float) -> str:
    """How long, the way a person says it. "0.0 h" is not a length of time.

    Minutes while the answer is still counted in them, then hours, then days -
    an anchor watch is read in minutes and a boat left on the hard is read in
    days, and the same figure has to serve both without ever being "168 h".
    """
    minutes = max(0, round(seconds / 60))
    if minutes < 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h" if rest == 0 else f"{hours} h {rest:02d}"
    days, spare = divmod(hours, 24)
    return f"{days} days" if spare == 0 else f"{days} days {spare} h"


def stamp(when: Any, tz: tzinfo | None = None) -> str:
    """"2026-09-08 06:00 EDT". A whole moment, for a heading."""
    moment = local(when, tz)
    return "" if moment is None else moment.strftime("%Y-%m-%d %H:%M %Z").strip()

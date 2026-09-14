"""Alert rules.

Three things every rule here obeys, because getting them wrong is how a boat
alarm becomes something you learn to ignore:

1. Missing data is not a reading. A depth sensor that has stopped reporting
   must never read as "0 m, aground". Every rule checks staleness first and
   stays silent when it has nothing to go on.
2. Nothing fires on a single sample. A condition has to hold for `for_seconds`
   before it raises - one spurious depth ping off a thermocline is not an alarm.
3. Clearing is slower than raising, and thresholds have hysteresis, so a value
   sitting exactly on the line does not flap between alert and clear.

Thresholds live at the top of the file so they can be argued with in one place.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .geo import as_position, distance_m
from .paths import BUS_PATHS
from .state import BoatState
from .units import cardinal, knots
from .weather import (
    ANCHORED_OUTLOOK_HOURS,
    OUTLOOK_HOURS,
    Hour,
    WeatherStore,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- constants --

KELVIN = 273.15


def c_to_k(celsius: float) -> float:
    return celsius + KELVIN


# House bank: a 12V LiFePO4 bank with integral BMS.
#
# Read these as rough. Without a battery monitor such as a SmartShunt the only
# voltage available is the MPPT's own battery-side reading, which is charger
# output while the sun is up and absent at night. It is good enough to catch
# "something is badly wrong" and not good enough for state of charge.
HOUSE_VOLTAGE_LOW = 12.0  # ~10% SOC on a 12V LiFePO4 resting curve
HOUSE_VOLTAGE_CRITICAL = 11.8  # BMS cutoff territory
HOUSE_VOLTAGE_HIGH = 14.8  # above any sane charge setpoint

# LiFePO4 must not be charged below freezing - it plates lithium and the damage
# is permanent. The integral BMS should refuse, but the BMS is the last line of
# defence, not the first.
BATTERY_TEMP_MIN_CHARGE_C = 0.0
BATTERY_TEMP_MAX_C = 45.0
BATTERY_TEMP_MIN_C = -10.0
CHARGING_CURRENT_A = 1.0  # above this the bank is being charged

# The MPPT derates above 40C, and the nav station locker is not ventilated.
LOCKER_TEMP_WARN_C = 40.0
LOCKER_TEMP_ALARM_C = 55.0

# Pi 5 throttles at 80C and shuts down at 85C.
CPU_TEMP_WARN_C = 78.0
CPU_TEMP_ALARM_C = 84.0
DISK_FREE_WARN_BYTES = 2 * 1024**3
DISK_FREE_ALARM_BYTES = 512 * 1024**2

# Draft 2.0 m to the bottom of the keel.
DRAFT_M = 2.0

# The instrument reports depth below the TRANSDUCER, so this offset is what
# turns it into water under the keel. It is set to the full draft, which
# assumes the transducer sits at the waterline. It does not - it is somewhere
# below it - so this UNDERSTATES the clearance by however deep the transducer
# actually is, and the alarm fires early rather than late. Measure the
# transducer depth and reduce this to (draft - transducer depth) to recover the
# difference. Signal K can also supply it live as
# environment.depth.transducerToKeel, which wins over this value.
TRANSDUCER_TO_KEEL_M = DRAFT_M

# Clearance under the keel, which is the number that actually matters.
KEEL_CLEARANCE_WARN_M = 2.0
KEEL_CLEARANCE_ALARM_M = 1.0
UNDERWAY_SPEED_MS = 0.5  # ~1 knot

STARLINK_DOWN_SECONDS = 600.0
STARLINK_ONLINE_STATES = frozenset({"online", "connected", "ok", "up"})

BILGE_CYCLES_WINDOW_S = 3600.0
BILGE_CYCLES_WARN = 6  # a pump running this often means water is coming in

ANCHOR_DRAG_MARGIN_M = 10.0  # added to the set radius before crying drag

# How long the instruments may all say nothing before that is itself the news.
# Long enough that starting the agent before switching the panel on is not an
# alert, short enough to find out before dark.
BUS_SILENT_SECONDS = 900.0

# Forecast wind, in metres per second because that is what everything here
# speaks. The numbers are Beaufort converted, and they are pitched at a
# cruising yacht on one anchor in an open cove.
#
# F6 is where a night at anchor stops being a night's sleep and becomes a
# decision - more scope, an anchor watch taken seriously, or leave. F8 is where
# it has stopped being a decision. The gust thresholds sit a level higher than
# the sustained ones because gusts always run ahead of the mean and alerting on
# every afternoon acceleration zone would teach the crew to ignore this.
FORECAST_WIND_WARN_MS = 12.0  # F6, about 23 kn sustained
FORECAST_GUST_WARN_MS = 17.0  # about 33 kn in the gusts
FORECAST_WIND_STRONG_MS = 17.2  # F8, 34 kn: a gale
FORECAST_GUST_STRONG_MS = 22.0  # about 43 kn
FORECAST_WIND_HYSTERESIS_MS = 1.0

# Two fetch intervals. Older than this and the forecast is not a forecast.
FORECAST_MAX_AGE = 7200.0

# How stale a value may be before a rule treats it as absent. Generous, because
# 'instant' subscriptions only deliver when a source actually reports.
DEFAULT_MAX_AGE = 300.0


class Severity(StrEnum):
    """Signal K notification states, in increasing order of urgency."""

    NORMAL = "normal"
    ALERT = "alert"
    WARN = "warn"
    ALARM = "alarm"
    EMERGENCY = "emergency"


SEVERITY_ORDER = {
    Severity.NORMAL: 0,
    Severity.ALERT: 1,
    Severity.WARN: 2,
    Severity.ALARM: 3,
    Severity.EMERGENCY: 4,
}


@dataclass(frozen=True)
class Finding:
    """What a rule thinks right now. None from a rule means 'all well'."""

    severity: Severity
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Alert:
    """A finding that has held long enough to be worth telling someone about."""

    rule_id: str
    severity: Severity
    message: str
    since: datetime
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "severity": str(self.severity),
            "message": self.message,
            "since": self.since.isoformat(timespec="seconds"),
            **({"data": self.data} if self.data else {}),
        }


@dataclass(frozen=True)
class AlertEvent:
    """A change in an alert's status - the thing worth notifying on."""

    kind: str  # "raised" | "cleared" | "escalated"
    alert: Alert

    def as_dict(self) -> dict[str, Any]:
        return {"event": f"alert_{self.kind}", **self.alert.as_dict()}


class Rule(Protocol):
    id: str
    for_seconds: float
    clear_after: float

    def check(self, state: BoatState, active: bool) -> Finding | None: ...

    def applies(self, state: BoatState) -> bool:
        """Is this rule's question still being asked at all?

        Distinct from check() returning None, and the difference matters when
        an alert is standing. A condition that has EASED needs the hysteresis:
        a voltage hovering on the line must not raise and clear all afternoon.
        A condition that no longer APPLIES does not - the anchor is on the bow
        roller, somebody has just said so, and holding a siren on for another
        two minutes teaches them to reach for the volume knob instead.

        Optional. A rule that does not define it is always applicable, which is
        true of every rule that watches an instrument rather than a decision.
        """
        ...


# -------------------------------------------------------------- rule bases --


@dataclass
class BaseRule:
    id: str
    for_seconds: float = 30.0
    clear_after: float = 60.0
    max_age: float = DEFAULT_MAX_AGE

    def reading(self, state: BoatState, path: str) -> float | None:
        """A numeric value, or None if it is missing, stale or not a number."""
        if state.is_stale(path, self.max_age):
            return None
        value = state.value(path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)


@dataclass
class RangeRule(BaseRule):
    """Fires when a numeric path leaves a range.

    `hysteresis` widens the range once the alert is active, so a value hovering
    on the threshold does not chatter.
    """

    path: str = ""
    low: float | None = None
    high: float | None = None
    critical_low: float | None = None
    critical_high: float | None = None
    hysteresis: float = 0.0
    severity: Severity = Severity.WARN
    critical_severity: Severity = Severity.ALARM
    label: str = ""
    unit: str = ""
    scale: Callable[[float], float] | None = None

    def _display(self, value: float) -> str:
        shown = self.scale(value) if self.scale else value
        return f"{shown:.1f}{self.unit}"

    def check(self, state: BoatState, active: bool) -> Finding | None:
        value = self.reading(state, self.path)
        if value is None:
            return None

        margin = self.hysteresis if active else 0.0
        name = self.label or self.path

        # Hysteresis belongs on whichever threshold decides when the alert
        # clears, which is the outermost one in each direction. Widening the
        # critical threshold as well would escalate a steady reading from warn
        # to alarm without the value having moved at all: a bank resting at
        # 11.9 V would report itself as critically low the tick after it
        # reported itself as low. So the critical lines only get the margin
        # when there is no warning threshold outside them to carry it.
        critical_low_margin = margin if self.low is None else 0.0
        critical_high_margin = margin if self.high is None else 0.0

        if self.critical_low is not None and value < self.critical_low + critical_low_margin:
            return self._finding(self.critical_severity, name, value, "critically low")
        if self.critical_high is not None and value > self.critical_high - critical_high_margin:
            return self._finding(self.critical_severity, name, value, "critically high")
        if self.low is not None and value < self.low + margin:
            return self._finding(self.severity, name, value, "low")
        if self.high is not None and value > self.high - margin:
            return self._finding(self.severity, name, value, "high")
        return None

    def _finding(self, severity: Severity, name: str, value: float, how: str) -> Finding:
        return Finding(
            severity=severity,
            message=f"{name} {how}: {self._display(value)}",
            data={"path": self.path, "value": value},
        )


# ------------------------------------------------------------ custom rules --


@dataclass
class ShallowWaterRule(BaseRule):
    """Not enough water under the keel, but only when actually moving.

    At anchor in 3 m the depth alarm is noise. Under way it is the one that
    matters.

    Reports clearance under the KEEL, not under the transducer, because that is
    the number that decides whether the boat stops. Prefers
    environment.depth.belowKeel if anything publishes it, and otherwise
    subtracts the transducer-to-keel offset itself.
    """

    warn_m: float = KEEL_CLEARANCE_WARN_M
    alarm_m: float = KEEL_CLEARANCE_ALARM_M
    hysteresis_m: float = 0.5
    underway_speed: float = UNDERWAY_SPEED_MS
    transducer_to_keel: float = TRANSDUCER_TO_KEEL_M

    def clearance(self, state: BoatState) -> tuple[float, str] | None:
        """Water under the keel, and where the number came from."""
        below_keel = self.reading(state, "environment.depth.belowKeel")
        if below_keel is not None:
            return below_keel, "belowKeel"

        below_transducer = self.reading(state, "environment.depth.belowTransducer")
        if below_transducer is None:
            return None

        # A live offset from Signal K wins over the built-in constant.
        offset = self.reading(state, "environment.depth.transducerToKeel")
        if offset is None:
            offset = self.transducer_to_keel
        return below_transducer - offset, "belowTransducer"

    def speed(self, state: BoatState) -> float | None:
        """How fast the boat is going, from whichever instrument still works.

        SOG comes from the GPS and STW from the paddlewheel -
        different boxes on different parts of the bus. Preferring SOG but
        falling back to STW means losing the GPS fix no longer takes the depth
        alarm with it. That mattered: the depth sounder and the paddlewheel can
        both be reporting perfectly while the GPS is out, which is precisely
        the moment a shoal is hardest to see coming.
        """
        sog = self.reading(state, "navigation.speedOverGround")
        if sog is not None:
            return sog
        return self.reading(state, "navigation.speedThroughWater")

    def check(self, state: BoatState, active: bool) -> Finding | None:
        speed = self.speed(state)
        if speed is None or speed < self.underway_speed:
            return None

        found = self.clearance(state)
        if found is None:
            return None
        clearance, source = found

        margin = self.hysteresis_m if active else 0.0
        data = {"under_keel_m": round(clearance, 2), "speed_ms": speed, "from": source}

        if clearance < self.alarm_m + margin:
            return Finding(
                Severity.ALARM, f"Shallow: {clearance:.1f} m under the keel", data
            )
        if clearance < self.warn_m + margin:
            return Finding(
                Severity.WARN, f"Shoaling: {clearance:.1f} m under the keel", data
            )
        return None


@dataclass
class BatteryTemperatureRule(BaseRule):
    """House bank temperature, with the charging case called out separately."""

    path: str = "electrical.batteries.house.temperature"
    charge_path: str = "electrical.solar.mppt.current"

    def check(self, state: BoatState, active: bool) -> Finding | None:
        kelvin = self.reading(state, self.path)
        if kelvin is None:
            return None
        celsius = kelvin - KELVIN
        margin = 1.0 if active else 0.0
        data = {"temperature_c": round(celsius, 1)}

        if celsius > BATTERY_TEMP_MAX_C - margin:
            return Finding(Severity.ALARM, f"House battery hot: {celsius:.1f} C", data)
        if celsius < BATTERY_TEMP_MIN_C + margin:
            return Finding(Severity.WARN, f"House battery cold: {celsius:.1f} C", data)

        # Charging a frozen LiFePO4 bank does permanent damage.
        current = self.reading(state, self.charge_path)
        if celsius < BATTERY_TEMP_MIN_CHARGE_C + margin and (current or 0) > CHARGING_CURRENT_A:
            return Finding(
                Severity.ALARM,
                f"Charging below freezing: {celsius:.1f} C at {current:.1f} A",
                {**data, "current_a": current},
            )
        return None


@dataclass
class StarlinkDownRule(BaseRule):
    """Link down for a while.

    Only ever fires if Starlink has reported at least once, so a Pi with the
    plugin not installed stays quiet instead of alarming forever.
    """

    path: str = "communication.starlink.state"
    down_seconds: float = STARLINK_DOWN_SECONDS

    def __post_init__(self) -> None:
        self._seen = False

    def check(self, state: BoatState, active: bool) -> Finding | None:
        sample = state.get(self.path)
        if sample is None:
            return None
        self._seen = True

        age = state.age(self.path) or 0.0
        value = sample.value
        offline = isinstance(value, str) and value.lower() not in STARLINK_ONLINE_STATES

        if age > self.down_seconds:
            return Finding(
                Severity.ALERT,
                f"Starlink silent for {age / 60:.0f} min",
                {"age_s": round(age), "last_state": value},
            )
        if offline:
            return Finding(Severity.ALERT, f"Starlink {value}", {"state": value})
        return None


@dataclass
class BilgeCyclingRule(BaseRule):
    """Too many bilge pump cycles in an hour.

    One cycle is normal - stuffing box drip, rain, a wave over the sill. Six in
    an hour means water is coming in faster than it should.
    """

    path: str = "notifications.bilge"
    window_s: float = BILGE_CYCLES_WINDOW_S
    max_cycles: int = BILGE_CYCLES_WARN

    def __post_init__(self) -> None:
        self._cycles: deque[datetime] = deque()
        self._was_active = False

    @staticmethod
    def _is_running(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, dict):  # a Signal K notification object
            return str(value.get("state", "")).lower() not in ("normal", "")
        if isinstance(value, str):
            return value.lower() not in ("normal", "off", "false", "")
        if isinstance(value, (int, float)):
            return value != 0
        return False

    def check(self, state: BoatState, active: bool) -> Finding | None:
        sample = state.get(self.path)
        if sample is None:
            return None

        running = self._is_running(sample.value)
        now = sample.received
        if running and not self._was_active:
            self._cycles.append(now)  # count starts, not duration
        self._was_active = running

        while self._cycles and (now - self._cycles[0]).total_seconds() > self.window_s:
            self._cycles.popleft()

        count = len(self._cycles)
        if count >= self.max_cycles:
            return Finding(
                Severity.ALARM,
                f"Bilge pump cycled {count} times in the last hour",
                {"cycles": count, "window_s": self.window_s},
            )
        return None


@dataclass
class AnchorDragRule(BaseRule):
    """Position outside the anchor watch circle.

    Reads navigation.anchor.* if something on the bus publishes it (the Signal K
    anchor alarm plugin does). Otherwise the anchor can be set on the rule
    directly with set_anchor(). No anchor set means no rule - it stays silent
    rather than guessing where the hook went down.
    """

    position_path: str = "navigation.position"
    anchor_path: str = "navigation.anchor.position"
    radius_path: str = "navigation.anchor.maxRadius"
    margin_m: float = ANCHOR_DRAG_MARGIN_M

    def __post_init__(self) -> None:
        self._anchor: tuple[float, float] | None = None
        self._radius: float | None = None

    def set_anchor(self, latitude: float, longitude: float, radius_m: float) -> None:
        self._anchor = (latitude, longitude)
        self._radius = radius_m
        log.info("anchor set at %.5f, %.5f with a %.0f m radius", latitude, longitude, radius_m)

    def clear_anchor(self) -> None:
        self._anchor = None
        self._radius = None

    @staticmethod
    def _radius_of(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if value > 0 else None

    def _anchor_from(self, state: BoatState) -> tuple[tuple[float, float], float] | None:
        """Where the anchor is and how big the watch circle is.

        Neither of these is staleness-checked, and that is deliberate. They are
        configuration, not sensor readings: the anchor plugin publishes them
        once when the anchor is set and then says nothing until it moves or is
        weighed. Expiring them after max_age would silently disarm the drag
        alarm a few minutes after anchoring - exactly when it is needed, and
        with nothing in the log to say it had stopped watching. The position
        the anchor is compared *against* is staleness-checked in check(),
        which is the reading that can actually go bad.
        """
        anchor = as_position(state.value(self.anchor_path))
        radius = self._radius_of(state.value(self.radius_path))
        if anchor is not None and radius is not None:
            return anchor, radius
        if self._anchor is not None and self._radius is not None:
            return self._anchor, self._radius
        return None

    def applies(self, state: BoatState) -> bool:
        """Only while there is an anchor down. Weighing it ends the question."""
        return self._anchor_from(state) is not None

    def check(self, state: BoatState, active: bool) -> Finding | None:
        anchored = self._anchor_from(state)
        if anchored is None:
            return None
        anchor, radius = anchored

        if state.is_stale(self.position_path, self.max_age):
            return None
        position = as_position(state.value(self.position_path))
        if position is None:
            return None

        distance = distance_m(anchor, position)
        limit = radius + (0.0 if active else self.margin_m)
        if distance > limit:
            return Finding(
                Severity.ALARM,
                f"Dragging: {distance:.0f} m from the anchor, watch circle {radius:.0f} m",
                {"distance_m": round(distance, 1), "radius_m": radius},
            )
        return None


@dataclass
class AnchorWatchBlindRule(BaseRule):
    """The anchor watch is armed and the agent cannot see where the boat is.

    The drag rule stays silent when it has no position, and that is right: a
    rule that guesses is worse than one that waits. But silence is the failure
    this whole project is most afraid of. You set the watch, you went to sleep
    believing you had one, and it stopped watching without saying so.

    So this watches the watch. It only speaks when an anchor has actually been
    set, because a boat alongside with no GPS does not need telling.

    Two ways to go blind, and they look nothing alike in the log. The receiver
    can stop reporting, which shows up as a stale path. Or it can keep
    reporting and have nothing to say, which is what a GPS with no fix does -
    on this boat that arrives as a position of 0,0 every second, fresh as
    anything and completely useless (see agent/geo.py). Both are the same fact
    to a sleeping crew, so both are the same alert here.

    It gets louder rather than starting loud. A GPS that drops out for two
    minutes under a bimini is worth a message; one that has been gone for a
    quarter of an hour with the boat swinging is worth waking up for.
    """

    anchor_path: str = "navigation.anchor.position"
    position_path: str = "navigation.position"
    # After this long without a position, stop being a warning and be an alarm.
    alarm_after: float = 900.0

    def __post_init__(self) -> None:
        self._blind_since: datetime | None = None

    def applies(self, state: BoatState) -> bool:
        """Only while there is an anchor down, same as the rule it watches."""
        return as_position(state.value(self.anchor_path)) is not None

    def check(self, state: BoatState, active: bool) -> Finding | None:
        if as_position(state.value(self.anchor_path)) is None:
            self._blind_since = None
            return None  # no anchor set: nothing is being watched, so nothing is lost

        stale = state.is_stale(self.position_path, self.max_age)
        position = None if stale else as_position(state.value(self.position_path))
        if position is not None:
            self._blind_since = None
            return None

        now = state.now()
        if self._blind_since is None:
            self._blind_since = now
        blind_for = (now - self._blind_since).total_seconds()

        why = "the GPS has stopped reporting" if stale else "the GPS has no fix"
        severity = Severity.ALARM if blind_for >= self.alarm_after else Severity.WARN
        return Finding(
            severity,
            f"Anchor watch is blind: {why}, {blind_for / 60:.0f} min ago",
            {"blind_for_s": round(blind_for), "stale": stale},
        )


@dataclass
class BusSilentRule(BaseRule):
    """Every instrument on the boat is quiet, which is not the same as calm.

    Each rule here goes silent when its own instrument goes silent, and that is
    right: a rule that guesses is worse than one that waits. But if EVERY
    instrument is silent at once the boat is not becalmed, it is disconnected,
    and the agent sits there watching nothing while looking exactly like a boat
    at peace. That is the failure anchor_watch_blind exists to catch, one level
    up: the whole bus, rather than one path.

    Found the hard way. A udev rule auto-detected the wrong USB serial adapter
    on install day and pinned it as the gateway. Signal K opened it, spoke
    Actisense at it, and reported nothing wrong for two days, because "no depth
    reported" and "no gateway" look identical from up here.

    It only speaks when something else is arriving. If nothing at all is coming
    through then Signal K is the problem, not the bus, and that is already
    shouted about when the agent starts.
    """

    paths: tuple[str, ...] = ()
    quiet_after: float = BUS_SILENT_SECONDS

    def __post_init__(self) -> None:
        self._watching_since: datetime | None = None

    def check(self, state: BoatState, active: bool) -> Finding | None:
        if state.deltas_seen <= 0:
            self._watching_since = None
            return None  # nothing at all is arriving: a different fault

        if any(path in state for path in self.paths):
            self._watching_since = None
            return None

        now = state.now()
        if self._watching_since is None:
            self._watching_since = now
        quiet_for = (now - self._watching_since).total_seconds()
        if quiet_for < self.quiet_after:
            return None

        return Finding(
            Severity.ALERT,
            f"Nothing from the instruments in {quiet_for / 60:.0f} min: no depth, "
            "no wind, no speed through water. Signal K is talking, so check the "
            "gateway and that the bus has power",
            {"quiet_for_s": round(quiet_for), "paths_seen": len(state)},
        )


def anchor_is_set(state: BoatState) -> bool:
    """Is there a watch circle around a hook right now?

    Reads only what is on the bus, which is enough: whatever armed the watch -
    the Signal K anchor plugin, or the crew's anchor file fed in by the agent -
    put these there. Not staleness-checked, for the reason set out in
    AnchorDragRule._anchor_from: they are configuration, not readings.
    """
    if as_position(state.value("navigation.anchor.position")) is None:
        return False
    radius = state.value("navigation.anchor.maxRadius")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)):
        return False
    return radius > 0


@dataclass
class ForecastWindRule(BaseRule):
    """A blow on the way, from the forecast rather than from the instruments.

    The only rule here that is about the future, and the only one with a
    ceiling on its severity. It never raises ALARM, because ALARM is what
    sounds the siren in sound.py, and a siren at 0200 for a wind that arrives
    at 1100 is precisely how a crew learns to turn the siren off - and then the
    drag alarm is silent too. A gale twelve hours out is a message and a
    notification on the screen. That is what it is worth.

    Everything it knows comes from the store, not from the state model: the
    forecast is a table of hours and a rule that could only see the current
    value would have nothing useful to say about when. Staleness is still
    checked, in the same spirit as everywhere else - a forecast nobody has been
    able to refresh for two hours is not evidence about tonight.

    It looks further ahead once the anchor is down. Underway the useful
    question is the next twelve hours, because the boat is already moving and
    can keep moving. At anchor the question is the whole night, and it is asked
    at the one moment when the answer is still cheap: more chain, a different
    cove, or leave before dark. Same thresholds either way - a night at anchor
    in a windy summer anchorage is routinely F6 and a lower bar here would fire most
    nights of the season, which is how an alert stops being read.
    """

    store: WeatherStore | None = None
    outlook_hours: float = OUTLOOK_HOURS
    anchored_outlook_hours: float = ANCHORED_OUTLOOK_HOURS
    wind_warn: float = FORECAST_WIND_WARN_MS
    gust_warn: float = FORECAST_GUST_WARN_MS
    wind_strong: float = FORECAST_WIND_STRONG_MS
    gust_strong: float = FORECAST_GUST_STRONG_MS
    hysteresis: float = FORECAST_WIND_HYSTERESIS_MS
    max_age: float = FORECAST_MAX_AGE

    def check(self, state: BoatState, active: bool) -> Finding | None:
        if self.store is None:
            return None

        now = state.now()
        forecast = self.store.fresh(self.max_age, now)
        if forecast is None:
            return None

        anchored = anchor_is_set(state)
        hours = self.anchored_outlook_hours if anchored else self.outlook_hours
        outlook = forecast.outlook(hours, now)
        if outlook.empty:
            return None
        wind, gust = outlook.wind_ms, outlook.gust_ms

        # Once raised, hold on until it drops a margin below the line, so a
        # model that nudges the peak up and down by half a knot between runs
        # does not send a message every hour.
        margin = self.hysteresis if active else 0.0
        strong = self._over(wind, self.wind_strong, margin) or self._over(
            gust, self.gust_strong, margin
        )
        building = self._over(wind, self.wind_warn, margin) or self._over(
            gust, self.gust_warn, margin
        )
        if not building and not strong:
            return None

        onset = self._onset(forecast.window(hours, now), margin)
        lead_h = (onset.time - now).total_seconds() / 3600 if onset is not None else None
        windiest = outlook.windiest
        direction = cardinal(windiest.direction_rad) if windiest is not None else None

        headline = "Gale forecast:" if strong else "Wind building:"
        # Named at anchor, because the alert is read on a phone by somebody who
        # needs to know at once whether it is about the boat they left swinging.
        parts = [f"At anchor. {headline}" if anchored else headline]
        if wind is not None:
            parts.append(f"{knots(wind):.0f} kn")
        if direction:
            parts.append(f"from the {direction}")
        if gust is not None:
            parts.append(f"gusting {knots(gust):.0f} kn")
        if onset is not None and lead_h is not None:
            when = onset.time.strftime("%H:%M")
            parts.append(
                f"from {when} UTC, in {lead_h:.0f} h"
                if lead_h >= 1
                else f"from {when} UTC, within the hour"
            )
        else:
            parts.append(f"within {hours:.0f} h")

        return Finding(
            # WARN outranks ALERT here (see Severity), and ALARM is deliberately
            # never reached.
            Severity.WARN if strong else Severity.ALERT,
            " ".join(parts),
            {
                "max_wind_ms": None if wind is None else round(wind, 1),
                "max_gust_ms": None if gust is None else round(gust, 1),
                "onset": None if onset is None else onset.time.isoformat(timespec="minutes"),
                "lead_h": None if lead_h is None else round(lead_h, 1),
                "outlook_h": hours,
                "anchored": anchored,
                "forecast_age_s": round(forecast.age(now)),
            },
        )

    @staticmethod
    def _over(value: float | None, threshold: float, margin: float) -> bool:
        return value is not None and value >= threshold - margin

    def _onset(self, window: tuple[Hour, ...], margin: float) -> Hour | None:
        """The first hour that crosses the line - the number you plan around.

        Not the peak. Knowing the worst of it is 35 knots matters less than
        knowing it starts at 0300, because one of those tells you when to move.
        """
        for hour in window:
            if self._over(hour.wind_ms, self.wind_warn, margin) or self._over(
                hour.gust_ms, self.gust_warn, margin
            ):
                return hour
        return None


# ------------------------------------------------------------------ engine --


@dataclass
class _Status:
    """Per-rule bookkeeping: how long a condition has held, and what is live."""

    pending_since: datetime | None = None
    clear_since: datetime | None = None
    alert: Alert | None = None


class RuleEngine:
    """Runs the rules and turns their findings into raise/clear events.

    The rules decide what is wrong; the engine decides when it is worth saying
    so. Keeping the timing here means a rule stays a pure function of state and
    is trivial to test.
    """

    def __init__(self, rules: list[Rule], clock: Callable[[], datetime] | None = None) -> None:
        self.rules = rules
        # UTC, to match the timestamps BoatState stamps samples with.
        self._clock = clock or (lambda: datetime.now(UTC))
        self._status: dict[str, _Status] = {rule.id: _Status() for rule in rules}

    @property
    def active(self) -> list[Alert]:
        return [s.alert for s in self._status.values() if s.alert is not None]

    def alert_for(self, rule_id: str) -> Alert | None:
        status = self._status.get(rule_id)
        return status.alert if status else None

    def evaluate(self, state: BoatState) -> list[AlertEvent]:
        now = self._clock()
        events: list[AlertEvent] = []

        for rule in self.rules:
            status = self._status[rule.id]
            try:
                finding = rule.check(state, status.alert is not None)
            except Exception:
                # A broken rule must not take down monitoring for every other
                # rule, so log it and carry on.
                log.exception("rule %s raised, skipping it this tick", rule.id)
                continue

            if finding is not None:
                events.extend(self._on_finding(rule, status, finding, now))
            else:
                events.extend(self._on_clear(rule, status, now, state))

        return events

    def _on_finding(
        self, rule: Rule, status: _Status, finding: Finding, now: datetime
    ) -> list[AlertEvent]:
        status.clear_since = None

        if status.alert is None:
            if status.pending_since is None:
                status.pending_since = now
            held = (now - status.pending_since).total_seconds()
            if held < rule.for_seconds:
                return []
            status.alert = Alert(
                rule_id=rule.id,
                severity=finding.severity,
                message=finding.message,
                since=status.pending_since,
                data=finding.data,
            )
            status.pending_since = None
            return [AlertEvent("raised", status.alert)]

        # Already active: only speak up again if it has got worse.
        if SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER[status.alert.severity]:
            status.alert = Alert(
                rule_id=rule.id,
                severity=finding.severity,
                message=finding.message,
                since=status.alert.since,
                data=finding.data,
            )
            return [AlertEvent("escalated", status.alert)]
        return []

    def _on_clear(
        self, rule: Rule, status: _Status, now: datetime, state: BoatState
    ) -> list[AlertEvent]:
        status.pending_since = None
        if status.alert is None:
            return []

        # A question that is over does not serve out the hysteresis. Found by
        # weighing the anchor while the drag alarm was sounding and listening
        # to it for another two minutes.
        applies = getattr(rule, "applies", None)
        if applies is not None and not applies(state):
            cleared = status.alert
            status.alert = None
            status.clear_since = None
            return [AlertEvent("cleared", cleared)]

        if status.clear_since is None:
            status.clear_since = now
        if (now - status.clear_since).total_seconds() < rule.clear_after:
            return []

        cleared = status.alert
        status.alert = None
        status.clear_since = None
        return [AlertEvent("cleared", cleared)]


# ---------------------------------------------------------------- defaults --


def build_default_rules(
    weather: WeatherStore | None = None,
    outlook_hours: float = OUTLOOK_HOURS,
    anchored_outlook_hours: float = ANCHORED_OUTLOOK_HOURS,
) -> list[Rule]:
    """The phase 1 rule set from CLAUDE.md.

    Debounce times are chosen per rule: shallow water has to react in seconds,
    while locker temperature moves over minutes and a fast trigger would only
    produce noise.

    `weather` is the forecast store, if there is one. Without it the forecast
    rule is still in the list and simply says nothing, which keeps the rule set
    the same shape whether or not the boat has a link.
    """
    return [
        ShallowWaterRule(id="shallow_water", for_seconds=10.0, clear_after=30.0),
        AnchorDragRule(id="anchor_drag", for_seconds=30.0, clear_after=120.0),
        # Longer than the drag rule's debounce on purpose: a receiver that
        # blinks for a few seconds is not news, and this must not cry wolf or
        # the crew will stop believing the one that matters.
        AnchorWatchBlindRule(
            id="anchor_watch_blind", for_seconds=120.0, clear_after=60.0
        ),
        RangeRule(
            id="house_voltage",
            path="electrical.solar.mppt.voltage",
            label="House voltage (MPPT, rough)",
            unit=" V",
            low=HOUSE_VOLTAGE_LOW,
            critical_low=HOUSE_VOLTAGE_CRITICAL,
            critical_high=HOUSE_VOLTAGE_HIGH,
            hysteresis=0.15,
            # Long, because a winch or the inverter sags the bank for seconds at
            # a time and that is not a fault.
            for_seconds=120.0,
            clear_after=120.0,
        ),
        BatteryTemperatureRule(id="battery_temperature", for_seconds=120.0, clear_after=300.0),
        RangeRule(
            id="locker_temperature",
            path="environment.inside.locker.temperature",
            label="Nav locker",
            unit=" C",
            scale=lambda k: k - KELVIN,
            high=c_to_k(LOCKER_TEMP_WARN_C),
            critical_high=c_to_k(LOCKER_TEMP_ALARM_C),
            hysteresis=1.0,
            for_seconds=300.0,
            clear_after=600.0,
        ),
        # Slow on both sides. The forecast only changes when a model run
        # lands, so a five-minute hold costs nothing and stops one odd fetch
        # sending a message; half an hour to clear stops a peak hovering on the
        # threshold from raising and clearing all afternoon.
        ForecastWindRule(
            id="wind_forecast",
            store=weather,
            outlook_hours=outlook_hours,
            anchored_outlook_hours=anchored_outlook_hours,
            for_seconds=300.0,
            clear_after=1800.0,
        ),
        # Slow to raise and quick to clear: one instrument waking up answers it.
        BusSilentRule(
            id="bus_silent", paths=BUS_PATHS, for_seconds=60.0, clear_after=0.0
        ),
        BilgeCyclingRule(id="bilge_cycling", for_seconds=0.0, clear_after=600.0),
        StarlinkDownRule(id="starlink_down", for_seconds=60.0, clear_after=120.0),
        RangeRule(
            id="cpu_temperature",
            path="environment.rpi.cpu.temperature",
            label="Pi CPU",
            unit=" C",
            scale=lambda k: k - KELVIN,
            high=c_to_k(CPU_TEMP_WARN_C),
            critical_high=c_to_k(CPU_TEMP_ALARM_C),
            hysteresis=2.0,
            for_seconds=120.0,
            clear_after=300.0,
        ),
        RangeRule(
            id="disk_free",
            path="environment.rpi.disk.free",
            label="Disk free",
            unit=" GB",
            scale=lambda b: b / 1024**3,
            low=DISK_FREE_WARN_BYTES,
            critical_low=DISK_FREE_ALARM_BYTES,
            hysteresis=100 * 1024**2,
            for_seconds=60.0,
            clear_after=300.0,
        ),
    ]

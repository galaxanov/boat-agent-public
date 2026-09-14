"""What the boat is actually doing, inferred from what the instruments report.

This is guesswork with the confidence attached, not measurement, and the code
says so everywhere. Two things are worth being blunt about:

*Motoring* is inferred, because there is no engine data at all without an engine
gateway such as an EMU-1 - no RPM, no oil pressure, no alternator output. The inference used
here only claims motoring when sailing is *physically impossible*: moving with
no wind, or moving while pointing closer to the wind than a cruising
boat can sail. Anything else is reported as sailing, so a genuine motorsail
under main will read as sailing. That is the safe direction to be wrong in.

*Moored* versus *anchored* cannot be told apart from position and speed alone -
both are "stopped". The state is ANCHORED only when an anchor is actually set
(from the Signal K anchor plugin, or set on the rule), and STOPPED otherwise.
Guessing between them from swing behaviour is possible but needs half an hour
of heading history, so it is not attempted here.

Kept out of state.py deliberately: BoatState is a plain value store that either
has a reading or does not, and mixing heuristics into it would make both harder
to reason about and to test.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .state import BoatState

log = logging.getLogger(__name__)

# Below this the boat is not going anywhere. About 1 knot: enough to ignore GPS
# noise and a boat sailing back and forth over its anchor.
MOVING_SPEED_MS = 0.5

# Moving with less true wind than this means the engine is on. 2 m/s is under
# 4 knots - a cruising yacht does not make way in that.
NO_WIND_MS = 2.0

# No cruising boat sails closer than about 30 degrees off the true wind. Inside
# that and still making way, it is motoring or motorsailing.
NO_GO_ANGLE_DEG = 30.0

# Making way for the purposes of the motoring test - higher than MOVING_SPEED_MS
# so that drifting in a calm does not read as motoring.
MAKING_WAY_MS = 1.0

# MPPT current thresholds. The MPPT reports its own output, so this is solar
# production rather than net bank current - that needs the SmartShunt.
CHARGING_A = 1.0
PANEL_ACTIVE_W = 5.0

# A state has to hold this long before it counts. Stops a wind lull flipping
# the boat between sailing and motoring every few seconds.
SETTLE_SECONDS = 120.0

DEFAULT_MAX_AGE = 300.0


class VesselState(StrEnum):
    UNKNOWN = "unknown"
    STOPPED = "stopped"
    ANCHORED = "anchored"
    MOORED = "moored"
    UNDERWAY_SAIL = "underway-sail"
    UNDERWAY_MOTOR = "underway-motor"


class ChargeState(StrEnum):
    UNKNOWN = "unknown"
    CHARGING = "charging"
    IDLE = "idle"


class Confidence(StrEnum):
    """How much to trust the state. Anything but CERTAIN is to be read as a hedge."""

    CERTAIN = "certain"  # directly measured
    LIKELY = "likely"  # inferred, physics says it cannot be otherwise
    GUESS = "guess"  # inferred, plausible alternatives exist


@dataclass(frozen=True)
class Derived:
    """A snapshot of what the boat appears to be doing."""

    vessel: VesselState = VesselState.UNKNOWN
    confidence: Confidence = Confidence.GUESS
    reason: str = "no data"
    charge: ChargeState = ChargeState.UNKNOWN
    solar_w: float | None = None
    # None means unknown rather than "not running" - there is no engine sensor,
    # so the agent must never claim the engine is off.
    engine_running: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "state": str(self.vessel),
            "confidence": str(self.confidence),
            "reason": self.reason,
            "charge": str(self.charge),
        }
        if self.solar_w is not None:
            out["solar_w"] = round(self.solar_w, 1)
        if self.engine_running is not None:
            out["engine_running"] = self.engine_running
        return out


@dataclass(frozen=True)
class Transition:
    """A change in the derived state, once it has settled."""

    previous: VesselState
    current: VesselState
    at: datetime
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": "state_changed",
            "from": str(self.previous),
            "to": str(self.current),
            "reason": self.reason,
            "at": self.at.isoformat(timespec="seconds"),
        }


def _number(state: BoatState, path: str, max_age: float) -> float | None:
    if state.is_stale(path, max_age):
        return None
    value = state.value(path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass
class StateDeriver:
    """Turns readings into a vessel state, with debounce on the transitions."""

    max_age: float = DEFAULT_MAX_AGE
    settle_seconds: float = SETTLE_SECONDS
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self._settled: VesselState = VesselState.UNKNOWN
        self._pending: VesselState | None = None
        self._pending_since: datetime | None = None

    # ------------------------------------------------------------- derive --

    def derive(self, state: BoatState) -> Derived:
        """The instantaneous reading. No history, no debounce."""
        charge, solar = self._charge(state)
        vessel, confidence, reason = self._vessel(state)

        engine: bool | None = None
        if vessel is VesselState.UNDERWAY_MOTOR:
            engine = True  # only ever inferred positively, never "off"

        return Derived(
            vessel=vessel,
            confidence=confidence,
            reason=reason,
            charge=charge,
            solar_w=solar,
            engine_running=engine,
        )

    def _charge(self, state: BoatState) -> tuple[ChargeState, float | None]:
        panel = _number(state, "electrical.solar.mppt.panelPower", self.max_age)
        current = _number(state, "electrical.solar.mppt.current", self.max_age)

        if panel is None and current is None:
            return ChargeState.UNKNOWN, None
        if (current or 0) > CHARGING_A or (panel or 0) > PANEL_ACTIVE_W:
            return ChargeState.CHARGING, panel
        return ChargeState.IDLE, panel

    def _vessel(self, state: BoatState) -> tuple[VesselState, Confidence, str]:
        sog = _number(state, "navigation.speedOverGround", self.max_age)
        stw = _number(state, "navigation.speedThroughWater", self.max_age)
        speed = sog if sog is not None else stw

        if speed is None:
            return VesselState.UNKNOWN, Confidence.GUESS, "no speed data"

        if speed < MOVING_SPEED_MS:
            return self._stopped(state, speed)

        return self._underway(state, speed)

    def _stopped(self, state: BoatState, speed: float) -> tuple[VesselState, Confidence, str]:
        anchored = not state.is_stale("navigation.anchor.position", self.max_age)
        if anchored:
            return (
                VesselState.ANCHORED,
                Confidence.CERTAIN,
                f"anchor is set and not making way ({speed:.1f} m/s)",
            )
        # Stopped, but nothing says whether that is an anchor, a dock or a buoy.
        return (
            VesselState.STOPPED,
            Confidence.LIKELY,
            f"not making way ({speed:.1f} m/s), no anchor set",
        )

    def _underway(self, state: BoatState, speed: float) -> tuple[VesselState, Confidence, str]:
        wind_speed = _number(state, "environment.wind.speedTrue", self.max_age)
        wind_angle = _number(state, "environment.wind.angleTrueWater", self.max_age)

        if speed >= MAKING_WAY_MS:
            # Making way in a calm: nothing else it could be.
            if wind_speed is not None and wind_speed < NO_WIND_MS:
                return (
                    VesselState.UNDERWAY_MOTOR,
                    Confidence.LIKELY,
                    f"making {speed:.1f} m/s in {wind_speed:.1f} m/s of true wind",
                )

            # Pointing higher than the boat can sail.
            if wind_angle is not None:
                off_the_wind = abs(math.degrees(wind_angle))
                off_the_wind = min(off_the_wind, 360 - off_the_wind)
                if off_the_wind < NO_GO_ANGLE_DEG:
                    return (
                        VesselState.UNDERWAY_MOTOR,
                        Confidence.LIKELY,
                        f"making way {off_the_wind:.0f} deg off the true wind",
                    )

        if wind_speed is None and wind_angle is None:
            return (
                VesselState.UNDERWAY_SAIL,
                Confidence.GUESS,
                f"making {speed:.1f} m/s, no wind data to tell sail from motor",
            )

        # Sailing is the default, so a motorsail reads as sailing. Wrong in the
        # direction that does not invent an engine that might not be running.
        return (
            VesselState.UNDERWAY_SAIL,
            Confidence.GUESS,
            f"making {speed:.1f} m/s with wind to sail by",
        )

    # --------------------------------------------------------- transitions --

    @property
    def current(self) -> VesselState:
        return self._settled

    def update(self, state: BoatState) -> tuple[Derived, Transition | None]:
        """Derive, and report a transition once a new state has settled."""
        derived = self.derive(state)
        now = self.clock()
        candidate = derived.vessel

        if candidate == self._settled:
            self._pending = None
            self._pending_since = None
            return derived, None

        # UNKNOWN is not a state worth announcing a move to - it means the data
        # went away, not that the boat did something.
        if candidate is VesselState.UNKNOWN:
            self._pending = None
            self._pending_since = None
            return derived, None

        # The first real state is adopted at once. Waiting two minutes to admit
        # the boat is anchored when the agent has only just started would be
        # silly, and there is no previous state for it to flap against.
        started_at = now
        if self._settled is not VesselState.UNKNOWN:
            if candidate != self._pending:
                self._pending = candidate
                self._pending_since = now
                return derived, None

            started_at = self._pending_since or now
            if (now - started_at).total_seconds() < self.settle_seconds:
                return derived, None

        transition = Transition(
            previous=self._settled,
            current=candidate,
            at=started_at,
            reason=derived.reason,
        )
        self._settled = candidate
        self._pending = None
        self._pending_since = None
        log.info(
            "state: %s -> %s (%s)", transition.previous, transition.current, transition.reason
        )
        return derived, transition

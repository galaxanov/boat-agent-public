"""The Signal K paths the agent subscribes to.

Everything here is SI, as it comes off the bus: metres, m/s, radians, Kelvin,
ratios 0-1, W, A, V. Conversion happens in units.py, for display only.

Subscribing to a path nothing publishes yet is harmless - the subscription just
stays quiet. Several paths below are in that state until the hardware is fitted;
they are listed anyway so the agent starts producing data the moment it is.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PathSpec:
    """One subscription entry.

    min_period_ms throttles a chatty source. Depth off a depth instrument arrives several
    times a second; once a second is plenty for anything the agent decides.
    """

    path: str
    unit: str
    min_period_ms: int
    note: str = ""


# Sources that exist today.
NAVIGATION: tuple[PathSpec, ...] = (
    PathSpec("navigation.position", "deg", 1000, "GPS via N2K"),
    PathSpec("navigation.speedOverGround", "m/s", 1000),
    PathSpec("navigation.courseOverGroundTrue", "rad", 1000),
    PathSpec("navigation.headingMagnetic", "rad", 1000, "compass / autopilot"),
    PathSpec("navigation.speedThroughWater", "m/s", 1000, "paddlewheel"),
    PathSpec("navigation.log", "m", 10_000, "trip log"),
)

ENVIRONMENT: tuple[PathSpec, ...] = (
    PathSpec("environment.depth.belowTransducer", "m", 1000, "depth sounder"),
    PathSpec("environment.depth.belowKeel", "m", 1000, "preferred if anything publishes it"),
    PathSpec(
        "environment.depth.transducerToKeel", "m", 60_000, "offset, from defaults.json"
    ),
    PathSpec("environment.water.temperature", "K", 10_000),
    PathSpec("environment.wind.speedApparent", "m/s", 1000, "wind instrument"),
    PathSpec("environment.wind.angleApparent", "rad", 1000),
    PathSpec("environment.wind.speedTrue", "m/s", 1000),
    PathSpec("environment.wind.angleTrueWater", "rad", 1000),
)

STEERING: tuple[PathSpec, ...] = (
    PathSpec("steering.autopilot.state", "enum", 5000),
    PathSpec("steering.autopilot.target.headingMagnetic", "rad", 5000),
)

# The MPPT publishes under electrical.solar.mppt.* - the device id configured in
# signalk/plugin-config-data/signalk-victron-ble.json becomes the path segment.
ELECTRICAL: tuple[PathSpec, ...] = (
    PathSpec("electrical.solar.mppt.panelPower", "W", 5000),
    PathSpec("electrical.solar.mppt.yieldToday", "J", 30_000),
    PathSpec("electrical.solar.mppt.chargingMode", "enum", 5000),
    PathSpec("electrical.solar.mppt.voltage", "V", 5000, "battery-side, rough house proxy"),
    PathSpec("electrical.solar.mppt.current", "A", 5000),
)

# Not fitted yet. Kept subscribed so nothing needs changing when they arrive.
PENDING_HARDWARE: tuple[PathSpec, ...] = (
    PathSpec("electrical.batteries.house.voltage", "V", 5000, "needs SmartShunt"),
    PathSpec("electrical.batteries.house.current", "A", 5000, "needs SmartShunt"),
    PathSpec(
        "electrical.batteries.house.capacity.stateOfCharge", "ratio", 5000, "needs SmartShunt"
    ),
    PathSpec("environment.inside.locker.temperature", "K", 30_000, "needs DS18B20"),
    PathSpec("electrical.batteries.house.temperature", "K", 30_000, "needs DS18B20"),
    PathSpec("notifications.bilge", "notification", 1000, "needs GPIO sense"),
)

INFRASTRUCTURE: tuple[PathSpec, ...] = (
    PathSpec("communication.starlink.state", "enum", 30_000),
    PathSpec("communication.starlink.obstruction", "ratio", 30_000),
    PathSpec("communication.starlink.uptime", "s", 30_000),
    PathSpec("environment.rpi.cpu.temperature", "K", 30_000),
    PathSpec("environment.rpi.disk.free", "bytes", 60_000),
)

SUBSCRIPTIONS: tuple[PathSpec, ...] = (
    *NAVIGATION,
    *ENVIRONMENT,
    *STEERING,
    *ELECTRICAL,
    *PENDING_HARDWARE,
    *INFRASTRUCTURE,
)

BY_PATH: dict[str, PathSpec] = {spec.path: spec for spec in SUBSCRIPTIONS}

# The paths that can only have come off the N2K bus, so the absence of every
# one of them means the bus itself, and not one quiet instrument.
#
# Position, course and speed over ground are deliberately NOT here: they come
# from gpsd and the u-blox, which is a different wire entirely. Nor is the
# MPPT, which arrives over Bluetooth. Those can all be perfectly healthy while
# the boat's own instruments say nothing at all.
BUS_PATHS: tuple[str, ...] = (
    "environment.depth.belowTransducer",
    "environment.depth.belowKeel",
    "environment.water.temperature",
    "environment.wind.speedApparent",
    "environment.wind.angleApparent",
    "navigation.speedThroughWater",
    "navigation.log",
    "navigation.headingMagnetic",
    "steering.autopilot.state",
)

# navigation.state is deliberately absent: the agent derives it (moored /
# anchored / underway-sail / underway-motor) rather than reading it.
DERIVED_PATHS: tuple[str, ...] = ("navigation.state",)


def subscription_message(context: str = "vessels.self") -> dict:
    """Build the Signal K subscription frame.

    policy 'instant' rather than 'ideal' on purpose. 'ideal' re-sends the last
    known value when a source goes quiet, which would make a dead sensor look
    alive - exactly the thing the staleness checks need to catch. With 'instant'
    a value arrives only when something actually reported it.
    """
    return {
        "context": context,
        "subscribe": [
            {
                "path": spec.path,
                "format": "delta",
                "policy": "instant",
                "minPeriod": spec.min_period_ms,
            }
            for spec in SUBSCRIPTIONS
        ],
    }

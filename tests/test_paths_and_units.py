from __future__ import annotations

import math

from agent import paths, units
from agent.state import BoatState

from .conftest import feed

# ------------------------------------------------------------------ paths --


def test_subscription_message_shape() -> None:
    message = paths.subscription_message()
    assert message["context"] == "vessels.self"
    assert len(message["subscribe"]) == len(paths.SUBSCRIPTIONS)
    entry = message["subscribe"][0]
    assert set(entry) == {"path", "format", "policy", "minPeriod"}
    assert entry["format"] == "delta"


def test_policy_is_instant_everywhere() -> None:
    """'ideal' would re-send cached values and hide a dead sensor."""
    assert all(e["policy"] == "instant" for e in paths.subscription_message()["subscribe"])


def test_no_duplicate_paths() -> None:
    listed = [spec.path for spec in paths.SUBSCRIPTIONS]
    assert len(listed) == len(set(listed))


def test_derived_paths_are_not_subscribed() -> None:
    for path in paths.DERIVED_PATHS:
        assert path not in paths.BY_PATH


def test_mppt_paths_match_the_configured_device_id() -> None:
    """signalk/plugin-config-data/signalk-victron-ble.json uses id "mppt"."""
    assert "electrical.solar.mppt.panelPower" in paths.BY_PATH
    assert "electrical.solar.mppt.voltage" in paths.BY_PATH


def test_min_periods_are_sane() -> None:
    assert all(100 <= spec.min_period_ms <= 300_000 for spec in paths.SUBSCRIPTIONS)


# ------------------------------------------------------------------ units --


def test_conversions() -> None:
    assert units.knots(1.0) == units.MS_TO_KNOTS
    assert units.degrees(math.pi) == 180.0
    assert units.celsius(273.15) == 0.0
    assert units.knots(None) is None
    assert units.celsius(None) is None


def test_compass_wraps_to_0_360() -> None:
    assert units.compass(-math.pi / 2) == 270.0
    assert units.compass(0.0) == 0.0


def test_relative_degrees_keeps_the_sign() -> None:
    assert units.relative_degrees(-math.pi / 4) == -45.0
    assert units.relative_degrees(math.pi / 4) == 45.0
    # 350 degrees apparent is 10 degrees off the other bow.
    assert round(units.relative_degrees(math.radians(350)), 6) == -10.0


def test_format_position() -> None:
    assert units.format_position({"latitude": 36.8342, "longitude": 10.2991}) == (
        "36.83420N 10.29910E"
    )
    assert units.format_position({"latitude": -36.5, "longitude": -25.5}) == (
        "36.50000S 25.50000W"
    )
    assert units.format_position(None) is None
    assert units.format_position({"latitude": 36.8}) is None


def test_format_status_with_no_data(clock) -> None:
    assert units.format_status(BoatState(clock=clock)) == "no data yet"


def test_format_status_shows_only_what_is_reporting(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)

    line = units.format_status(state)
    assert "36.83420N 10.29910E" in line
    assert "DBT 10.9m" in line
    assert "AWS 12.4kn" in line
    assert "AWA -35°" in line
    assert "PV 214W" in line
    assert "BATT 13.42V" in line
    # Nothing publishes these yet, so they must not appear at all.
    assert "LOCKER" not in line
    assert "CPU" not in line


def test_format_status_ignores_non_numeric_values(clock) -> None:
    state = BoatState(clock=clock)
    state.apply_delta(
        {
            "updates": [
                {
                    "$source": "test",
                    "values": [
                        {"path": "environment.depth.belowTransducer", "value": None},
                        {"path": "navigation.speedOverGround", "value": "fast"},
                    ],
                }
            ]
        }
    )
    assert units.format_status(state) == "no data yet"


def test_a_duration_reads_the_way_a_person_says_it() -> None:
    """"0.0 h" is not a length of time, and neither is "168 h"."""
    assert units.span(0) == "0 min"
    assert units.span(12 * 60) == "12 min"
    assert units.span(89 * 60) == "89 min"
    assert units.span(90 * 60) == "1 h 30"
    assert units.span(3 * 3600) == "3 h"
    # An anchor watch is read in minutes; a boat left on the hard is read in
    # days, and the same figure has to carry both.
    assert units.span(47 * 3600) == "47 h"
    assert units.span(48 * 3600) == "2 days"
    assert units.span(7 * 24 * 3600 + 3 * 3600) == "7 days 3 h"
    # A clock that has gone backwards is not a negative length of time.
    assert units.span(-500) == "0 min"

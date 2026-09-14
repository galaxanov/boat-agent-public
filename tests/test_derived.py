from __future__ import annotations

import math

import pytest

from agent.derived import ChargeState, Confidence, StateDeriver, VesselState
from agent.state import BoatState

from .conftest import push


@pytest.fixture
def state(clock) -> BoatState:
    return BoatState(clock=clock)


@pytest.fixture
def deriver(clock) -> StateDeriver:
    return StateDeriver(clock=clock)


def knots(kn: float) -> float:
    return kn / 1.9438444924406046


def deg(d: float) -> float:
    return math.radians(d)


# ------------------------------------------------------------------ stopped --


def test_no_speed_data_is_unknown(state, deriver) -> None:
    """Never claim to know what the boat is doing on no data."""
    derived = deriver.derive(state)
    assert derived.vessel is VesselState.UNKNOWN
    assert derived.engine_running is None


def test_stopped_without_an_anchor_is_not_called_anchored(state, deriver) -> None:
    push(state, {"navigation.speedOverGround": knots(0.2)})
    derived = deriver.derive(state)
    assert derived.vessel is VesselState.STOPPED
    assert "no anchor set" in derived.reason


def test_anchor_set_and_stopped_is_anchored(state, deriver) -> None:
    push(
        state,
        {
            "navigation.speedOverGround": knots(0.2),
            "navigation.anchor.position": {"latitude": 36.83, "longitude": 10.29},
        },
    )
    derived = deriver.derive(state)
    assert derived.vessel is VesselState.ANCHORED
    assert derived.confidence is Confidence.CERTAIN


def test_speed_through_water_is_used_when_sog_is_missing(state, deriver) -> None:
    push(state, {"navigation.speedThroughWater": knots(0.1)})
    assert deriver.derive(state).vessel is VesselState.STOPPED


# ----------------------------------------------------------------- underway --


def test_making_way_in_a_calm_is_motoring(state, deriver) -> None:
    push(
        state,
        {
            "navigation.speedOverGround": knots(5.0),
            "environment.wind.speedTrue": knots(2.0),
        },
    )
    derived = deriver.derive(state)
    assert derived.vessel is VesselState.UNDERWAY_MOTOR
    assert derived.confidence is Confidence.LIKELY
    assert derived.engine_running is True


def test_sailing_closer_than_possible_is_motoring(state, deriver) -> None:
    """No cruising boat sails 15 degrees off the true wind."""
    push(
        state,
        {
            "navigation.speedOverGround": knots(5.0),
            "environment.wind.speedTrue": knots(15.0),
            "environment.wind.angleTrueWater": deg(15),
        },
    )
    assert deriver.derive(state).vessel is VesselState.UNDERWAY_MOTOR


def test_no_go_angle_works_on_the_other_tack(state, deriver) -> None:
    push(
        state,
        {
            "navigation.speedOverGround": knots(5.0),
            "environment.wind.speedTrue": knots(15.0),
            "environment.wind.angleTrueWater": deg(-20),
        },
    )
    assert deriver.derive(state).vessel is VesselState.UNDERWAY_MOTOR


def test_close_hauled_but_sailable_is_sailing(state, deriver) -> None:
    push(
        state,
        {
            "navigation.speedOverGround": knots(5.5),
            "environment.wind.speedTrue": knots(14.0),
            "environment.wind.angleTrueWater": deg(42),
        },
    )
    derived = deriver.derive(state)
    assert derived.vessel is VesselState.UNDERWAY_SAIL
    # Never assert the engine is off - there is no sensor for it.
    assert derived.engine_running is None


def test_broad_reach_is_sailing(state, deriver) -> None:
    push(
        state,
        {
            "navigation.speedOverGround": knots(6.5),
            "environment.wind.speedTrue": knots(18.0),
            "environment.wind.angleTrueWater": deg(130),
        },
    )
    assert deriver.derive(state).vessel is VesselState.UNDERWAY_SAIL


def test_underway_without_wind_data_is_a_guess(state, deriver) -> None:
    push(state, {"navigation.speedOverGround": knots(5.0)})
    derived = deriver.derive(state)
    assert derived.vessel is VesselState.UNDERWAY_SAIL
    assert derived.confidence is Confidence.GUESS
    assert "no wind data" in derived.reason


def test_drifting_in_a_calm_is_not_motoring(state, deriver) -> None:
    """Just over the moving threshold in no wind is drift, not the engine."""
    push(
        state,
        {
            "navigation.speedOverGround": 0.7,  # under MAKING_WAY_MS
            "environment.wind.speedTrue": knots(1.0),
        },
    )
    assert deriver.derive(state).vessel is not VesselState.UNDERWAY_MOTOR


# ---------------------------------------------------------------- charging --


def test_charge_state_unknown_without_an_mppt(state, deriver) -> None:
    assert deriver.derive(state).charge is ChargeState.UNKNOWN


def test_solar_producing_is_charging(state, deriver) -> None:
    push(
        state,
        {
            "electrical.solar.mppt.panelPower": 214.0,
            "electrical.solar.mppt.current": 15.9,
        },
    )
    derived = deriver.derive(state)
    assert derived.charge is ChargeState.CHARGING
    assert derived.solar_w == 214.0


def test_panels_asleep_is_idle(state, deriver) -> None:
    push(
        state,
        {
            "electrical.solar.mppt.panelPower": 0.0,
            "electrical.solar.mppt.current": 0.0,
        },
    )
    assert deriver.derive(state).charge is ChargeState.IDLE


def test_stale_mppt_data_is_unknown_not_idle(state, deriver, clock) -> None:
    push(state, {"electrical.solar.mppt.panelPower": 200.0})
    clock.advance(1000)
    assert deriver.derive(state).charge is ChargeState.UNKNOWN


# ------------------------------------------------------------- transitions --


def test_first_state_is_adopted_immediately(state, deriver, clock) -> None:
    """Do not wait two minutes to admit the boat is anchored at startup."""
    push(
        state,
        {
            "navigation.speedOverGround": knots(0.1),
            "navigation.anchor.position": {"latitude": 36.83, "longitude": 10.29},
        },
    )
    _derived, transition = deriver.update(state)
    assert transition is not None
    assert transition.previous is VesselState.UNKNOWN
    assert transition.current is VesselState.ANCHORED
    assert deriver.current is VesselState.ANCHORED


def test_a_change_must_settle_before_it_counts(state, deriver, clock) -> None:
    push(state, {"navigation.speedOverGround": knots(0.1)})
    deriver.update(state)
    assert deriver.current is VesselState.STOPPED

    push(state, {"navigation.speedOverGround": knots(5.0)})
    _d, transition = deriver.update(state)
    assert transition is None  # under way, but only just

    clock.advance(60)
    assert deriver.update(state)[1] is None

    clock.advance(70)
    _d, transition = deriver.update(state)
    assert transition is not None
    assert transition.current is VesselState.UNDERWAY_SAIL


def test_a_wind_lull_does_not_flip_the_state(state, deriver, clock) -> None:
    """Sail -> motor -> sail inside the settle window must stay sailing."""
    push(
        state,
        {
            "navigation.speedOverGround": knots(6.0),
            "environment.wind.speedTrue": knots(14.0),
            "environment.wind.angleTrueWater": deg(90),
        },
    )
    deriver.update(state)
    assert deriver.current is VesselState.UNDERWAY_SAIL

    clock.advance(10)
    push(state, {"environment.wind.speedTrue": knots(3.0)})  # brief lull
    assert deriver.update(state)[1] is None

    clock.advance(20)
    push(state, {"environment.wind.speedTrue": knots(14.0)})  # wind returns
    assert deriver.update(state)[1] is None
    assert deriver.current is VesselState.UNDERWAY_SAIL


def test_losing_data_does_not_announce_a_change(state, deriver, clock) -> None:
    """Instruments going quiet is not the boat doing something."""
    push(state, {"navigation.speedOverGround": knots(0.1)})
    deriver.update(state)
    assert deriver.current is VesselState.STOPPED

    clock.advance(1000)  # everything goes stale
    derived, transition = deriver.update(state)
    assert derived.vessel is VesselState.UNKNOWN
    assert transition is None
    assert deriver.current is VesselState.STOPPED


def test_transition_serialises(state, deriver) -> None:
    import json

    push(state, {"navigation.speedOverGround": knots(0.1)})
    _d, transition = deriver.update(state)
    payload = transition.as_dict()
    assert payload["event"] == "state_changed"
    assert payload["to"] == "stopped"
    json.dumps(payload)


def test_derived_serialises(state, deriver) -> None:
    import json

    push(
        state,
        {
            "navigation.speedOverGround": knots(5.0),
            "environment.wind.speedTrue": knots(1.0),
            "electrical.solar.mppt.panelPower": 100.0,
        },
    )
    payload = deriver.derive(state).as_dict()
    assert payload["state"] == "underway-motor"
    assert payload["engine_running"] is True
    json.dumps(payload)

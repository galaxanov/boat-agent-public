from __future__ import annotations

import math

import pytest

from agent import rules as R
from agent.geo import as_position, distance_m
from agent.state import BoatState

from .conftest import push


@pytest.fixture
def state(clock) -> BoatState:
    return BoatState(clock=clock)


def engine(rule, clock) -> R.RuleEngine:
    return R.RuleEngine([rule], clock=clock)


def kelvin(celsius: float) -> float:
    return celsius + R.KELVIN


# ------------------------------------------------------------------ engine --


def test_nothing_fires_before_the_debounce_expires(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=60.0)
    eng = engine(rule, clock)

    push(state, {"p": 11.0})
    assert eng.evaluate(state) == []  # condition true, but only just

    clock.advance(59)
    assert eng.evaluate(state) == []

    clock.advance(2)
    events = eng.evaluate(state)
    assert [e.kind for e in events] == ["raised"]
    assert events[0].alert.severity is R.Severity.WARN


def test_a_brief_excursion_never_raises(state, clock) -> None:
    """One bad sample must not become an alarm."""
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=60.0)
    eng = engine(rule, clock)

    push(state, {"p": 11.0})
    eng.evaluate(state)
    clock.advance(10)
    push(state, {"p": 13.0})  # recovered well inside the debounce
    eng.evaluate(state)
    clock.advance(120)
    push(state, {"p": 13.0})
    assert eng.evaluate(state) == []
    assert eng.active == []


def test_clearing_waits_for_clear_after(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0, clear_after=60.0)
    eng = engine(rule, clock)

    push(state, {"p": 11.0})
    assert [e.kind for e in eng.evaluate(state)] == ["raised"]

    push(state, {"p": 13.0})
    assert eng.evaluate(state) == []  # recovered, but not for long enough
    assert eng.alert_for("v") is not None

    clock.advance(61)
    push(state, {"p": 13.0})
    events = eng.evaluate(state)
    assert [e.kind for e in events] == ["cleared"]
    assert eng.active == []


def test_active_alert_does_not_repeat_every_tick(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0)
    eng = engine(rule, clock)

    push(state, {"p": 11.0})
    assert len(eng.evaluate(state)) == 1
    for _ in range(5):
        clock.advance(30)
        push(state, {"p": 11.0})
        assert eng.evaluate(state) == []


def test_getting_worse_escalates(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, critical_low=11.8, for_seconds=0.0)
    eng = engine(rule, clock)

    push(state, {"p": 11.9})
    raised = eng.evaluate(state)[0]
    assert raised.alert.severity is R.Severity.WARN

    push(state, {"p": 11.5})
    escalated = eng.evaluate(state)[0]
    assert escalated.kind == "escalated"
    assert escalated.alert.severity is R.Severity.ALARM
    # The alert keeps the time the trouble started, not the time it worsened.
    assert escalated.alert.since == raised.alert.since


def test_hysteresis_stops_flapping_on_the_threshold(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, hysteresis=0.2, for_seconds=0.0, clear_after=0.0)
    eng = engine(rule, clock)

    push(state, {"p": 11.9})
    assert [e.kind for e in eng.evaluate(state)] == ["raised"]

    # Back over the line, but inside the hysteresis band: still alerting.
    push(state, {"p": 12.1})
    assert eng.evaluate(state) == []
    assert eng.alert_for("v") is not None

    push(state, {"p": 12.3})
    assert [e.kind for e in eng.evaluate(state)] == ["cleared"]


def test_a_steady_reading_does_not_escalate_itself(state, clock) -> None:
    """Hysteresis must not widen the critical band.

    A house bank resting at 11.9 V is low, not at BMS cutoff. It used to raise
    the warning and then escalate to ALARM on the next tick with the reading
    unchanged, because the active margin was applied to critical_low too.
    """
    rule = R.RangeRule(
        id="v", path="p", low=12.0, critical_low=11.8, hysteresis=0.15, for_seconds=0.0
    )
    eng = engine(rule, clock)

    push(state, {"p": 11.9})
    assert [e.kind for e in eng.evaluate(state)] == ["raised"]
    assert eng.alert_for("v").severity is R.Severity.WARN

    for _ in range(3):
        push(state, {"p": 11.9})
        assert eng.evaluate(state) == []
    assert eng.alert_for("v").severity is R.Severity.WARN

    # Actually crossing the line still escalates.
    push(state, {"p": 11.75})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.ALARM


def test_hysteresis_still_holds_a_lone_critical_threshold(state, clock) -> None:
    """With no warning threshold outside it, the critical line carries the margin."""
    rule = R.RangeRule(
        id="v", path="p", critical_low=11.8, hysteresis=0.2, for_seconds=0.0, clear_after=0.0
    )
    eng = engine(rule, clock)

    push(state, {"p": 11.7})
    assert [e.kind for e in eng.evaluate(state)] == ["raised"]

    # Back over the line but inside the band: no flapping.
    push(state, {"p": 11.9})
    assert eng.evaluate(state) == []

    push(state, {"p": 12.1})
    assert [e.kind for e in eng.evaluate(state)] == ["cleared"]


def test_the_house_bank_at_11_9_stays_a_warning(state, clock) -> None:
    """The real configured rule, not a synthetic one."""
    rule = next(r for r in R.build_default_rules() if r.id == "house_voltage")
    rule.for_seconds = 0.0
    eng = engine(rule, clock)

    push(state, {"electrical.solar.mppt.voltage": 11.9})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.WARN
    push(state, {"electrical.solar.mppt.voltage": 11.9})
    assert eng.evaluate(state) == []
    assert eng.alert_for("house_voltage").severity is R.Severity.WARN


def test_missing_data_never_alerts(state, clock) -> None:
    """A silent sensor is not a reading of zero."""
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0)
    eng = engine(rule, clock)
    assert eng.evaluate(state) == []


def test_stale_data_never_alerts(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0, max_age=100.0)
    eng = engine(rule, clock)

    push(state, {"p": 13.0})
    clock.advance(200)  # sensor has gone quiet
    assert eng.evaluate(state) == []


def test_non_numeric_value_never_alerts(state, clock) -> None:
    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0)
    eng = engine(rule, clock)
    push(state, {"p": "eleven"})
    assert eng.evaluate(state) == []
    push(state, {"p": None})
    assert eng.evaluate(state) == []


def test_a_broken_rule_does_not_stop_the_others(state, clock) -> None:
    class Exploding:
        id = "boom"
        for_seconds = 0.0
        clear_after = 0.0

        def check(self, state, active):
            raise RuntimeError("bad rule")

    good = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0)
    eng = R.RuleEngine([Exploding(), good], clock=clock)

    push(state, {"p": 11.0})
    events = eng.evaluate(state)
    assert [e.alert.rule_id for e in events] == ["v"]


# ----------------------------------------------------------- shallow water --


def test_shallow_water_is_silent_at_anchor(state, clock) -> None:
    """Anchored in 5 m with a 2 m draft is exactly where you want to be."""
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)
    push(state, {"environment.depth.belowTransducer": 5.0, "navigation.speedOverGround": 0.05})
    assert eng.evaluate(state) == []


def test_shallow_water_fires_underway(state, clock) -> None:
    """Thresholds are clearance under the keel, with a 2.0 m draft."""
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)

    # 5 m under the transducer = 3 m under the keel. Fine.
    push(state, {"environment.depth.belowTransducer": 5.0, "navigation.speedOverGround": 3.0})
    assert eng.evaluate(state) == []

    # 3.5 m under the transducer = 1.5 m under the keel.
    push(state, {"environment.depth.belowTransducer": 3.5, "navigation.speedOverGround": 3.0})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.WARN

    # 2.8 m under the transducer = 0.8 m under the keel.
    push(state, {"environment.depth.belowTransducer": 2.8, "navigation.speedOverGround": 3.0})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.ALARM


def test_shallow_water_message_is_in_keel_clearance(state, clock) -> None:
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)
    push(state, {"environment.depth.belowTransducer": 2.8, "navigation.speedOverGround": 3.0})
    alert = eng.evaluate(state)[0].alert
    assert "0.8 m under the keel" in alert.message
    assert alert.data["under_keel_m"] == 0.8


def test_below_keel_is_preferred_when_published(state, clock) -> None:
    """If something on the bus already does the arithmetic, trust it."""
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)
    push(
        state,
        {
            "environment.depth.belowKeel": 0.5,
            "environment.depth.belowTransducer": 50.0,  # would say "fine"
            "navigation.speedOverGround": 3.0,
        },
    )
    event = eng.evaluate(state)[0]
    assert event.alert.severity is R.Severity.ALARM
    assert event.alert.data["from"] == "belowKeel"


def test_a_live_offset_overrides_the_built_in_one(state, clock) -> None:
    """Measuring the transducer depth recovers clearance the default gives up."""
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)
    push(
        state,
        {
            "environment.depth.belowTransducer": 2.8,
            "environment.depth.transducerToKeel": 1.6,  # transducer 0.4 m down
            "navigation.speedOverGround": 3.0,
        },
    )
    # 1.2 m under the keel rather than 0.8: a warning, not an alarm.
    assert eng.evaluate(state)[0].alert.severity is R.Severity.WARN


def test_the_default_offset_errs_towards_alarming_early(state, clock) -> None:
    """Assuming the transducer is at the waterline understates clearance."""
    rule = R.ShallowWaterRule(id="d", for_seconds=0.0)
    assert rule.transducer_to_keel == R.DRAFT_M
    push(state, {"environment.depth.belowTransducer": 3.0, "navigation.speedOverGround": 3.0})
    clearance, source = rule.clearance(state)
    assert clearance == 1.0  # never flatters the real number
    assert source == "belowTransducer"


def test_shallow_water_needs_both_depth_and_speed(state, clock) -> None:
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)
    push(state, {"environment.depth.belowTransducer": 1.0})  # no speed known
    assert eng.evaluate(state) == []


# --------------------------------------------------------------- batteries --


def test_house_voltage_thresholds(state, clock) -> None:
    rule = next(r for r in R.build_default_rules() if r.id == "house_voltage")
    rule.for_seconds = 0.0
    eng = engine(rule, clock)

    push(state, {"electrical.solar.mppt.voltage": 13.4})
    assert eng.evaluate(state) == []

    push(state, {"electrical.solar.mppt.voltage": 11.9})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.WARN


def test_house_voltage_critical_low_and_high(state, clock) -> None:
    for voltage in (11.5, 15.0):
        rule = next(r for r in R.build_default_rules() if r.id == "house_voltage")
        rule.for_seconds = 0.0
        eng = engine(rule, clock)
        push(state, {"electrical.solar.mppt.voltage": voltage})
        assert eng.evaluate(state)[0].alert.severity is R.Severity.ALARM


def test_battery_hot_and_cold(state, clock) -> None:
    eng = engine(R.BatteryTemperatureRule(id="t", for_seconds=0.0), clock)

    push(state, {"electrical.batteries.house.temperature": kelvin(25)})
    assert eng.evaluate(state) == []

    push(state, {"electrical.batteries.house.temperature": kelvin(50)})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.ALARM


def test_charging_below_freezing_is_an_alarm(state, clock) -> None:
    """Charging a frozen LiFePO4 bank damages it permanently."""
    eng = engine(R.BatteryTemperatureRule(id="t", for_seconds=0.0), clock)

    # Cold but not charging: worth knowing, not an alarm.
    push(
        state,
        {
            "electrical.batteries.house.temperature": kelvin(-2),
            "electrical.solar.mppt.current": 0.0,
        },
    )
    assert eng.evaluate(state) == []

    push(
        state,
        {
            "electrical.batteries.house.temperature": kelvin(-2),
            "electrical.solar.mppt.current": 12.0,
        },
    )
    event = eng.evaluate(state)[0]
    assert event.alert.severity is R.Severity.ALARM
    assert "freezing" in event.alert.message


# ---------------------------------------------------------------- starlink --


def test_starlink_silent_when_the_plugin_is_not_installed(state, clock) -> None:
    """No Starlink data at all must not mean a permanent alert."""
    eng = engine(R.StarlinkDownRule(id="s", for_seconds=0.0), clock)
    assert eng.evaluate(state) == []
    clock.advance(100_000)
    assert eng.evaluate(state) == []


def test_starlink_offline_state_alerts(state, clock) -> None:
    eng = engine(R.StarlinkDownRule(id="s", for_seconds=0.0), clock)

    push(state, {"communication.starlink.state": "online"})
    assert eng.evaluate(state) == []

    push(state, {"communication.starlink.state": "obstructed"})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.ALERT


def test_starlink_going_quiet_alerts(state, clock) -> None:
    eng = engine(R.StarlinkDownRule(id="s", for_seconds=0.0, down_seconds=600.0), clock)
    push(state, {"communication.starlink.state": "online"})
    assert eng.evaluate(state) == []

    clock.advance(700)
    event = eng.evaluate(state)[0]
    assert "silent" in event.alert.message


# ------------------------------------------------------------------- bilge --


def test_bilge_counts_starts_not_duration(state, clock) -> None:
    rule = R.BilgeCyclingRule(id="b", for_seconds=0.0, max_cycles=3)
    eng = engine(rule, clock)

    # One long run is one cycle, however many times it is reported.
    for _ in range(10):
        clock.advance(5)
        push(state, {"notifications.bilge": True})
        assert eng.evaluate(state) == []


def test_bilge_cycling_alarms(state, clock) -> None:
    rule = R.BilgeCyclingRule(id="b", for_seconds=0.0, max_cycles=3)
    eng = engine(rule, clock)

    events = []
    for _ in range(3):
        clock.advance(60)
        push(state, {"notifications.bilge": True})
        events += eng.evaluate(state)
        clock.advance(60)
        push(state, {"notifications.bilge": False})
        events += eng.evaluate(state)

    # Raised once, on the third start - and not repeated when the pump stops,
    # because "3 cycles in the last hour" is still true.
    assert [e.kind for e in events] == ["raised"]
    assert events[0].alert.severity is R.Severity.ALARM
    assert "3 times" in events[0].alert.message


def test_bilge_forgets_old_cycles(state, clock) -> None:
    rule = R.BilgeCyclingRule(id="b", for_seconds=0.0, max_cycles=3, window_s=600.0)
    eng = engine(rule, clock)

    for _ in range(2):
        clock.advance(30)
        push(state, {"notifications.bilge": True})
        eng.evaluate(state)
        push(state, {"notifications.bilge": False})
        eng.evaluate(state)

    clock.advance(700)  # both cycles fall out of the window
    push(state, {"notifications.bilge": True})
    assert eng.evaluate(state) == []


def test_bilge_understands_notification_objects(state, clock) -> None:
    rule = R.BilgeCyclingRule(id="b", for_seconds=0.0, max_cycles=1)
    eng = engine(rule, clock)
    push(state, {"notifications.bilge": {"state": "alarm", "message": "bilge high"}})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.ALARM


# ------------------------------------------------------------ anchor watch --


ANCHORAGE = (36.8342, 10.2991)


def test_no_anchor_set_means_no_rule(state, clock) -> None:
    eng = engine(R.AnchorDragRule(id="a", for_seconds=0.0), clock)
    push(state, {"navigation.position": {"latitude": 36.9, "longitude": 10.35}})
    assert eng.evaluate(state) == []


def test_anchor_drag_fires_outside_the_circle(state, clock) -> None:
    rule = R.AnchorDragRule(id="a", for_seconds=0.0, margin_m=10.0)
    rule.set_anchor(*ANCHORAGE, radius_m=40.0)
    eng = engine(rule, clock)

    # Swinging inside the circle.
    push(state, {"navigation.position": {"latitude": 36.83440, "longitude": 10.29910}})
    assert eng.evaluate(state) == []

    # ~200 m away.
    push(state, {"navigation.position": {"latitude": 36.83600, "longitude": 10.29910}})
    event = eng.evaluate(state)[0]
    assert event.alert.severity is R.Severity.ALARM
    assert event.alert.data["distance_m"] > 40


def test_anchor_from_the_bus_is_preferred(state, clock) -> None:
    """If the Signal K anchor plugin is running, use its circle."""
    eng = engine(R.AnchorDragRule(id="a", for_seconds=0.0), clock)
    push(
        state,
        {
            "navigation.anchor.position": {"latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]},
            "navigation.anchor.maxRadius": 30.0,
            "navigation.position": {"latitude": 36.83600, "longitude": 10.29910},
        },
    )
    assert eng.evaluate(state)[0].alert.severity is R.Severity.ALARM


def test_anchor_drag_ignores_a_stale_position(state, clock) -> None:
    """A lost GPS fix is not a drag."""
    rule = R.AnchorDragRule(id="a", for_seconds=0.0, max_age=60.0)
    rule.set_anchor(*ANCHORAGE, radius_m=40.0)
    eng = engine(rule, clock)

    push(state, {"navigation.position": {"latitude": 36.83600, "longitude": 10.29910}})
    clock.advance(120)
    assert eng.evaluate(state) == []


def test_weighing_the_anchor_silences_the_drag_alarm_at_once(state, clock) -> None:
    """Found by listening to one. The hysteresis is there so a value hovering
    on a threshold does not flap; an anchor on the bow roller is not hovering,
    it is a person saying the question is over. Serving out another two minutes
    of siren after that teaches them to reach for the volume knob."""
    rule = R.AnchorDragRule(id="a", for_seconds=0.0, clear_after=120.0)
    rule.set_anchor(*ANCHORAGE, radius_m=40.0)
    eng = engine(rule, clock)

    push(state, {"navigation.position": {"latitude": 36.83600, "longitude": 10.29910}})
    assert [e.kind for e in eng.evaluate(state)] == ["raised"]

    rule.clear_anchor()
    clock.advance(1)
    push(state, {"navigation.position": {"latitude": 36.83600, "longitude": 10.29910}})

    # One second later, not a hundred and twenty.
    assert [e.kind for e in eng.evaluate(state)] == ["cleared"]
    assert eng.active == []


def test_a_condition_that_merely_eased_still_serves_out_the_hysteresis(state, clock) -> None:
    """The other half. A boat that sails back inside its circle has not ended
    the question, and the alarm must not chatter as it crosses the line."""
    rule = R.AnchorDragRule(id="a", for_seconds=0.0, clear_after=120.0)
    rule.set_anchor(*ANCHORAGE, radius_m=40.0)
    eng = engine(rule, clock)

    push(state, {"navigation.position": {"latitude": 36.83600, "longitude": 10.29910}})
    assert eng.evaluate(state) != []

    # Back inside, anchor still down: nothing yet.
    clock.advance(1)
    push(state, {"navigation.position": {"latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]}})
    assert eng.evaluate(state) == []
    assert eng.active != []

    clock.advance(121)
    push(state, {"navigation.position": {"latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]}})
    assert [e.kind for e in eng.evaluate(state)] == ["cleared"]


# --------------------------------------------------------------------- geo --


def test_distance_between_known_points() -> None:
    # One minute of latitude is a nautical mile, near enough.
    assert distance_m((36.0, 10.0), (36.0 + 1 / 60, 10.0)) == pytest.approx(1852, rel=0.01)
    assert distance_m(ANCHORAGE, ANCHORAGE) == 0.0


def test_distance_across_the_antimeridian() -> None:
    d = distance_m((0.0, 179.999), (0.0, -179.999))
    assert d == pytest.approx(222, rel=0.05)  # ~0.002 deg of longitude at the equator


def test_as_position_rejects_junk() -> None:
    assert as_position({"latitude": 36.0, "longitude": 10.0}) == (36.0, 10.0)
    assert as_position(None) is None
    assert as_position({"latitude": 36.0}) is None
    assert as_position({"latitude": "36", "longitude": "25"}) is None
    assert as_position({"latitude": 91.0, "longitude": 10.0}) is None
    assert as_position({"latitude": True, "longitude": False}) is None


def test_null_island_is_not_a_position() -> None:
    """A GPS with no fix reports 0,0. It is not there, and neither is the boat.

    gpsd regenerates NMEA from its own state and with no fix emits an RMC
    marked void with every field zeroed, which Signal K converts into a
    position like any other. Taken at face value the drag rule measures the
    distance from the anchor to the Gulf of Guinea and wakes the crew.
    """
    assert as_position({"latitude": 0.0, "longitude": 0.0}) is None
    assert as_position({"latitude": 0, "longitude": 0}) is None
    assert as_position({"latitude": 1e-9, "longitude": -1e-9}) is None

    # Only both together. The prime meridian and the equator are real places,
    # and a boat on one of them still has a position worth watching.
    assert as_position({"latitude": 36.83, "longitude": 0.0}) == (36.83, 0.0)
    assert as_position({"latitude": 0.0, "longitude": 10.30}) == (0.0, 10.30)


def test_a_no_fix_gps_does_not_raise_a_drag_alarm(state, clock) -> None:
    """The whole point of the guard above, seen from the drag rule."""
    rule = R.AnchorDragRule(id="a", for_seconds=0.0, margin_m=10.0)
    rule.set_anchor(*ANCHORAGE, radius_m=40.0)
    eng = engine(rule, clock)

    push(state, {"navigation.position": {"latitude": 36.83440, "longitude": 10.29910}})
    assert eng.evaluate(state) == []

    # The receiver loses its fix and starts insisting it is at Null Island,
    # which is 4000 km away and would otherwise read as a spectacular drag.
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})
    assert eng.evaluate(state) == [], "a lost fix raised a dragging alarm"


# ------------------------------------------------------- watching the watch --


def armed(state) -> None:
    push(
        state,
        {
            "navigation.anchor.position": {"latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]},
            "navigation.anchor.maxRadius": 40.0,
        },
    )


def test_nothing_is_blind_when_no_anchor_is_set(state, clock) -> None:
    eng = engine(R.AnchorWatchBlindRule(id="b", for_seconds=0.0), clock)
    clock.advance(600)
    assert eng.evaluate(state) == []


def test_an_armed_watch_with_no_fix_says_so(state, clock) -> None:
    eng = engine(R.AnchorWatchBlindRule(id="b", for_seconds=0.0), clock)
    armed(state)
    push(state, {"navigation.position": {"latitude": 36.83440, "longitude": 10.29910}})
    assert eng.evaluate(state) == []

    # A fix lost, but the receiver still talking: fresh, frequent, useless.
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})
    clock.advance(30)
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})
    event = eng.evaluate(state)[0]
    assert event.alert.severity is R.Severity.WARN
    assert "no fix" in event.alert.message
    assert event.alert.data["stale"] is False


def test_an_armed_watch_with_a_silent_receiver_says_so_differently(state, clock) -> None:
    eng = engine(R.AnchorWatchBlindRule(id="b", for_seconds=0.0, max_age=60.0), clock)
    armed(state)
    push(state, {"navigation.position": {"latitude": 36.83440, "longitude": 10.29910}})
    assert eng.evaluate(state) == []

    clock.advance(300)
    event = eng.evaluate(state)[0]
    assert "stopped reporting" in event.alert.message
    assert event.alert.data["stale"] is True


def test_going_blind_for_long_enough_becomes_an_alarm(state, clock) -> None:
    eng = engine(R.AnchorWatchBlindRule(id="b", for_seconds=0.0, alarm_after=900.0), clock)
    armed(state)
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})

    assert eng.evaluate(state)[0].alert.severity is R.Severity.WARN
    clock.advance(1000)
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})
    events = eng.evaluate(state)
    assert events[0].kind == "escalated"
    assert events[0].alert.severity is R.Severity.ALARM


def test_a_recovered_fix_clears_and_forgets(state, clock) -> None:
    eng = engine(R.AnchorWatchBlindRule(id="b", for_seconds=0.0, clear_after=0.0), clock)
    armed(state)
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})
    assert eng.evaluate(state)[0].kind == "raised"

    clock.advance(600)
    push(state, {"navigation.position": {"latitude": 36.83440, "longitude": 10.29910}})
    assert eng.evaluate(state)[0].kind == "cleared"

    # Blind again later starts its own clock rather than resuming the old one,
    # so a second dropout is a warning again and not instantly an alarm.
    clock.advance(60)
    push(state, {"navigation.position": {"latitude": 0.0, "longitude": 0.0}})
    assert eng.evaluate(state)[0].alert.severity is R.Severity.WARN


def test_default_rule_set_is_complete_and_unique() -> None:
    ids = [rule.id for rule in R.build_default_rules()]
    assert len(ids) == len(set(ids))
    assert set(ids) == {
        "shallow_water",
        "anchor_drag",
        "house_voltage",
        "battery_temperature",
        "locker_temperature",
        "bilge_cycling",
        "starlink_down",
        "cpu_temperature",
        "disk_free",
        "anchor_watch_blind",
        "wind_forecast",
        "bus_silent",
    }


def test_alert_serialises_for_the_logbook() -> None:
    import json

    rule = R.RangeRule(id="v", path="p", low=12.0, for_seconds=0.0)
    from .conftest import FakeClock

    clock = FakeClock()
    state = BoatState(clock=clock)
    eng = R.RuleEngine([rule], clock=clock)
    push(state, {"p": 11.0})
    event = eng.evaluate(state)[0]

    payload = event.as_dict()
    assert payload["event"] == "alert_raised"
    assert payload["severity"] == "warn"
    json.dumps(payload)


def test_severity_ordering_is_sane() -> None:
    assert R.SEVERITY_ORDER[R.Severity.EMERGENCY] > R.SEVERITY_ORDER[R.Severity.ALARM]
    assert R.SEVERITY_ORDER[R.Severity.ALARM] > R.SEVERITY_ORDER[R.Severity.WARN]
    assert math.isclose(R.c_to_k(0.0), 273.15)


# ------------------------------------------- config outliving the plugin --


def test_anchor_watch_survives_a_quiet_plugin(state, clock) -> None:
    """The anchor circle is configuration, not a reading, so it must not expire.

    The Signal K anchor plugin announces the anchor once when it goes down and
    then stays quiet. Treating maxRadius as a sensor value disarmed the drag
    alarm a few minutes after anchoring, silently.
    """
    rule = R.AnchorDragRule(id="a", for_seconds=0.0, max_age=60.0)
    eng = engine(rule, clock)

    push(
        state,
        {
            "navigation.anchor.position": {"latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]},
            "navigation.anchor.maxRadius": 40.0,
        },
    )

    # Hours later the plugin has said nothing more, and the boat drags.
    clock.advance(6 * 3600)
    push(state, {"navigation.position": {"latitude": 36.83600, "longitude": 10.29910}})

    events = eng.evaluate(state)
    assert [e.kind for e in events] == ["raised"]
    assert events[0].alert.severity is R.Severity.ALARM


def test_a_nonsense_radius_is_ignored(state, clock) -> None:
    """A zero or negative circle is not a watch circle."""
    rule = R.AnchorDragRule(id="a", for_seconds=0.0)
    eng = engine(rule, clock)
    push(
        state,
        {
            "navigation.anchor.position": {"latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]},
            "navigation.anchor.maxRadius": 0.0,
            "navigation.position": {"latitude": 36.83600, "longitude": 10.29910},
        },
    )
    assert eng.evaluate(state) == []


def test_shallow_water_survives_a_lost_gps_fix(state, clock) -> None:
    """SOG is the GPS; STW is the paddlewheel. Losing one is not
    losing the ability to know the boat is moving over a shoal.

    Run well past clear_after, or the alarm merely has not finished clearing
    yet and the test proves nothing.
    """
    rule = R.ShallowWaterRule(id="d", for_seconds=0.0, clear_after=60.0, max_age=60.0)
    eng = engine(rule, clock)

    push(
        state,
        {
            "environment.depth.belowTransducer": 2.4,
            "navigation.speedOverGround": 2.6,
            "navigation.speedThroughWater": 2.6,
        },
    )
    assert [e.kind for e in eng.evaluate(state)] == ["raised"]

    # The GPS drops out for ten minutes. Depth and paddlewheel keep reporting,
    # and the water keeps getting shallower.
    kinds: list[str] = []
    for _ in range(10):
        clock.advance(60)
        push(
            state,
            {"environment.depth.belowTransducer": 2.2, "navigation.speedThroughWater": 2.6},
        )
        kinds += [e.kind for e in eng.evaluate(state)]

    assert "cleared" not in kinds, "the depth alarm went quiet when the GPS died"
    assert rule.speed(state) == pytest.approx(2.6)  # fell back to the paddlewheel
    active = eng.active
    assert active and active[0].severity is R.Severity.ALARM


def test_shallow_water_still_needs_some_speed(state, clock) -> None:
    """Falling back to STW must not make the rule fire while stopped."""
    eng = engine(R.ShallowWaterRule(id="d", for_seconds=0.0), clock)
    push(
        state,
        {"environment.depth.belowTransducer": 2.2, "navigation.speedThroughWater": 0.05},
    )
    assert eng.evaluate(state) == []


# ---------------------------------------------------------------- the bus --


def test_a_silent_bus_is_reported_once_it_has_been_silent_a_while(state, clock) -> None:
    """Found the hard way: a udev rule pinned the wrong USB adapter as the
    gateway, Signal K opened it and said nothing was wrong for two days."""
    rule = R.BusSilentRule(id="bus", paths=("environment.depth.belowTransducer",),
                           quiet_after=900.0, for_seconds=0.0)
    eng = engine(rule, clock)

    # The GPS is arriving, so Signal K is fine. The instruments are not.
    push(state, {"navigation.position": {"latitude": 36.83, "longitude": 10.30}})
    assert eng.evaluate(state) == []

    clock.advance(901)
    push(state, {"navigation.position": {"latitude": 36.83, "longitude": 10.30}})
    events = eng.evaluate(state)
    assert [e.kind for e in events] == ["raised"]
    assert "no depth" in events[0].alert.message
    assert events[0].alert.severity is R.Severity.ALERT  # a message, not a siren


def test_one_instrument_waking_up_answers_it(state, clock) -> None:
    rule = R.BusSilentRule(id="bus", paths=("environment.depth.belowTransducer",),
                           quiet_after=900.0, for_seconds=0.0, clear_after=0.0)
    eng = engine(rule, clock)
    push(state, {"navigation.position": {"latitude": 36.83, "longitude": 10.30}})
    eng.evaluate(state)  # the clock on the silence starts here, not at import
    clock.advance(901)
    push(state, {"navigation.position": {"latitude": 36.83, "longitude": 10.30}})
    assert eng.evaluate(state) != []

    clock.advance(1)
    push(state, {"environment.depth.belowTransducer": 8.4})
    assert [e.kind for e in eng.evaluate(state)] == ["cleared"]


def test_it_stays_quiet_when_nothing_at_all_is_arriving(state, clock) -> None:
    """No deltas means Signal K is the problem, and the agent already shouts
    about that when it starts. Two alerts for one fault is one too many."""
    rule = R.BusSilentRule(id="bus", paths=("environment.depth.belowTransducer",),
                           quiet_after=1.0, for_seconds=0.0)
    eng = engine(rule, clock)
    clock.advance(900)
    assert eng.evaluate(state) == []


def test_the_gps_alone_never_counts_as_the_bus(state, clock) -> None:
    """Position, course and speed over ground come off the u-blox through
    gpsd, which is a different wire: they can be perfectly healthy while every
    instrument on the boat says nothing."""
    for path in ("navigation.position", "navigation.speedOverGround",
                 "navigation.courseOverGroundTrue"):
        assert path not in R.BUS_PATHS
    assert "environment.depth.belowTransducer" in R.BUS_PATHS
    assert "environment.wind.speedApparent" in R.BUS_PATHS

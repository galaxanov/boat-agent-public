from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.anchor import DEFAULT_RADIUS_M, AnchorFile, AnchorFix
from agent.logbook import Logbook
from agent.main import apply_anchor_file, parse_latlon
from agent.rules import AnchorDragRule, RuleEngine, Severity
from agent.state import BoatState

from .conftest import push

T0 = datetime(2026, 9, 5, 18, 30, tzinfo=UTC)
HOOK = (36.83, 10.30)


def fix(radius_m: float = 30.0, **kwargs) -> AnchorFix:
    return AnchorFix(
        latitude=kwargs.get("latitude", HOOK[0]),
        longitude=kwargs.get("longitude", HOOK[1]),
        radius_m=radius_m,
        set_at=kwargs.get("set_at", T0),
        note=kwargs.get("note", "test"),
    )


def north_of(metres: float) -> dict:
    return {"latitude": HOOK[0] + metres / 111320.0, "longitude": HOOK[1]}


# -------------------------------------------------------------------- file --


def test_a_written_anchor_reads_back(tmp_path: Path) -> None:
    anchor = AnchorFile(tmp_path / "anchor.json")

    assert anchor.write(fix(radius_m=42.0))
    reread = AnchorFile(tmp_path / "anchor.json").read()

    assert reread is not None
    assert (reread.latitude, reread.radius_m) == (HOOK[0], 42.0)
    assert reread.set_at == T0


def test_an_absent_file_means_no_anchor(tmp_path: Path) -> None:
    assert AnchorFile(tmp_path / "nothing.json").read() is None


def test_clearing_writes_an_empty_object(tmp_path: Path) -> None:
    path = tmp_path / "anchor.json"
    anchor = AnchorFile(path)
    anchor.write(fix())

    anchor.write(None)

    assert json.loads(path.read_text()) == {}
    assert AnchorFile(path).read() is None


def test_a_corrupt_file_is_ignored_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "anchor.json"
    path.write_text("{not json at all")

    assert AnchorFile(path).read() is None


def test_an_impossible_position_is_refused(tmp_path: Path) -> None:
    """A typo in the file must not arm a watch around the wrong place."""
    path = tmp_path / "anchor.json"
    path.write_text(json.dumps({"latitude": 936.83, "longitude": 10.3, "radius_m": 30}))

    assert AnchorFile(path).read() is None


def test_a_zero_radius_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "anchor.json"
    path.write_text(json.dumps({"latitude": 36.83, "longitude": 10.3, "radius_m": 0}))

    assert AnchorFile(path).read() is None


def test_a_missing_radius_falls_back_to_the_default(tmp_path: Path) -> None:
    path = tmp_path / "anchor.json"
    path.write_text(json.dumps({"latitude": 36.83, "longitude": 10.3}))

    read = AnchorFile(path).read()

    assert read is not None
    assert read.radius_m == DEFAULT_RADIUS_M


def test_an_unwritable_path_is_reported_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("I am a file, not a directory")

    assert AnchorFile(blocker / "anchor.json").write(fix()) is False


def test_changed_notices_a_new_file(tmp_path: Path) -> None:
    path = tmp_path / "anchor.json"
    anchor = AnchorFile(path)
    anchor.read()

    assert not anchor.changed()  # still absent

    AnchorFile(path).write(fix())
    assert anchor.changed()


# ------------------------------------------------------------------- agent --


def test_the_hand_set_anchor_arms_the_drag_rule(tmp_path: Path, clock) -> None:
    """The whole point: a file on disk makes the alarm live."""
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix(radius_m=30.0))

    state = BoatState(clock=clock)
    rule = AnchorDragRule(id="anchor_drag", for_seconds=0.0)
    engine = RuleEngine([rule], clock=clock)
    with Logbook(tmp_path / "logs") as logbook:
        apply_anchor_file(AnchorFile(path), state, logbook)

    push(state, {"navigation.position": north_of(120)})
    events = engine.evaluate(state)

    assert [e.alert.severity for e in events] == [Severity.ALARM]
    assert "Dragging" in events[0].alert.message


def test_setting_the_anchor_is_logged(tmp_path: Path, clock) -> None:
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix())
    state = BoatState(clock=clock)

    with Logbook(tmp_path / "logs") as logbook:
        apply_anchor_file(AnchorFile(path), state, logbook)

    written = sorted((tmp_path / "logs").glob("*.jsonl"))[0].read_text()
    assert '"event": "anchor_set"' in written


def test_weighing_the_anchor_disarms_the_rule(tmp_path: Path, clock) -> None:
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix(radius_m=30.0))
    watcher = AnchorFile(path)

    state = BoatState(clock=clock)
    rule = AnchorDragRule(id="anchor_drag", for_seconds=0.0, clear_after=0.0)
    engine = RuleEngine([rule], clock=clock)

    with Logbook(tmp_path / "logs") as logbook:
        apply_anchor_file(watcher, state, logbook)
        push(state, {"navigation.position": north_of(120)})
        assert engine.evaluate(state)  # dragging

        AnchorFile(path).write(None)
        clock.advance(1)
        apply_anchor_file(watcher, state, logbook)

    # Clearing the file is not enough on its own: the rule never expires the
    # anchor, so the agent has to publish a zero radius to stand it down.
    events = engine.evaluate(state)
    assert [e.kind for e in events] == ["cleared"]
    assert engine.alert_for("anchor_drag") is None


def test_the_anchor_is_refreshed_so_it_cannot_go_stale(tmp_path: Path, clock) -> None:
    """A watch that ages out at 0300 is worse than none: you think you have one."""
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix())
    watcher = AnchorFile(path)
    state = BoatState(clock=clock)

    with Logbook(tmp_path / "logs") as logbook:
        apply_anchor_file(watcher, state, logbook)
        clock.advance(6 * 3600)  # six hours with nothing new on the bus
        apply_anchor_file(watcher, state, logbook)

    assert state.age("navigation.anchor.position") == 0.0


def test_a_second_pass_does_not_log_it_again(tmp_path: Path, clock) -> None:
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix())
    watcher = AnchorFile(path)
    state = BoatState(clock=clock)

    with Logbook(tmp_path / "logs") as logbook:
        for _ in range(5):
            apply_anchor_file(watcher, state, logbook)

    written = sorted((tmp_path / "logs").glob("*.jsonl"))[0].read_text()
    assert written.count("anchor_set") == 1


def test_resetting_the_anchor_logs_the_new_one(tmp_path: Path, clock) -> None:
    """Re-anchoring in the same cove is a new fix, not a duplicate."""
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix())
    watcher = AnchorFile(path)
    state = BoatState(clock=clock)

    with Logbook(tmp_path / "logs") as logbook:
        apply_anchor_file(watcher, state, logbook)
        AnchorFile(path).write(fix(set_at=T0 + timedelta(hours=2)))
        apply_anchor_file(watcher, state, logbook)

    written = sorted((tmp_path / "logs").glob("*.jsonl"))[0].read_text()
    assert written.count("anchor_set") == 2


# --------------------------------------------------------------- arguments --


def test_latlon_parsing() -> None:
    assert parse_latlon("36.83,10.30") == (36.83, 10.30)
    assert parse_latlon(" 36.83 , 10.30 ") == (36.83, 10.30)
    assert parse_latlon("-36.83,-10.30") == (-36.83, -10.30)


def test_nonsense_latlon_is_rejected() -> None:
    for text in ("36.83", "north a bit", "936.83,10.3", "36.83,10.30,7", ""):
        assert parse_latlon(text) is None


# ------------------------------------------------- the weather at arming --


def _store(winds, gusts=None, at=None):
    """A weather store holding a forecast starting now."""
    from agent import weather as W

    start = at or datetime.now(UTC)
    count = len(winds)
    payload = {
        "latitude": HOOK[0],
        "longitude": HOOK[1],
        "hourly": {
            "time": [
                (start + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(count)
            ],
            "wind_speed_10m": winds,
            "wind_gusts_10m": gusts if gusts is not None else [None] * count,
            "wind_direction_10m": [0.0] * count,
            "pressure_msl": [1013.0] * count,
            "temperature_2m": [26.0] * count,
        },
    }
    store = W.WeatherStore()
    store.record(W.build_forecast(HOOK, payload, None, now=start), start)
    return store


def _forecast_entry(written: str) -> dict:
    return next(json.loads(x) for x in written.splitlines() if "anchor_forecast" in x)


def _arm(tmp_path: Path, clock, store, config) -> str:
    path = tmp_path / "anchor.json"
    AnchorFile(path).write(fix(set_at=datetime.now(UTC)))
    state = BoatState(clock=clock)
    with Logbook(tmp_path / "logs") as logbook:
        apply_anchor_file(AnchorFile(path), state, logbook, store, config)
    return sorted((tmp_path / "logs").glob("*.jsonl"))[0].read_text()


def test_arming_the_watch_records_what_the_night_was_forecast_to_do(
    tmp_path: Path, clock
) -> None:
    from agent.config import Config

    written = _arm(tmp_path, clock, _store([6.0] * 24), Config())

    assert '"event": "anchor_forecast"' in written
    entry = _forecast_entry(written)
    assert entry["over_threshold"] is None  # a quiet night, said so anyway
    assert entry["outlook_h"] == 18.0  # tonight, not the next twelve hours


def test_arming_into_a_blow_says_so(tmp_path: Path, clock) -> None:
    from agent.config import Config

    # Quiet until 0300, then F7. A twelve-hour window would not have seen it.
    written = _arm(tmp_path, clock, _store([6.0] * 14 + [16.0] * 10), Config())

    entry = _forecast_entry(written)
    assert entry["over_threshold"] == "F6 or more sustained"
    assert "up to 31 kn" in entry["summary"]


def test_arming_when_the_forecast_could_not_be_fetched_admits_it(tmp_path: Path, clock) -> None:
    """Believing somebody checked is worse than knowing nobody did."""
    from agent.config import Config
    from agent.weather import WeatherStore

    # Tried and failed, rather than not tried yet: those read differently.
    store = WeatherStore()
    store.record_failure("no route to the forecast", datetime.now(UTC))
    written = _arm(tmp_path, clock, store, Config())

    entry = _forecast_entry(written)
    assert entry["summary"] == "no forecast to check tonight against"
    assert entry["over_threshold"] is None


def test_the_night_line_names_which_threshold_was_crossed() -> None:
    from agent.main import night_ahead
    from agent.weather import Hour, Outlook

    def hour(wind=None, gust=None):
        return Hour(time=datetime(2026, 9, 7, 3, 0, tzinfo=UTC), wind_ms=wind, gust_ms=gust)

    # A steady blow, and a quiet night with vicious gusts, read differently.
    _, steady = night_ahead(Outlook(hours=18.0, windiest=hour(wind=14.0)))
    _, gusty = night_ahead(Outlook(hours=18.0, windiest=hour(wind=7.0), gustiest=hour(gust=18.0)))
    _, both = night_ahead(
        Outlook(hours=18.0, windiest=hour(wind=14.0), gustiest=hour(gust=18.0))
    )
    _, quiet = night_ahead(Outlook(hours=18.0, windiest=hour(wind=5.0)))

    assert steady == "F6 or more sustained"
    assert gusty == "gusts over 33 kn"
    assert both == "F6 or more sustained, with gusts over 33 kn"
    assert quiet is None


def test_no_outlook_at_all_is_its_own_sentence() -> None:
    from agent.main import night_ahead
    from agent.weather import Outlook

    assert night_ahead(None) == ("no forecast to check tonight against", None)
    line, reason = night_ahead(Outlook(hours=18.0))
    assert "says nothing about the next 18 h" in line
    assert reason is None


def test_a_restart_does_not_claim_nobody_checked_the_weather(tmp_path: Path, clock) -> None:
    """Seen live: the agent restarts with the hook already down, and the weather
    loop has not had its first tick yet. "No forecast" is true for about a
    minute and misleading for as long as anyone remembers reading it."""
    from agent.config import Config
    from agent.weather import WeatherStore

    written = _arm(tmp_path, clock, WeatherStore(), Config())

    entry = _forecast_entry(written)
    assert entry["summary"] == "the forecast has not come in yet; the wind rule takes it from here"
    assert entry["over_threshold"] is None


def test_the_forecast_being_switched_off_reads_differently_again(tmp_path: Path, clock) -> None:
    from agent.config import Config

    written = _arm(tmp_path, clock, None, Config())

    entry = _forecast_entry(written)
    assert entry["summary"] == "the forecast is turned off, so nothing has looked at tonight"


def test_arming_with_no_position_says_that_rather_than_no_forecast(
    tmp_path: Path, clock
) -> None:
    """A hand-set anchor with the GPS blind: there is nowhere to forecast for."""
    from agent.config import Config
    from agent.weather import NO_POSITION, WeatherStore

    store = WeatherStore()
    store.blocked = NO_POSITION
    written = _arm(tmp_path, clock, store, Config())

    assert _forecast_entry(written)["summary"] == NO_POSITION

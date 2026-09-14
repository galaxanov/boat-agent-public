from __future__ import annotations

from datetime import UTC, datetime

from agent.state import BoatState

from .conftest import feed


def test_hello_records_self_context(hello: dict, clock) -> None:
    state = BoatState(clock=clock)
    state.apply_hello(hello)
    assert state.self_context == hello["self"]


def test_latest_value_wins(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)
    # Depth is reported three times in the fixture; the last one is 10.9.
    assert state.value("environment.depth.belowTransducer") == 10.9
    assert state.path_counts["environment.depth.belowTransducer"] == 3


def test_source_from_dollar_source(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)
    assert state.get("electrical.solar.mppt.panelPower").source == "victron-ble.mppt"
    assert state.get("environment.wind.speedApparent").source == "ngx1.18"


def test_source_falls_back_to_label(clock) -> None:
    state = BoatState(clock=clock)
    state.apply_delta(
        {
            "updates": [
                {
                    "source": {"label": "ngx1", "pgn": 128267},
                    "values": [{"path": "environment.depth.belowTransducer", "value": 5.0}],
                }
            ]
        }
    )
    assert state.get("environment.depth.belowTransducer").source == "ngx1"


def test_other_vessels_are_ignored(deltas: list[dict], clock) -> None:
    """An AIS target must not overwrite our own position."""
    state = BoatState(clock=clock)
    feed(state, deltas)
    position = state.value("navigation.position")
    assert position == {"latitude": 36.8342, "longitude": 10.2991}
    assert state.value("navigation.speedOverGround") == 0.12


def test_meta_updates_store_nothing(clock) -> None:
    state = BoatState(clock=clock)
    stored = state.apply_delta(
        {
            "updates": [
                {
                    "$source": "ngx1.35",
                    "meta": [
                        {
                            "path": "environment.depth.belowTransducer",
                            "value": {"units": "m"},
                        }
                    ],
                }
            ]
        }
    )
    assert stored == []
    assert len(state) == 0


def test_malformed_entries_are_skipped_not_raised(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)
    # The junk update has an empty path, a value-less entry, a path-less entry
    # and a bare string. None of them may land in the state.
    assert "" not in state
    assert "navigation.headingMagnetic" not in state


def test_garbage_delta_does_not_raise(clock) -> None:
    state = BoatState(clock=clock)
    for junk in ({}, {"updates": "nope"}, {"updates": [None]}, {"updates": [{"values": None}]}):
        assert state.apply_delta(junk) == []


def test_unparseable_timestamp_still_stores_value(deltas: list[dict], hello: dict, clock) -> None:
    """A source with a broken clock must not cost us the reading."""
    state = BoatState(clock=clock)
    state.apply_hello(hello)

    bad = next(
        message
        for message in deltas
        if "updates" in message
        and any(u.get("timestamp") == "not-a-timestamp" for u in message["updates"])
    )
    state.apply_delta(bad)

    sample = state.get("environment.depth.belowTransducer")
    assert sample.value == 11.2
    assert sample.timestamp is None
    assert sample.received == clock.now  # we still know when we saw it


def test_timestamp_is_parsed_with_timezone(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)
    sample = state.get("electrical.solar.mppt.panelPower")
    assert sample.timestamp == datetime(2026, 8, 22, 9, 15, 2, tzinfo=UTC)


def test_age_and_staleness_use_receipt_time(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)

    assert state.age("environment.depth.belowTransducer") == 0.0
    assert not state.is_stale("environment.depth.belowTransducer", max_age=60)

    clock.advance(120)
    assert state.age("environment.depth.belowTransducer") == 120.0
    assert state.is_stale("environment.depth.belowTransducer", max_age=60)


def test_missing_path_is_stale_not_an_error(clock) -> None:
    state = BoatState(clock=clock)
    assert state.age("environment.depth.belowTransducer") is None
    assert state.is_stale("environment.depth.belowTransducer", max_age=1)
    assert state.value("environment.depth.belowTransducer", "unknown") == "unknown"


def test_fresh_paths_filters_by_age(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)
    clock.advance(30)
    state.apply_delta(
        {
            "updates": [
                {
                    "$source": "ngx1.35",
                    "values": [{"path": "environment.depth.belowTransducer", "value": 9.8}],
                }
            ]
        }
    )
    fresh = state.fresh_paths(max_age=10)
    assert fresh == ["environment.depth.belowTransducer"]


def test_snapshot_is_json_shaped(deltas: list[dict], clock) -> None:
    import json

    state = BoatState(clock=clock)
    feed(state, deltas)
    clock.advance(400)
    snapshot = state.snapshot(stale_after=300)

    assert snapshot["counts"]["paths"] == len(state)
    assert snapshot["counts"]["deltas"] == state.deltas_seen
    depth = snapshot["values"]["environment.depth.belowTransducer"]
    assert depth["value"] == 10.9
    assert depth["age_s"] == 400.0
    assert depth["src"] == "ngx1.35"
    assert depth["stale"] is True
    json.dumps(snapshot)  # must survive the logbook


def test_snapshot_without_stale_after_has_no_stale_flag(deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)
    snapshot = state.snapshot()
    assert "stale" not in snapshot["values"]["environment.depth.belowTransducer"]


def test_named_vessel_dropped_before_hello(deltas: list[dict], clock) -> None:
    """Without the hello frame, self is unknown, so named contexts are dropped.

    Signal K always sends hello first, so this costs nothing in practice - and
    it stops an AIS target's position from becoming ours.
    """
    state = BoatState(clock=clock)
    for message in deltas:
        if "updates" in message:
            state.apply_delta(message)
    assert len(state) == 0
    assert state.deltas_seen == 0

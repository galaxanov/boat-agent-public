from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import digest as D

NOW = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
KELVIN = 273.15


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def snapshot(hours_ago: float, **values) -> dict:
    return {
        "type": "snapshot",
        "ts": at(hours_ago),
        "values": {path: {"value": value, "age_s": 1.0} for path, value in values.items()},
        "counts": {"paths": len(values), "deltas": 1, "values": len(values)},
    }


def write_log(log_dir: Path, day: str, records: list[dict], compressed: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r) + "\n" for r in records)
    path = log_dir / (f"{day}.jsonl.gz" if compressed else f"{day}.jsonl")
    if compressed:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------------ window --


def test_the_window_spans_midnight(tmp_path: Path) -> None:
    """A 0600 entry is mostly about yesterday, so both files are read."""
    write_log(tmp_path, "2026-09-04", [snapshot(20, **{D.VOLTAGE: 12.9})])
    write_log(tmp_path, "2026-09-05", [snapshot(1, **{D.VOLTAGE: 13.4})])

    records = D.read_window(tmp_path, NOW)

    assert len(records) == 2
    assert [r["ts"] for r in records] == sorted(r["ts"] for r in records)


def test_a_rotated_day_is_still_read(tmp_path: Path) -> None:
    """Yesterday is gzipped the moment today opens - the digest still needs it."""
    write_log(tmp_path, "2026-09-04", [snapshot(20, **{D.VOLTAGE: 12.9})], compressed=True)

    assert len(D.read_window(tmp_path, NOW)) == 1


def test_records_outside_the_window_are_ignored(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-04",
        [snapshot(30, **{D.VOLTAGE: 11.0}), snapshot(10, **{D.VOLTAGE: 13.0})],
    )

    records = D.read_window(tmp_path, NOW)

    assert len(records) == 1
    assert records[0]["values"][D.VOLTAGE]["value"] == 13.0


def test_a_corrupt_line_does_not_stop_the_digest(tmp_path: Path) -> None:
    path = write_log(tmp_path, "2026-09-05", [snapshot(1, **{D.VOLTAGE: 13.1})])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json\n")

    assert len(D.read_window(tmp_path, NOW)) == 1


def test_a_missing_log_directory_is_an_empty_digest(tmp_path: Path) -> None:
    facts = D.build_digest(tmp_path / "nothing-here", now=NOW)
    assert not facts.has_data
    assert "Nothing in the log" in D.render_entry(facts)


# ------------------------------------------------------------------- facts --


def test_extremes_come_from_the_whole_window(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            snapshot(5, **{D.VOLTAGE: 12.6, D.SOLAR_W: 0.0, D.LOCKER_T: 28 + KELVIN}),
            snapshot(3, **{D.VOLTAGE: 13.9, D.SOLAR_W: 640.0, D.LOCKER_T: 41 + KELVIN}),
            snapshot(1, **{D.VOLTAGE: 13.2, D.SOLAR_W: 240.0, D.LOCKER_T: 33 + KELVIN}),
        ],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert (facts.voltage.low, facts.voltage.high) == (12.6, 13.9)
    assert facts.solar_w.high == 640.0
    assert facts.locker_c.low == pytest.approx(28.0)
    assert facts.locker_c.high == pytest.approx(41.0)


def test_a_stale_reading_is_not_a_reading(tmp_path: Path) -> None:
    """The last thing a dead instrument said is not a measurement of the night."""
    stale = snapshot(2, **{D.VOLTAGE: 9.8})
    stale["values"][D.VOLTAGE]["stale"] = True
    write_log(tmp_path, "2026-09-05", [stale, snapshot(1, **{D.VOLTAGE: 13.1})])

    facts = D.build_digest(tmp_path, now=NOW)

    assert facts.voltage.low == 13.1


def test_swing_is_measured_from_the_first_fix(tmp_path: Path) -> None:
    here = {"latitude": 36.83, "longitude": 10.30}
    there = {"latitude": 36.8309, "longitude": 10.30}  # ~100 m north
    write_log(
        tmp_path,
        "2026-09-05",
        [snapshot(4, **{D.POSITION: here}), snapshot(2, **{D.POSITION: there})],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert facts.swing_m == pytest.approx(100, abs=5)


def test_alarms_and_states_are_collected(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            {
                "type": "alert",
                "event": "alert_raised",
                "rule": "anchor_drag",
                "severity": "alarm",
                "message": "Dragging: 50 m from the anchor, watch circle 30 m",
                "ts": at(4),
            },
            {
                "type": "alert",
                "event": "alert_cleared",
                "rule": "anchor_drag",
                "severity": "alarm",
                "message": "Dragging: 50 m from the anchor, watch circle 30 m",
                "ts": at(3.5),
            },
            {
                "type": "state",
                "from": "anchored",
                "to": "underway-sail",
                "reason": "making way",
                "ts": at(2),
            },
        ],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert len(facts.alerts) == 2
    assert facts.unresolved == []  # raised and cleared inside the window
    assert facts.states[0]["to"] == "underway-sail"


def test_an_alarm_that_never_cleared_is_called_out(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            {
                "type": "alert",
                "event": "alert_raised",
                "rule": "house_voltage",
                "severity": "warn",
                "message": "House voltage low: 11.9 V",
                "ts": at(6),
            }
        ],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert [a["rule"] for a in facts.unresolved] == ["house_voltage"]
    assert "still active" in D.render_entry(facts)


def test_restarts_are_counted(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            {"type": "event", "event": "agent_started", "ts": at(5)},
            {"type": "event", "event": "signalk_disconnected", "reason": "closed", "ts": at(4)},
            {"type": "event", "event": "agent_started", "ts": at(4)},
        ],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert (facts.restarts, facts.disconnects) == (2, 1)


# ------------------------------------------------------------------ output --


def test_an_absent_instrument_gets_no_row(tmp_path: Path) -> None:
    """No depth sounder reading all night is not a depth of zero."""
    write_log(tmp_path, "2026-09-05", [snapshot(2, **{D.VOLTAGE: 13.1})])

    entry = D.render_entry(D.build_digest(tmp_path, now=NOW))

    assert "House voltage" in entry
    assert "Depth" not in entry


# -------------------------------------------------------------------- file --


def test_newest_entry_goes_on_top(tmp_path: Path) -> None:
    path = tmp_path / "ships-log.md"

    D.update_file(path, "## 2026-09-04 06:00 UTC\n\nolder\n")
    D.update_file(path, "## 2026-09-05 06:00 UTC\n\nnewer\n")

    text = path.read_text(encoding="utf-8")
    assert text.startswith(D.TITLE)
    assert text.index("2026-09-05") < text.index("2026-09-04")


def test_old_entries_fall_off_the_bottom(tmp_path: Path) -> None:
    path = tmp_path / "ships-log.md"

    for day in range(1, 6):
        D.update_file(path, f"## day {day}\n\nbody\n", keep=3)

    text = path.read_text(encoding="utf-8")
    assert text.count("## day") == 3
    assert "day 5" in text and "day 2" not in text


def test_the_file_is_replaced_not_appended(tmp_path: Path) -> None:
    """Rewrite-and-rename, so a power cut cannot leave half a file."""
    path = tmp_path / "ships-log.md"
    D.update_file(path, "## one\n\nbody\n")
    D.update_file(path, "## two\n\nbody\n")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["ships-log.md"]


def test_an_unwritable_path_is_reported_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("I am a file, not a directory")

    assert D.update_file(blocker / "ships-log.md", "## one\n\nbody\n") is False


def test_headings_read_like_english(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            {
                "type": "alert",
                "event": "alert_raised",
                "rule": "house_voltage",
                "severity": "warn",
                "message": "House voltage low: 11.9 V",
                "ts": at(6),
            },
            snapshot(2, **{D.VOLTAGE: 11.9}),
        ],
    )

    entry = D.render_entry(D.build_digest(tmp_path, now=NOW))

    assert "1 alarm still active" in entry
    assert "alarm(s)" not in entry
    assert "1 snapshot." in entry


# ---------------------------------------------------------------- distance --


def north_of(metres: float) -> dict:
    return {"latitude": 36.83 + metres / 111320.0, "longitude": 10.30}


def test_ground_covered_is_summed_between_fixes(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            snapshot(5, **{D.POSITION: north_of(0), D.SOG: 3.0}),
            snapshot(4, **{D.POSITION: north_of(1852), D.SOG: 3.0}),
            snapshot(3, **{D.POSITION: north_of(3704), D.SOG: 3.0}),
        ],
    )

    run = D.build_digest(tmp_path, now=NOW).distance_nm

    assert run is not None
    assert run[0] == pytest.approx(2.0, abs=0.05)
    assert run[1] == "estimated from fixes"


def test_swinging_at_anchor_is_not_a_passage(tmp_path: Path) -> None:
    """A night on the hook must not read as miles covered."""
    write_log(
        tmp_path,
        "2026-09-05",
        [
            snapshot(6, **{D.POSITION: north_of(0), D.SOG: 0.05}),
            snapshot(5, **{D.POSITION: north_of(28), D.SOG: 0.08}),
            snapshot(4, **{D.POSITION: north_of(-25), D.SOG: 0.03}),
            snapshot(3, **{D.POSITION: north_of(30), D.SOG: 0.06}),
        ],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert facts.distance_nm is None
    assert facts.swing_m == pytest.approx(30, abs=3)


def test_gps_jitter_under_way_is_not_counted(tmp_path: Path) -> None:
    """Metre-scale wobble between fixes is noise, not progress."""
    write_log(
        tmp_path,
        "2026-09-05",
        [
            snapshot(5, **{D.POSITION: north_of(0), D.SOG: 2.0}),
            snapshot(4, **{D.POSITION: north_of(4), D.SOG: 2.0}),
            snapshot(3, **{D.POSITION: north_of(9), D.SOG: 2.0}),
        ],
    )

    assert D.build_digest(tmp_path, now=NOW).distance_nm is None


def test_the_trip_log_beats_the_estimate(tmp_path: Path) -> None:
    """A real instrument reading wins over joining fixes with straight lines."""
    write_log(
        tmp_path,
        "2026-09-05",
        [
            snapshot(5, **{D.POSITION: north_of(0), D.SOG: 3.0, D.TRIP_LOG: 100_000.0}),
            snapshot(3, **{D.POSITION: north_of(1852), D.SOG: 3.0, D.TRIP_LOG: 118_520.0}),
        ],
    )

    run = D.build_digest(tmp_path, now=NOW).distance_nm

    assert run is not None
    assert run[0] == pytest.approx(10.0, abs=0.05)
    assert run[1] == "trip log"


# -------------------------------------------------------------- anchorages --


def anchored(hours_ago: float, position: dict, anchor: dict) -> dict:
    record = snapshot(hours_ago, **{D.POSITION: position, D.ANCHOR: anchor, D.SOG: 0.05})
    record["derived"] = {"state": "anchored", "confidence": "certain"}
    return record


def test_where_the_boat_anchored_and_for_how_long(tmp_path: Path) -> None:
    hook = {"latitude": 36.83, "longitude": 10.30}
    write_log(
        tmp_path,
        "2026-09-05",
        [
            anchored(12, north_of(15), hook),
            anchored(6, north_of(31), hook),
            anchored(2, north_of(10), hook),
        ],
    )

    facts = D.build_digest(tmp_path, now=NOW)

    assert len(facts.anchorages) == 1
    spot = facts.anchorages[0]
    assert spot.hours == pytest.approx(10.0, abs=0.1)
    assert spot.swing_m == pytest.approx(31, abs=3)
    assert "36.83000N 10.30000E" in D.render_entry(facts)


def test_weighing_anchor_closes_the_anchorage(tmp_path: Path) -> None:
    hook = {"latitude": 36.83, "longitude": 10.30}
    under_way = snapshot(2, **{D.POSITION: north_of(3000), D.SOG: 3.2})
    under_way["derived"] = {"state": "underway-sail"}
    write_log(tmp_path, "2026-09-05", [anchored(8, north_of(12), hook), under_way])

    facts = D.build_digest(tmp_path, now=NOW)

    assert len(facts.anchorages) == 1
    assert facts.anchorages[0].until is not None


# ---------------------------------------------------------------- forecast --


def forecast(hours_ago: float, wind_ms: float, gust_ms: float | None = None) -> dict:
    return {
        "type": "forecast",
        "ts": at(hours_ago),
        "fetched_at": at(hours_ago),
        "outlook_h": 12.0,
        "waves": True,
        "grid_offset_m": 6000,
        "now": {"time": at(hours_ago), "wind_ms": 5.0},
        "max_wind": {
            "time": at(hours_ago - 6),
            "wind_ms": wind_ms,
            "direction_rad": 0.0,
            **({"gust_ms": gust_ms} if gust_ms is not None else {}),
        },
        "max_wave": {"time": at(hours_ago - 6), "wave_m": 1.8},
    }


def test_the_last_forecast_of_the_window_is_the_one_kept() -> None:
    records = [forecast(20, 8.0), forecast(2, 14.0)]
    facts = D.summarise_day(records, NOW - timedelta(hours=24), NOW)

    assert facts.forecast is not None
    assert facts.forecast["max_wind"]["wind_ms"] == 14.0


def test_the_forecast_reads_as_the_future_and_not_as_a_reading() -> None:
    records = [snapshot(3, **{D.VOLTAGE: 13.2}), forecast(1, 13.5, gust_ms=17.0)]
    facts = D.summarise_day(records, NOW - timedelta(hours=24), NOW)
    entry = D.render_entry(facts)

    assert "**Forecast**" in entry
    assert "next 12 h" in entry
    # Under everything that was actually measured, never mixed in with it.
    assert entry.index("**Forecast**") > entry.index("| reading |")
    assert "up to 26 kn" in entry  # 13.5 m/s
    assert "gusting 33 kn" in entry
    assert "from the N" in entry
    assert "sea to 1.8 m" in entry


def test_no_forecast_means_no_line_about_one() -> None:
    facts = D.summarise_day([snapshot(3, **{D.VOLTAGE: 13.2})], NOW - timedelta(hours=24), NOW)
    assert facts.forecast is None
    assert "Forecast" not in D.render_entry(facts)


def test_a_forecast_with_no_wind_figure_says_nothing() -> None:
    """Half a forecast is not a forecast, and is never padded out."""
    record = {"type": "forecast", "ts": at(1), "outlook_h": 12.0, "max_wind": {"time": at(1)}}
    facts = D.summarise_day([record], NOW - timedelta(hours=24), NOW)

    assert facts.forecast is not None
    assert D._forecast_line(facts.forecast) is None


def test_the_brief_names_the_forecast_so_it_cannot_read_as_measured() -> None:
    facts = D.summarise_day([forecast(1, 13.5)], NOW - timedelta(hours=24), NOW)
    brief = facts.as_dict()

    assert brief["forecast_for_the_hours_ahead"]["max_wind"]["wind_ms"] == 13.5
    assert "forecast" in brief["note"]


# ----------------------------------------------------------------- silence --


def silenced(hours_ago: float, since_hours_ago: float, note: str = "") -> dict:
    """One of the lines the running agent writes twice an hour while quiet."""
    return {
        "type": "event",
        "ts": at(hours_ago),
        "event": "silenced",
        "since": at(since_hours_ago),
        "note": note,
    }


def test_a_silence_that_ended_is_measured_and_said(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "2026-09-05",
        [
            silenced(5, 5, "alongside in the marina"),
            silenced(4.5, 5, "alongside in the marina"),
            {"type": "event", "ts": at(2), "event": "unsilenced"},
            snapshot(1, **{D.VOLTAGE: 13.4}),
        ],
    )
    facts = D.build_digest(tmp_path, NOW)

    assert facts.silenced_s == pytest.approx(3 * 3600)
    assert facts.still_silenced is False
    assert facts.silence_notes == ["alongside in the marina"]

    entry = D.render_entry(facts)
    assert "silenced for 3 h" in entry
    assert "alongside in the marina" in entry
    assert "reached nobody" in entry


def test_a_silence_still_standing_reaches_the_heading(tmp_path: Path) -> None:
    """The morning entry is where somebody finds out she is still quiet."""
    write_log(
        tmp_path,
        "2026-09-05",
        [silenced(6, 6), silenced(1, 6), snapshot(1, **{D.VOLTAGE: 13.4})],
    )
    facts = D.build_digest(tmp_path, NOW)

    assert facts.still_silenced is True
    assert facts.silenced_s == pytest.approx(6 * 3600)

    entry = D.render_entry(facts)
    assert "ALERTS SILENCED" in entry.splitlines()[0]
    assert "still are" in entry
    assert "boat --unsilence" in entry


def test_a_silence_older_than_the_window_counts_only_from_its_start(tmp_path: Path) -> None:
    """The entry may only claim the 24 h it can actually see."""
    write_log(tmp_path, "2026-09-05", [silenced(3, 400), snapshot(1, **{D.VOLTAGE: 13.4})])
    facts = D.build_digest(tmp_path, NOW)

    assert facts.silenced_s == pytest.approx(24 * 3600)
    assert facts.still_silenced is True


def test_a_quiet_day_with_no_silence_says_nothing_about_one(tmp_path: Path) -> None:
    write_log(tmp_path, "2026-09-05", [snapshot(1, **{D.VOLTAGE: 13.4})])
    facts = D.build_digest(tmp_path, NOW)

    assert facts.silenced_s == 0
    assert D._silence_line(facts) is None
    assert "silenced" not in D.render_entry(facts).lower()


def test_the_brief_handed_to_the_model_says_nobody_was_told(tmp_path: Path) -> None:
    """Otherwise a summary describes a quiet night that nobody was watching."""
    write_log(tmp_path, "2026-09-05", [silenced(6, 6, "hauled out"), snapshot(1)])
    brief = D.build_digest(tmp_path, NOW).as_dict()

    assert brief["alerts_still_silenced"] is True
    assert brief["alerts_silenced_seconds"] == pytest.approx(6 * 3600, abs=2)
    assert brief["why_silenced"] == ["hauled out"]

"""Local on the screen, UTC on the disk.

The split is the whole point. A boat crosses zones and lays up in another
country, so a log in local time cannot be compared with itself six months
later. But nobody stands a watch in UTC either, and "the wind gets up at 03:00"
has to mean three in the morning where the boat is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import digest as D
from agent import ui as U
from agent import units as un
from agent.anchor import AnchorFix
from agent.config import Config
from agent.derived import Confidence, Derived, VesselState
from agent.logbook import Logbook
from agent.rules import Alert, Severity
from agent.state import BoatState

# 06:00 UTC is 09:00 in Helsinki in September, and 02:00 in New York the same day.
NOON = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)


@pytest.fixture
def helsinki():
    un.set_display_timezone("Europe/Helsinki")
    yield
    un.set_display_timezone("UTC")


def derived() -> Derived:
    return Derived(
        vessel=VesselState.ANCHORED, confidence=Confidence.LIKELY, reason="in the circle"
    )


# ------------------------------------------------------------------ helpers --


def test_a_time_never_appears_without_its_zone(helsinki) -> None:
    """A bare 03:00 on a boat that has just crossed a time zone is a trap."""
    assert un.hhmm(NOON) == "09:00 EEST"
    assert un.stamp(NOON) == "2026-09-08 09:00 EEST"


def test_it_reads_an_iso_string_too_because_the_log_is_json(helsinki) -> None:
    assert un.hhmm("2026-09-08T06:00:00+00:00") == "09:00 EEST"
    assert un.hhmm("not a time") == ""
    assert un.hhmm(None) == ""


def test_a_naive_timestamp_is_treated_as_utc(helsinki) -> None:
    """Everything this agent stamps is UTC, so a missing offset means UTC."""
    assert un.hhmm(datetime(2026, 9, 8, 6, 0)) == "09:00 EEST"


def test_a_zone_the_machine_does_not_know_falls_back_rather_than_raising() -> None:
    """A typo in .env must not stop a boat being monitored."""
    assert un.set_display_timezone("Mars/Olympus") == ""
    assert un.hhmm(NOON)  # still renders something
    un.set_display_timezone("UTC")


def test_the_zone_can_be_asked_for_by_name(helsinki) -> None:
    assert un.zone_name(NOON) == "EEST"


# ------------------------------------------------------------------ on show --


def test_the_console_payload_shows_local(helsinki, tmp_path: Path) -> None:
    config = Config(
        anchor_file=tmp_path / "a.json",
        hush_file=tmp_path / "h.json",
        silence_file=tmp_path / "s.json",
    )
    state = BoatState()
    alert = Alert(rule_id="anchor_drag", severity=Severity.ALARM, message="Dragging", since=NOON)
    fix = AnchorFix(latitude=36.83, longitude=10.30, radius_m=40, set_at=NOON)

    payload = U.build_payload(state, derived(), [alert], fix, None, None, config, now=NOON)
    assert payload["alerts"][0]["since"] == "09:00 EEST"
    assert payload["anchor"]["set_at"] == "09:00 EEST"


def test_the_ships_log_entry_is_headed_in_local_time(helsinki) -> None:
    facts = D.summarise_day([], NOON - timedelta(hours=24), NOON)
    assert "2026-09-08 09:00 EEST" in D.render_entry(facts)


def test_the_alarms_in_the_entry_are_local_too(helsinki) -> None:
    records = [
        {
            "type": "alert",
            "ts": (NOON - timedelta(hours=2)).isoformat(),
            "event": "alert_raised",
            "rule": "wind_forecast",
            "severity": "alert",
            "message": "Wind building",
        }
    ]
    facts = D.summarise_day(records, NOON - timedelta(hours=24), NOON)
    assert "07:00 EEST" in D.render_entry(facts)  # 04:00 UTC


# ------------------------------------------------------------------ on disk --


def test_what_is_written_down_stays_utc(helsinki, tmp_path: Path) -> None:
    """The whole reason for the split. Zone on the screen, never in the file."""
    with Logbook(tmp_path) as logbook:
        logbook.event("agent_started")

    line = json.loads(sorted(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert line["ts"].endswith("+00:00")

    # And the anchor file, which outlives a restart and is read back by a rule.
    written = AnchorFix(latitude=36.83, longitude=10.30, radius_m=40, set_at=NOON).as_dict()
    assert written["set_at"] == "2026-09-08T06:00:00+00:00"

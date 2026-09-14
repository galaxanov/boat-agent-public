from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.logbook import Logbook
from agent.state import BoatState

from .conftest import feed


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def only_file(log_dir: Path) -> Path:
    files = sorted(log_dir.glob("*.jsonl"))
    assert len(files) == 1, files
    return files[0]


def test_writes_one_json_object_per_line(tmp_path: Path) -> None:
    with Logbook(tmp_path) as book:
        book.event("agent_started", host="localhost")
        book.event("signalk_connected", host="localhost")

    records = read_lines(only_file(tmp_path))
    assert [r["event"] for r in records] == ["agent_started", "signalk_connected"]
    assert all(r["type"] == "event" for r in records)
    assert all("ts" in r for r in records)


def test_file_is_named_for_the_utc_date(tmp_path: Path) -> None:
    with Logbook(tmp_path) as book:
        book.event("agent_started")
    expected = datetime.now(UTC).strftime("%Y-%m-%d") + ".jsonl"
    assert only_file(tmp_path).name == expected


def test_creates_the_log_directory(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist"
    with Logbook(nested) as book:
        book.event("agent_started")
    assert only_file(nested).exists()


def test_appends_rather_than_truncates(tmp_path: Path) -> None:
    with Logbook(tmp_path) as book:
        book.event("first")
    with Logbook(tmp_path) as book:
        book.event("second")
    assert [r["event"] for r in read_lines(only_file(tmp_path))] == ["first", "second"]


def test_snapshot_round_trips(tmp_path: Path, deltas: list[dict], clock) -> None:
    state = BoatState(clock=clock)
    feed(state, deltas)

    with Logbook(tmp_path) as book:
        book.snapshot(state, stale_after=300)

    record = read_lines(only_file(tmp_path))[0]
    assert record["type"] == "snapshot"
    assert record["values"]["environment.depth.belowTransducer"]["value"] == 10.9
    assert record["counts"]["paths"] == len(state)


def test_unwritable_directory_does_not_raise(tmp_path: Path) -> None:
    """A full or read-only disk must not take the agent down."""
    blocker = tmp_path / "logs"
    blocker.write_text("I am a file, not a directory")

    book = Logbook(blocker)
    book.event("agent_started")  # must not raise
    book.event("still_going")
    book.close()


def test_unserialisable_value_is_stringified(tmp_path: Path) -> None:
    with Logbook(tmp_path) as book:
        book.write({"type": "event", "event": "odd", "value": {1, 2, 3}})
    record = read_lines(only_file(tmp_path))[0]
    assert isinstance(record["value"], str)


def test_explicit_timestamp_is_kept(tmp_path: Path) -> None:
    with Logbook(tmp_path) as book:
        book.write({"type": "event", "event": "backdated", "ts": "2026-01-01T00:00:00+00:00"})
    assert read_lines(only_file(tmp_path))[0]["ts"] == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------- rotation --


def make_days(log_dir: Path, days_ago: list[int], now: datetime, suffix: str = ".jsonl") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    for days in days_ago:
        when = now - timedelta(days=days)
        path = log_dir / f"{when.strftime('%Y-%m-%d')}{suffix}"
        path.write_text('{"type": "event", "event": "old"}\n', encoding="utf-8")


def names(log_dir: Path) -> list[str]:
    return sorted(p.name for p in log_dir.iterdir())


def test_finished_days_are_compressed(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    make_days(tmp_path, [1, 2, 3], now)
    (tmp_path / "2026-09-05.jsonl").write_text("{}\n", encoding="utf-8")

    Logbook(tmp_path).rotate(now)

    assert names(tmp_path) == [
        "2026-09-02.jsonl.gz",
        "2026-09-03.jsonl.gz",
        "2026-09-04.jsonl.gz",
        "2026-09-05.jsonl",  # today is still being written to
    ]


def test_a_compressed_day_is_still_readable(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    make_days(tmp_path, [1], now)

    Logbook(tmp_path).rotate(now)

    with gzip.open(tmp_path / "2026-09-04.jsonl.gz", "rt", encoding="utf-8") as packed:
        assert json.loads(packed.read())["event"] == "old"


def test_days_past_the_retention_window_are_deleted(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    make_days(tmp_path, [5, 40], now, suffix=".jsonl.gz")
    make_days(tmp_path, [1], now)

    Logbook(tmp_path, retention_days=30).rotate(now)

    assert names(tmp_path) == ["2026-08-31.jsonl.gz", "2026-09-04.jsonl.gz"]


def test_retention_of_zero_keeps_everything(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    make_days(tmp_path, [400], now, suffix=".jsonl.gz")

    Logbook(tmp_path, retention_days=0).rotate(now)

    assert names(tmp_path) == ["2025-08-01.jsonl.gz"]


def test_rotation_ignores_files_that_are_not_daily_logs(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    (tmp_path / "outbox.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")

    Logbook(tmp_path, retention_days=1).rotate(now)

    assert names(tmp_path) == ["notes.txt", "outbox.jsonl"]


def test_rotation_runs_when_the_day_rolls_over(tmp_path: Path) -> None:
    """The old file is closed and packed away when the new day opens."""
    today = datetime(2026, 9, 5, 0, 1, tzinfo=UTC)
    make_days(tmp_path, [1], today)
    book = Logbook(tmp_path, retention_days=30)

    book._handle_for(today)  # what the first write after midnight does
    book.close()

    assert names(tmp_path) == ["2026-09-04.jsonl.gz", "2026-09-05.jsonl"]


def test_rotation_never_raises_on_a_bad_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory")
    Logbook(blocker, retention_days=1).rotate()  # must not raise

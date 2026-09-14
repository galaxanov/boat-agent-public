from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def deltas() -> list[dict]:
    """Recorded Signal K frames, including the malformed ones."""
    lines = (FIXTURES / "deltas.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def hello(deltas: list[dict]) -> dict:
    return deltas[0]


def feed(state, messages: list[dict]) -> None:
    """Push recorded frames through the state model the way the client does.

    The hello frame matters: until it arrives the state does not know which
    vessel is self, and deltas for named vessels are dropped.
    """
    for message in messages:
        if "updates" in message:
            state.apply_delta(message)
        else:
            state.apply_hello(message)


class FakeClock:
    """Manually advanced clock, so staleness tests do not sleep."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 22, 9, 15, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _fixed_display_timezone():
    """Show times in UTC for the whole suite, whatever the machine is set to.

    Display is local now, which is right for a person on a boat and wrong for
    an assertion: the same test would pass in Lisbon and fail in Helsinki. Pinned
    here rather than in each test, so nothing can forget.
    """
    from agent.units import set_display_timezone

    set_display_timezone("UTC")
    yield
    set_display_timezone("")


def push(state, values: dict, source: str = "test") -> None:
    """Apply one delta carrying the given path/value pairs."""
    state.apply_delta(
        {
            "updates": [
                {
                    "$source": source,
                    "values": [{"path": path, "value": value} for path, value in values.items()],
                }
            ]
        }
    )

"""Rolling state model: the latest value seen for each Signal K path.

Deliberately dumb for now. It stores what arrived and how long ago, and that is
all - transition detection (anchored/underway, engine on, charging) and the
rules that read them come later. Everything stays in SI as received.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse a Signal K ISO 8601 timestamp, tolerating what the bus produces."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Sample:
    """One observed value.

    `timestamp` is what the source claims; `received` is when the agent saw it.
    They differ, sometimes wildly - an instrument with no GPS fix backdates, and
    the Pi's own clock is wrong until Starlink or GPS sets it. Staleness is
    measured against `received`, which is the only one the agent can trust.
    """

    path: str
    value: Any
    source: str
    received: datetime
    timestamp: datetime | None = None

    def age(self, now: datetime | None = None) -> float:
        return ((now or _utcnow()) - self.received).total_seconds()


class BoatState:
    """Latest sample per path, plus counters for what has been seen."""

    def __init__(self, clock: Clock = _utcnow) -> None:
        self._clock = clock
        self._values: dict[str, Sample] = {}
        self.self_context: str | None = None
        self.deltas_seen = 0
        self.values_seen = 0
        self.path_counts: Counter[str] = Counter()
        self.last_delta_at: datetime | None = None
        self._warned_unknown_context = False

    # ------------------------------------------------------------- ingest --

    def apply_hello(self, message: dict) -> None:
        """Record the server's self context from the greeting frame."""
        context = message.get("self")
        if isinstance(context, str) and context:
            self.self_context = context
            log.info("Signal K self is %s (server %s)", context, message.get("version", "?"))

    def apply_delta(self, message: dict) -> list[Sample]:
        """Fold one delta message into the state, returning the samples stored.

        Anything malformed is dropped with a warning rather than raised: the
        agent has to keep running through a plugin emitting rubbish.
        """
        if not isinstance(message, dict):
            return []

        updates = message.get("updates")
        if not isinstance(updates, list):
            return []

        context = message.get("context")
        if not self._is_self(context):
            # AIS targets and other vessels. Out of scope for now.
            return []

        now = self._clock()
        self.deltas_seen += 1
        self.last_delta_at = now
        stored: list[Sample] = []

        for update in updates:
            if not isinstance(update, dict):
                continue
            source = self._source_of(update)
            timestamp = _parse_timestamp(update.get("timestamp"))

            # 'meta' updates carry units and display hints, not readings.
            for item in update.get("values") or ():
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                if not isinstance(path, str) or not path:
                    # Empty path means a vessel-level object (mmsi, name, ...).
                    continue
                if "value" not in item:
                    continue

                sample = Sample(
                    path=path,
                    value=item["value"],
                    source=source,
                    received=now,
                    timestamp=timestamp,
                )
                self._values[path] = sample
                self.path_counts[path] += 1
                self.values_seen += 1
                stored.append(sample)

        return stored

    def _is_self(self, context: Any) -> bool:
        """Is this delta about our own vessel?

        A named context that is not known to be ours is dropped, even when the
        hello frame has not been seen yet. Signal K sends the hello before any
        delta, so in practice nothing is lost - and the failure mode of the
        other choice is an AIS target's position landing in navigation.position
        and setting off the anchor alarm.
        """
        if not isinstance(context, str) or not context:
            return True  # No context at all means self.
        if context in ("vessels.self", "self"):
            return True
        if self.self_context is None:
            if not self._warned_unknown_context:
                log.warning(
                    "dropping deltas for %s: no hello frame yet, so self is unknown", context
                )
                self._warned_unknown_context = True
            return False
        return context == self.self_context

    @staticmethod
    def _source_of(update: dict) -> str:
        dollar = update.get("$source")
        if isinstance(dollar, str) and dollar:
            return dollar
        source = update.get("source")
        if isinstance(source, dict):
            label = source.get("label")
            if isinstance(label, str) and label:
                return label
        return "unknown"

    # -------------------------------------------------------------- query --

    def __contains__(self, path: str) -> bool:
        return path in self._values

    def __len__(self) -> int:
        return len(self._values)

    def get(self, path: str) -> Sample | None:
        return self._values.get(path)

    def value(self, path: str, default: Any = None) -> Any:
        sample = self._values.get(path)
        return default if sample is None else sample.value

    def now(self) -> datetime:
        """The state model's idea of now, so a rule that has to measure elapsed
        time uses the same clock the samples were stamped with - and the same
        one the tests can wind forward."""
        return self._clock()

    def age(self, path: str) -> float | None:
        sample = self._values.get(path)
        return None if sample is None else sample.age(self._clock())

    def is_stale(self, path: str, max_age: float) -> bool:
        """True if the path is missing entirely or has not reported recently."""
        age = self.age(path)
        return age is None or age > max_age

    def fresh_paths(self, max_age: float) -> list[str]:
        now = self._clock()
        return sorted(p for p, s in self._values.items() if s.age(now) <= max_age)

    def paths(self) -> list[str]:
        return sorted(self._values)

    # ------------------------------------------------------------ reporting --

    def snapshot(self, stale_after: float | None = None) -> dict[str, Any]:
        """A JSON-serialisable view of the state, for the daily log."""
        now = self._clock()
        values: dict[str, Any] = {}
        for path, sample in sorted(self._values.items()):
            age = round(sample.age(now), 1)
            entry: dict[str, Any] = {"value": sample.value, "age_s": age, "src": sample.source}
            if stale_after is not None and age > stale_after:
                entry["stale"] = True
            values[path] = entry

        return {
            "values": values,
            "counts": {
                "paths": len(self._values),
                "deltas": self.deltas_seen,
                "values": self.values_seen,
            },
        }

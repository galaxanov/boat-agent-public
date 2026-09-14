"""Turning every alarm off on purpose, and being unable to forget you did.

sound.py already has a hush, and a hush is deliberately narrow: it quiets the
speaker in the saloon for half an hour and touches nothing else. That is the
right answer to "it is going off, I am already dealing with it, stop shouting".

It is the wrong answer to the other thing that happens on a boat. The agent is
running, the instruments are live, and nothing about the situation deserves an
alarm: she is on the hard at the yard, or alongside with the shore lead in, or the
depth rule is firing every ten minutes because the transducer offset is still
unverified and nothing is going to be done about that tonight. What is wanted
then is not a quieter speaker. It is every channel off - speaker, Signal and
screen - until somebody says otherwise.

So this is a second, wider switch, and it is not simply a longer hush. Three
differences, and each one is a decision:

**It stops all three channels together.** A silence that left Signal running
would put twelve alarms on a phone overnight, and a crew that mutes the crew
group in August is a crew that misses the real one in October. Off means off.

**It does not expire.** The hush expires because it is granted in the middle of
an event, and the alarm somebody switched off at 0300 and forgot is the one
that was going to save them. A silence is granted for a situation - hauled out,
alongside, laid up for the winter - that outlasts any timer worth setting. One
that came back on its own at dawn would just be re-applied every morning until
somebody set AGENT_ALARM_SOUND=0 in .env and lost the alarm for the season.
An honest switch that stays where it is put is safer than a timer that teaches
people to disable the real thing.

**So it nags.** That is the price of not expiring, and it is paid in full: a
line in the journal twice an hour, a banner across the top of the console, a
banner on the page, and a line in the next morning's ship's log entry saying
how long the boat went unwatched. An indefinite switch has to be impossible to
leave on by accident.

What a silence does NOT stop: watching, deciding, or writing anything down. The
rules run, alerts are raised and cleared exactly as they would be, every line
still reaches the daily log, and the console and the page still show what is
standing. Only the three ways the boat has of reaching a person are held. And
when the silence is lifted, anything still standing is announced again on the
spot - turning the alarms back on must not leave a drag alarm sitting unread
because it happened to be raised while nobody was listening.

Same file mechanism as the anchor and the hush, for the same reasons: it
survives a restart, it can be set over SSH with nothing else running, and it is
one line a person can read and argue with.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .units import hhmm, span

log = logging.getLogger(__name__)

# How often a running agent repeats itself in the journal while silenced. Twice
# an hour is often enough that nobody can read `journalctl -u boat-agent`
# without meeting it, and rare enough that it does not bury anything.
NAG_INTERVAL_S = 1800.0


@dataclass(frozen=True)
class Silence:
    """Every alert channel off, since a moment, for a stated reason."""

    since: datetime
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"since": self.since.isoformat(timespec="seconds"), "note": self.note}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Silence | None:
        try:
            since = datetime.fromisoformat(str(raw["since"]))
        except (KeyError, TypeError, ValueError):
            return None
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        return cls(since=since, note=str(raw.get("note", "")))

    def held_for(self, now: datetime | None = None) -> str:
        """How long it has been off, the way a person says it."""
        return span(((now or datetime.now(UTC)) - self.since).total_seconds())

    def describe(self, now: datetime | None = None) -> str:
        """The banner. Says what is off, and how long it has been off for.

        Written to be read at a glance and to be unmistakable: this is the one
        line standing between the crew and believing the boat is being watched.
        """
        line = (
            f"SILENCED since {hhmm(self.since)}, {self.held_for(now)} ago "
            "- no alarm will reach you"
        )
        return f"{line} ({self.note})" if self.note else line


class SilenceFile:
    """The file, read and written safely. Nothing here raises.

    A third copy of the pattern in anchor.py and sound.py, kept rather than
    factored out because all three are read on the alert path and each one is
    short enough to check by eye. The shared shape is the point: anything that
    writes the file changes the state, and there is one way to do each.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._silence: Silence | None = None
        self._loaded = False

    def _changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime if self.path.is_file() else None
        except OSError:
            mtime = None
        return not self._loaded or mtime != self._mtime

    def read(self) -> Silence | None:
        self._loaded = True
        try:
            if not self.path.is_file():
                self._mtime, self._silence = None, None
                return None
            self._mtime = self.path.stat().st_mtime
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Unreadable is treated as not silenced, which is the loud answer.
            # Every other guess here ends with a boat that believes it is being
            # watched by an agent that has quietly decided not to say anything.
            log.warning(
                "cannot read the silence file at %s (%s), so alerts stay ON", self.path, exc
            )
            self._silence = None
            return None

        self._silence = Silence.from_dict(raw) if isinstance(raw, dict) and raw else None
        return self._silence

    def active(self) -> Silence | None:
        """The silence in force, if there is one. Cheap enough to poll."""
        if self._changed():
            self.read()
        return self._silence

    def write(self, silence: Silence | None) -> bool:
        """Silence every channel, or give them all back with None."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(silence.as_dict() if silence else {}, indent=1) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("cannot write the silence file at %s: %s", self.path, exc)
            return False

        self._loaded = False  # force the next read, including our own
        return True


@dataclass
class Nag:
    """Says it again, now and then, for as long as it is still true.

    Deliberately not a counter of ticks: the rule loop runs every five seconds
    and the interval here is half an hour, so the two must not be coupled.
    """

    interval_s: float = NAG_INTERVAL_S
    _last: datetime | None = field(default=None, init=False, repr=False)

    def due(self, now: datetime | None = None) -> bool:
        """Is it time to say it again? Says it at once the first time."""
        now = now or datetime.now(UTC)
        if self._last is not None and (now - self._last).total_seconds() < self.interval_s:
            return False
        self._last = now
        return True

    def reset(self) -> None:
        """The silence is over: the next one starts talking immediately."""
        self._last = None


def silence_now(note: str = "", now: datetime | None = None) -> Silence:
    return Silence(since=now or datetime.now(UTC), note=note.strip())

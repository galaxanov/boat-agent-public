"""Daily log, one JSON object per line.

logs/YYYY-MM-DD.jsonl, rotated on the UTC date. UTC rather than local time
because the boat changes timezone and a log that jumps an hour is worse than one
that needs converting.

Finished days are gzipped and eventually deleted, because the Pi has one disk
and nobody empties it. A day of snapshots is mostly repeated keys, so gzip
takes it down by roughly a factor of ten and the history stays greppable with
zcat. Housekeeping runs when a new day is opened, never on the write path.

Writes never raise. A full disk or a read-only filesystem must not take the
agent down - it drops the line, says so once, and carries on. The same goes
for rotation: a log that cannot be tidied is not a reason to stop logging.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

log = logging.getLogger(__name__)

# Keep three months aboard. Long enough to answer "when did the bilge start
# cycling more often", short enough that it never threatens the card.
RETENTION_DAYS = 90


class Logbook:
    def __init__(self, log_dir: Path, retention_days: int = RETENTION_DAYS) -> None:
        self.log_dir = Path(log_dir)
        # 0 or less keeps everything, for anyone who would rather buy a bigger
        # card than lose the record.
        self.retention_days = retention_days
        self._handle: TextIO | None = None
        self._date: str | None = None
        self._failing = False

    # ----------------------------------------------------------- plumbing --

    def _path_for(self, when: datetime) -> Path:
        return self.log_dir / f"{when.strftime('%Y-%m-%d')}.jsonl"

    def _handle_for(self, when: datetime) -> TextIO | None:
        date = when.strftime("%Y-%m-%d")
        if self._handle is not None and self._date == date:
            return self._handle

        self.close()
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # Tidy up before opening the new day, so the housekeeping happens
            # once a day rather than on any write.
            self.rotate(when)
            # buffering=1 is line buffered: a power cut loses at most the line
            # being written, not the last few minutes.
            self._handle = self._path_for(when).open("a", encoding="utf-8", buffering=1)
            self._date = date
        except OSError as exc:
            if not self._failing:
                log.error("cannot open logbook in %s: %s - dropping log lines", self.log_dir, exc)
                self._failing = True
            return None

        if self._failing:
            log.info("logbook writable again: %s", self._path_for(when))
            self._failing = False
        return self._handle

    # --------------------------------------------------------- rotation --

    @staticmethod
    def _date_of(path: Path) -> str | None:
        """The YYYY-MM-DD a log file is for, or None if it is not one of ours."""
        stem = path.name.split(".", 1)[0]
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            return None
        return stem

    def _compress(self, path: Path) -> None:
        target = path.with_suffix(path.suffix + ".gz")
        try:
            # Write the .gz first and only then drop the original, so an
            # interrupted compress loses nothing.
            with path.open("rb") as raw, gzip.open(target, "wb") as packed:
                shutil.copyfileobj(raw, packed)
            path.unlink()
        except OSError as exc:
            log.warning("could not compress %s: %s", path.name, exc)
            with contextlib.suppress(OSError):
                target.unlink()

    def rotate(self, now: datetime | None = None) -> None:
        """Gzip finished days, then delete anything past the retention window.

        Today's file is never touched: it is the one still being appended to.
        """
        now = now or datetime.now(UTC)
        today = now.strftime("%Y-%m-%d")
        cutoff = (
            (now - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
            if self.retention_days > 0
            else None
        )

        try:
            files = sorted(self.log_dir.glob("*.jsonl*"))
        except OSError as exc:
            log.warning("cannot list %s to rotate it: %s", self.log_dir, exc)
            return

        compressed = removed = 0
        for path in files:
            date = self._date_of(path)
            if date is None or date == today:
                continue
            if cutoff is not None and date < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    log.warning("could not delete %s: %s", path.name, exc)
                continue
            if path.suffix == ".jsonl":
                self._compress(path)
                compressed += 1

        if compressed or removed:
            log.info("logbook rotation: compressed %d, deleted %d", compressed, removed)

    # ----------------------------------------------------------- closing --

    def close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
        self._handle = None
        self._date = None

    def __enter__(self) -> Logbook:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- write --

    def write(self, record: dict[str, Any]) -> None:
        when = datetime.now(UTC)
        handle = self._handle_for(when)
        if handle is None:
            return

        line = dict(record)
        line.setdefault("ts", when.isoformat(timespec="seconds"))
        try:
            # default=str so an unexpected type logs as a string instead of
            # blowing up the write.
            handle.write(json.dumps(line, default=str, sort_keys=False) + "\n")
        except (OSError, ValueError) as exc:
            if not self._failing:
                log.error("logbook write failed: %s", exc)
                self._failing = True

    def event(self, event: str, **fields: Any) -> None:
        """A discrete thing that happened: connected, disconnected, started."""
        self.write({"type": "event", "event": event, **fields})

    def snapshot(self, state: Any, stale_after: float | None = None) -> None:
        """The periodic state dump."""
        self.write({"type": "snapshot", **state.snapshot(stale_after=stale_after)})

"""Telling the agent the hook is down.

The drag alarm needs to know where the anchor is. On a boat with the Signal K
anchor plugin running, the bus says so. This is for the boat that has not got
that yet, which is this one: a small file the crew writes and the running agent
reads.

A file rather than a socket or an endpoint, for three reasons. It survives an
agent restart, which matters more here than anywhere else in the codebase - an
anchor watch that quietly forgets itself when the Pi reboots at 0300 is worse
than no anchor watch, because you believe you have one. It can be set from a
laptop over SSH with no network service running. And it can be read, and
argued with, by a person: it is four numbers in a text file.

The file is the whole interface. Anything that writes it arms the watch, and
the agent feeds what it finds straight into the state model as if the plugin
had published it, so the drag rule, the derived state, the snapshots and the
ship's log all see one anchor and not two.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# What to use when nobody says. Short enough to be useful in a crowded cove,
# and the crew is told the number every time so it can be argued with.
DEFAULT_RADIUS_M = 35.0


@dataclass(frozen=True)
class AnchorFix:
    """Where the anchor is, and how far the boat may go from it."""

    latitude: float
    longitude: float
    radius_m: float
    set_at: datetime
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "latitude": round(self.latitude, 7),
            "longitude": round(self.longitude, 7),
            "radius_m": round(self.radius_m, 1),
            "set_at": self.set_at.isoformat(timespec="seconds"),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AnchorFix | None:
        try:
            latitude = float(raw["latitude"])
            longitude = float(raw["longitude"])
            radius = float(raw.get("radius_m", DEFAULT_RADIUS_M))
        except (KeyError, TypeError, ValueError):
            return None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or radius <= 0:
            log.warning("anchor file has an impossible position or radius, ignoring it")
            return None

        try:
            set_at = datetime.fromisoformat(str(raw.get("set_at")))
        except ValueError:
            set_at = datetime.now(UTC)
        if set_at.tzinfo is None:
            set_at = set_at.replace(tzinfo=UTC)

        return cls(
            latitude=latitude,
            longitude=longitude,
            radius_m=radius,
            set_at=set_at,
            note=str(raw.get("note", "")),
        )

    def as_delta(self, source: str = "anchor.file") -> dict[str, Any]:
        """The same fact in the shape the bus would have sent it.

        Feeding this into the state model means nothing downstream has to know
        whether a human or a plugin set the anchor.
        """
        return {
            "updates": [
                {
                    "$source": source,
                    "values": [
                        {
                            "path": "navigation.anchor.position",
                            "value": {"latitude": self.latitude, "longitude": self.longitude},
                        },
                        {"path": "navigation.anchor.maxRadius", "value": self.radius_m},
                    ],
                }
            ]
        }


class AnchorFile:
    """The file, read and written safely. Nothing here raises."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._fix: AnchorFix | None = None
        self._loaded = False

    @property
    def fix(self) -> AnchorFix | None:
        return self._fix

    def changed(self) -> bool:
        """Has the file been written since the last read? Cheap enough to poll."""
        try:
            mtime = self.path.stat().st_mtime if self.path.is_file() else None
        except OSError:
            mtime = None
        return not self._loaded or mtime != self._mtime

    def read(self) -> AnchorFix | None:
        """Re-read the file. An absent or empty file means no anchor set."""
        self._loaded = True
        try:
            if not self.path.is_file():
                self._mtime, self._fix = None, None
                return None
            self._mtime = self.path.stat().st_mtime
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cannot read the anchor file at %s: %s", self.path, exc)
            self._fix = None
            return None

        self._fix = AnchorFix.from_dict(raw) if isinstance(raw, dict) and raw else None
        return self._fix

    def write(self, fix: AnchorFix | None) -> bool:
        """Arm the watch, or clear it with None. Written beside and renamed over."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(fix.as_dict() if fix else {}, indent=1) + "\n", encoding="utf-8"
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("cannot write the anchor file at %s: %s", self.path, exc)
            return False

        self._loaded = False  # force the next read, including our own
        return True

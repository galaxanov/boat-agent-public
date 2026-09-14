"""Alert delivery over Signal.

The important part of this module is not sending a message - that is one
subprocess call. It is what happens when sending fails, which on a boat is
routine: Starlink drops, the dish reboots, the anchorage has a hill in the way.

So every message goes into a small persistent outbox first. If the send fails
it stays there and is retried, and it survives an agent restart. An alarm
raised during an outage still arrives when the link comes back, marked with how
late it is - a drag alarm from twenty minutes ago is very different news from
one raised now, and it must not arrive looking current.

Nothing here ever raises into the agent loop. Losing the notification channel
must not stop monitoring: the rules keep running and the logbook keeps its
record whatever Signal is doing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .rules import SEVERITY_ORDER, AlertEvent, Severity
from .units import hhmm

log = logging.getLogger(__name__)

# Signal-cli starts a JVM per call, so a send is slow rather than instant.
SEND_TIMEOUT = 120.0

# Give up on a message this old. A three-hour-old "shoaling" warning is noise;
# by the time it lands the boat is somewhere else entirely.
MAX_MESSAGE_AGE = 6 * 3600.0
MAX_OUTBOX = 200

SEVERITY_PREFIX = {
    Severity.NORMAL: "",
    Severity.ALERT: "note",
    Severity.WARN: "WARNING",
    Severity.ALARM: "ALARM",
    Severity.EMERGENCY: "EMERGENCY",
}


class NotifyError(Exception):
    """A send failed. Expected, and always recoverable by retrying."""


class Notifier(Protocol):
    name: str

    async def send(self, text: str) -> None:
        """Deliver text, or raise NotifyError."""
        ...


# ------------------------------------------------------------------ senders --


@dataclass
class LoggingNotifier:
    """Fallback when Signal is not configured. Never fails."""

    name: str = "log"

    async def send(self, text: str) -> None:
        log.info("[notification, not configured to send anywhere]\n%s", text)


def parse_recipients(raw: str | Iterable[str]) -> tuple[str, ...]:
    """Split "+301, +302" (or a list) into numbers, in order, without repeats."""
    if isinstance(raw, str):
        parts: Iterable[str] = raw.replace(",", " ").split()
    else:
        parts = raw
    seen: list[str] = []
    for part in parts:
        number = part.strip()
        if number and number not in seen:
            seen.append(number)
    return tuple(seen)


@dataclass
class SignalCliNotifier:
    """Sends through signal-cli as a linked secondary device.

    See deploy/install-signal-cli.sh - on aarch64 this needs a patched
    libsignal, and every command fails with UnsatisfiedLinkError without it.

    Several recipients get one signal-cli call each rather than one call with
    every number on it. That costs a JVM start per phone, but it keeps the
    failures apart: one number that is wrong or unregistered then cannot stop
    the others being told the boat is dragging.
    """

    account: str
    recipients: tuple[str, ...] = ()
    group_id: str = ""
    cli_path: str = "signal-cli"
    timeout: float = SEND_TIMEOUT
    name: str = "signal"

    def __post_init__(self) -> None:
        # Accept a bare string so a single number still reads naturally.
        self.recipients = parse_recipients(self.recipients)
        if not self.recipients and not self.group_id:
            raise ValueError("SignalCliNotifier needs a recipient or a group_id")

    def _command(self, text: str, recipient: str = "") -> list[str]:
        # The account is a global option and must come before the subcommand.
        # Note -a means --account globally but --attachment after `send`, so
        # the ordering here is load-bearing.
        command = [self.cli_path, "-a", self.account, "send", "-m", text]
        if self.group_id:
            command += ["-g", self.group_id]
        else:
            command.append(recipient or self.recipients[0])
        return command

    async def send(self, text: str) -> None:
        if self.group_id:
            await self._run(self._command(text))
            return

        failures: list[tuple[str, NotifyError]] = []
        for recipient in self.recipients:
            try:
                await self._run(self._command(text, recipient))
            except NotifyError as exc:
                failures.append((recipient, exc))

        if not failures:
            return
        failed = [number for number, _ in failures]
        if len(failures) == len(self.recipients):
            # Everything failed, which usually means no link. Raising puts the
            # message back in the outbox to be retried.
            raise failures[0][1]
        # Someone got it. Retrying would send them the alarm a second time, so
        # take the partial delivery and say loudly who missed out.
        log.error("alert delivered, but not to %s - check the number", ", ".join(failed))

    async def _run(self, command: list[str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise NotifyError(f"cannot run {self.cli_path}: {exc}") from exc

        try:
            _out, err = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise NotifyError(f"signal-cli timed out after {self.timeout:.0f}s") from None

        if process.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            raise NotifyError(
                f"signal-cli exited {process.returncode}: {detail[-1] if detail else 'no output'}"
            )


# ------------------------------------------------------------------ outbox --


@dataclass
class Pending:
    text: str
    created: datetime
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "created": self.created.isoformat(), "attempts": self.attempts}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Pending | None:
        try:
            created = datetime.fromisoformat(str(raw["created"]))
        except (KeyError, ValueError):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        text = raw.get("text")
        if not isinstance(text, str) or not text:
            return None
        return cls(text=text, created=created, attempts=int(raw.get("attempts", 0)))


class Outbox:
    """Messages waiting to go out, persisted so a restart does not lose them."""

    def __init__(
        self,
        path: Path,
        max_age: float = MAX_MESSAGE_AGE,
        max_items: int = MAX_OUTBOX,
    ) -> None:
        self.path = Path(path)
        self.max_age = max_age
        self.max_items = max_items
        self._items: list[Pending] = []
        self._load()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[Pending]:
        return list(self._items)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read the outbox at %s (%s), starting empty", self.path, exc)
            return
        if not isinstance(raw, list):
            return
        for entry in raw:
            if isinstance(entry, dict):
                item = Pending.from_dict(entry)
                if item is not None:
                    self._items.append(item)
        if self._items:
            log.info("outbox has %d message(s) waiting from a previous run", len(self._items))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a power cut cannot leave a half-written file.
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps([item.as_dict() for item in self._items], indent=0),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("cannot persist the outbox: %s", exc)

    def add(self, text: str, now: datetime) -> None:
        self._items.append(Pending(text=text, created=now))
        if len(self._items) > self.max_items:
            dropped = len(self._items) - self.max_items
            self._items = self._items[-self.max_items :]
            log.warning("outbox full, dropped %d of the oldest message(s)", dropped)
        self._save()

    def expire(self, now: datetime) -> int:
        keep = [i for i in self._items if (now - i.created).total_seconds() <= self.max_age]
        dropped = len(self._items) - len(keep)
        if dropped:
            log.warning("dropped %d message(s) too old to still be worth sending", dropped)
            self._items = keep
            self._save()
        return dropped

    def remove(self, item: Pending) -> None:
        if item in self._items:
            self._items.remove(item)
            self._save()

    def record_failure(self, item: Pending) -> None:
        item.attempts += 1
        self._save()


# ---------------------------------------------------------------- service --


def format_alert(event: AlertEvent, boat: str = "Seabird", now: datetime | None = None) -> str:
    """A message that reads sensibly on a phone at 3am."""
    alert = event.alert
    now = now or datetime.now(UTC)

    if event.kind == "cleared":
        headline = f"Cleared - {boat}"
    else:
        prefix = SEVERITY_PREFIX.get(alert.severity, str(alert.severity).upper())
        verb = "worsened" if event.kind == "escalated" else ""
        headline = f"{prefix} - {boat}{f' ({verb})' if verb else ''}"

    lines = [headline, alert.message]

    if alert.data:
        details = ", ".join(f"{k}={v}" for k, v in sorted(alert.data.items()))
        lines.append(details)

    lines.append(f"since {hhmm(alert.since)}")
    return "\n".join(lines)


def stamp_delay(text: str, created: datetime, now: datetime) -> str:
    """Mark a message that has been sitting in the outbox.

    Without this a drag alarm from during a Starlink outage arrives looking
    like it just happened.
    """
    delay = (now - created).total_seconds()
    if delay < 120:
        return text
    return f"{text}\n(delayed {delay / 60:.0f} min - no link when this was raised)"


@dataclass
class AlertNotifier:
    """Formats alert events, filters them, and gets them out reliably."""

    notifier: Notifier
    outbox: Outbox
    min_severity: Severity = Severity.ALERT
    boat: str = "Seabird"
    clock: Any = field(default=lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return self.clock()

    def wants(self, event: AlertEvent) -> bool:
        # A clear is always worth sending if the raise was: otherwise you are
        # left believing the boat is still dragging.
        threshold = SEVERITY_ORDER[self.min_severity]
        return SEVERITY_ORDER[event.alert.severity] >= threshold

    async def handle(self, event: AlertEvent) -> None:
        if not self.wants(event):
            return
        now = self._now()
        self.outbox.add(format_alert(event, boat=self.boat, now=now), now)
        await self.flush()

    async def flush(self) -> int:
        """Try to send everything waiting. Returns how many got through."""
        now = self._now()
        self.outbox.expire(now)

        sent = 0
        for item in self.outbox.items:
            text = stamp_delay(item.text, item.created, now)
            try:
                await self.notifier.send(text)
            except NotifyError as exc:
                self.outbox.record_failure(item)
                # Debug, not error: no link is the normal state at sea, and an
                # ERROR line every retry would bury the real problems.
                log.debug("send failed (attempt %d): %s", item.attempts, exc)
                break  # the link is down; no point trying the rest right now
            except Exception:
                self.outbox.record_failure(item)
                log.exception("unexpected failure sending a notification")
                break
            else:
                self.outbox.remove(item)
                sent += 1

        if sent:
            log.info("sent %d notification(s) over %s", sent, self.notifier.name)
        return sent


def build_notifier(config: Any) -> Notifier:
    """Pick a sender from config. Never raises - falls back to logging."""
    if not config.signal_account:
        log.warning(
            "SIGNAL_ACCOUNT is not set: alerts will be logged but not sent. "
            "See deploy/install-signal-cli.sh"
        )
        return LoggingNotifier()

    try:
        return SignalCliNotifier(
            account=config.signal_account,
            recipients=config.signal_recipients,
            group_id=config.signal_group,
            cli_path=config.signal_cli_path,
        )
    except ValueError as exc:
        log.error("Signal is misconfigured (%s), falling back to logging only", exc)
        return LoggingNotifier()

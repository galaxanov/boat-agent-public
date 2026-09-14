"""Alerts on the screen of the machine the agent is running on.

Signal reaches whoever is ashore, on deck, or asleep with a phone beside them.
It does not reach the person sitting at the chart table with the laptop open in
front of them, which on this boat is where most of the day is spent. A message
that arrives on a phone in a locker is a message nobody reads.

So every alert also goes to the desktop, through notify-send. That is a much
weaker promise than the sound in sound.py - it needs a session, a notification
daemon, and someone looking at the screen - which is exactly why it is a third
channel rather than a replacement for either of the other two. The three fail
in different ways: Signal needs the sky, the speaker needs the volume up, the
screen needs someone in front of it.

Unlike Signal there is no outbox and nothing is ever retried. A notification
that turns up twenty minutes late is worse than one that never came, because
the screen has no way of saying "this happened a while ago" and it will be read
as now. If it does not go out at the moment it is raised it is dropped, and the
logbook still has it.

Nothing here can raise into the agent loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .notify import format_alert
from .rules import SEVERITY_ORDER, AlertEvent, Severity
from .sound import audio_env

log = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0

# Critical notifications are the ones the desktop keeps on screen until they
# are dismissed, which is right for an alarm and wrong for anything less: a
# sticky note about the locker being warm is how people learn to swipe without
# reading.
SEVERITY_URGENCY = {
    Severity.NORMAL: "low",
    Severity.ALERT: "normal",
    Severity.WARN: "normal",
    Severity.ALARM: "critical",
    Severity.EMERGENCY: "critical",
}

SEVERITY_ICON = {
    Severity.NORMAL: "dialog-information",
    Severity.ALERT: "dialog-information",
    Severity.WARN: "dialog-warning",
    Severity.ALARM: "dialog-error",
    Severity.EMERGENCY: "dialog-error",
}

# How long a non-critical notification stays up, in milliseconds. Long enough
# to come back from the heads and still see it.
EXPIRE_MS = 30_000


def session_env() -> dict[str, str]:
    """The environment a notification client needs, filled in if systemd did not.

    Same problem as the sound server, and the same fix: a systemd system unit
    has no session, so it does not know where the user's message bus is. The
    socket is in a predictable place, so look there rather than going quiet.
    """
    env = audio_env()  # XDG_RUNTIME_DIR, which is the directory holding the bus
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        socket = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "bus"
        if socket.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={socket}"
    return env


@dataclass
class DesktopNotifier:
    """Puts an alert on screen, and never gets in the way of anything else."""

    boat: str = "Seabird"
    min_severity: Severity = Severity.ALERT
    enabled: bool = True
    command: str = "notify-send"
    timeout: float = SEND_TIMEOUT

    # One notification per rule, replaced in place. Otherwise a rule that
    # raises, worsens and clears leaves three notifications on the screen
    # disagreeing with each other about the state of the boat.
    _ids: dict[str, int] = field(default_factory=dict, repr=False)
    _complained: bool = field(default=False, init=False, repr=False)

    def wants(self, event: AlertEvent) -> bool:
        if not self.enabled:
            return False
        floor = SEVERITY_ORDER[self.min_severity]
        return SEVERITY_ORDER.get(event.alert.severity, 0) >= floor

    def _argv(self, event: AlertEvent, now: datetime) -> list[str]:
        alert = event.alert
        cleared = event.kind == "cleared"

        # The same words as the Signal message, split at the first line: the
        # headline is what the desktop shows in bold, the rest is the body.
        # One formatting rule for both channels means they cannot drift apart.
        summary, _, body = format_alert(event, boat=self.boat, now=now).partition("\n")

        urgency = "normal" if cleared else SEVERITY_URGENCY.get(alert.severity, "normal")
        icon = "dialog-information" if cleared else SEVERITY_ICON.get(
            alert.severity, "dialog-information"
        )

        argv = [
            self.command,
            "--print-id",
            "--app-name",
            self.boat,
            "--urgency",
            urgency,
            "--icon",
            icon,
            "--expire-time",
            "0" if urgency == "critical" else str(EXPIRE_MS),
        ]
        previous = self._ids.get(alert.rule_id)
        if previous:
            argv += ["--replace-id", str(previous)]
        return [*argv, summary, body]

    async def handle(self, event: AlertEvent, now: datetime | None = None) -> bool:
        """Show the alert. Returns whether it went out."""
        if not self.wants(event):
            return False
        now = now or datetime.now(UTC)

        try:
            sent = await self._run(self._argv(event, now), event.alert.rule_id)
        except Exception:
            log.exception("desktop notification failed, carrying on")
            return False

        if event.kind == "cleared":
            # Nothing left to replace, and rule ids should not accumulate for a
            # boat that runs for a season without a restart.
            self._ids.pop(event.alert.rule_id, None)
        return sent

    async def _run(self, argv: list[str], rule_id: str) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=session_env(),
            )
        except (OSError, ValueError) as exc:
            self._complain(f"cannot run {self.command}: {exc}")
            return False

        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            log.debug("%s did not finish within %.0fs", self.command, self.timeout)
            return False

        if process.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            last = detail[-1] if detail else "no output"
            self._complain(f"{self.command} exited {process.returncode}: {last}")
            return False

        if self._complained:
            log.info("desktop notifications are getting through again")
            self._complained = False

        # --print-id gives back the id to replace next time this rule speaks.
        try:
            self._ids[rule_id] = int(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError, UnicodeDecodeError):
            self._ids.pop(rule_id, None)
        return True

    def _complain(self, detail: str) -> None:
        """Say it once. The alert itself is the thing worth reading in the log."""
        if self._complained:
            log.debug("desktop notification failed: %s", detail)
            return
        self._complained = True
        log.warning(
            "no alert is reaching this machine's screen (%s). Alerts still go to "
            "Signal and the log, and an alarm still makes a noise.",
            detail,
        )


def build_desktop_notifier(config: Any) -> DesktopNotifier:
    """Pick a desktop notifier from config. Never raises."""
    try:
        min_severity = Severity(config.desktop_min_severity)
    except ValueError:
        log.warning(
            "AGENT_DESKTOP_MIN_SEVERITY=%r is not a severity, using alert",
            config.desktop_min_severity,
        )
        min_severity = Severity.ALERT

    return DesktopNotifier(
        boat=config.boat_name,
        min_severity=min_severity,
        enabled=config.desktop_notify,
        command=config.notify_send_path,
    )

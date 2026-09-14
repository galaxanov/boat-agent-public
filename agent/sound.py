"""A noise the boat can make on its own.

notify.py is the honest half of the alerting story, and the fragile one. Every
message it sends depends on Starlink being up, on the dish having sky, and on a
phone being somewhere it can ring. On the night it matters none of that is
certain, which is why the README has said from the start that a message is not
an alarm.

This is the other half, and it is the one thing the nav laptop does better than
the Pi ever would: it has speakers. The alarm that actually matters - the
anchor dragging at 0300 with the crew asleep six feet away - can be made in the
saloon, over a link that cannot go down because there is no link.

Three things it has to get right:

- It has to keep making the noise. One beep is lost in a swell, or arrives
  while someone is on deck. So it repeats for as long as the alert is standing,
  and stops on its own when the alert clears.
- It has to be silenceable. An alarm that cannot be stopped gets its power
  pulled, and then nothing works for the rest of the season. --hush gives a
  quiet period with an expiry on it.
- It must never break the agent. Sound is the least important thing here: the
  rules, the logbook and Signal all matter more, so every failure in this file
  is caught and logged and the loop carries on.

The sound itself is generated rather than shipped. A .wav in the repo is one
more thing to lose, and the alarm should not depend on a file someone might
tidy away.
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import math
import os
import shlex
import shutil
import sys
import wave
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .rules import SEVERITY_ORDER, Alert, AlertEvent, Severity
from .units import hhmm

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- the sound --

# 22 kHz is plenty for a siren and halves the file. Nothing here is hi-fi.
SAMPLE_RATE = 22050
HIGH_HZ = 880.0
LOW_HZ = 622.0  # deliberately not a pleasant interval with the high tone
BEEP_S = 0.30
GAP_S = 0.06
BEEPS = 8  # about three seconds of noise per sounding
AMPLITUDE = 0.7  # of full scale; the rest is headroom against clipping
EDGE_S = 0.005  # attack and release, so the tone does not click

DEFAULT_REPEAT_S = 20.0
PLAY_TIMEOUT = 20.0
DEFAULT_HUSH_MINUTES = 30.0


def _tone(frequency: float, seconds: float, into: array.array) -> None:
    total = int(SAMPLE_RATE * seconds)
    edge = max(1, int(SAMPLE_RATE * EDGE_S))
    peak = AMPLITUDE * 32767
    for i in range(total):
        # Ramp both ends. A square-edged tone clicks, and a click is what a
        # cheap toy alarm sounds like - easy to dismiss half asleep.
        ramp = min(1.0, i / edge, (total - i) / edge)
        into.append(int(peak * ramp * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE)))


def _silence(seconds: float, into: array.array) -> None:
    into.extend([0] * int(SAMPLE_RATE * seconds))


def build_alarm_wav(path: Path) -> Path | None:
    """Write the alarm sound if it is not already there. None if it cannot."""
    path = Path(path)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        pass

    samples = array.array("h")
    for n in range(BEEPS):
        _tone(HIGH_HZ if n % 2 == 0 else LOW_HZ, BEEP_S, samples)
        _silence(GAP_S, samples)
    if sys.byteorder == "big":
        samples.byteswap()  # WAV samples are little-endian whatever the machine is

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with wave.open(str(temporary), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(samples.tobytes())
        os.replace(temporary, path)
    except (OSError, wave.Error) as exc:
        log.error("cannot write the alarm sound to %s: %s", path, exc)
        return None

    log.info("wrote the alarm sound to %s", path)
    return path


# --------------------------------------------------------------- playing it --

# In order of preference. pw-play and paplay go through the sound server, which
# is what a desktop session is using, so they mix rather than fight over the
# card. aplay talks to ALSA directly and is the one that still works when
# nobody is logged in, provided the user is in the audio group.
PLAYERS: tuple[tuple[str, ...], ...] = (
    ("pw-play", "{file}"),
    ("paplay", "{file}"),
    ("aplay", "-q", "{file}"),
    ("ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{file}"),
)


def audio_env() -> dict[str, str]:
    """The environment a sound server client needs, filled in if systemd did not.

    pw-play and paplay find the server through a socket under XDG_RUNTIME_DIR.
    A systemd system unit does not set that variable, so a command that works
    perfectly from a shell is silent from the service - which is exactly the
    situation where nobody is watching to notice.
    """
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        guess = Path(f"/run/user/{os.getuid()}")
        if guess.is_dir():
            env["XDG_RUNTIME_DIR"] = str(guess)
    return env


@dataclass
class Player:
    """Whatever on this machine can be persuaded to make a noise."""

    override: str = ""  # AGENT_ALARM_PLAYER, with {file} where the path goes
    timeout: float = PLAY_TIMEOUT
    name: str = ""  # the last command that worked, for --test-alarm to report
    _preferred: tuple[str, ...] | None = field(default=None, repr=False)

    def specs(self) -> list[tuple[str, ...]]:
        """The commands worth trying here, best first."""
        if self.override:
            spec = tuple(shlex.split(self.override))
            return [spec] if spec else []

        found = [spec for spec in PLAYERS if shutil.which(spec[0])]
        # Whatever worked last time goes first: after the first success this is
        # one exec rather than a walk down a list of things that are not there.
        if self._preferred in found:
            found.remove(self._preferred)
            found.insert(0, self._preferred)
        return found

    @staticmethod
    def _command(spec: tuple[str, ...], path: Path) -> list[str]:
        if not any("{file}" in part for part in spec):
            return [*spec, str(path)]
        return [part.replace("{file}", str(path)) for part in spec]

    async def play(self, path: Path) -> str:
        """Make the noise. Returns the command that worked, or "" if none did."""
        for spec in self.specs():
            if await self._run(self._command(spec, path)):
                self._preferred = spec
                self.name = spec[0]
                return spec[0]
        return ""

    async def _run(self, command: list[str]) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=audio_env(),
            )
        except (OSError, ValueError) as exc:
            log.debug("cannot run %s: %s", command[0], exc)
            return False

        try:
            _out, err = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            log.debug("%s did not finish within %.0fs", command[0], self.timeout)
            return False

        if process.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            log.debug(
                "%s exited %d: %s",
                command[0],
                process.returncode,
                detail[-1] if detail else "no output",
            )
            return False
        return True


# ------------------------------------------------------------------- hushing --


@dataclass(frozen=True)
class Hush:
    until: datetime
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"until": self.until.isoformat(timespec="seconds"), "note": self.note}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Hush | None:
        try:
            until = datetime.fromisoformat(str(raw["until"]))
        except (KeyError, TypeError, ValueError):
            return None
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return cls(until=until, note=str(raw.get("note", "")))


class HushFile:
    """Silence, asked for by a person, with an expiry on it.

    Two things this deliberately does not do. It does not touch the Signal
    message or the logbook line: hush is about the speaker in the saloon, not
    about what the boat records or who it tells. And it does not last forever -
    the alarm somebody switched off and forgot is the one that was going to
    save them.

    Same shape as the anchor file, and for the same reasons: it survives a
    restart, it can be set over SSH, and it is one line a person can read.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._hush: Hush | None = None
        self._loaded = False

    def _changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime if self.path.is_file() else None
        except OSError:
            mtime = None
        return not self._loaded or mtime != self._mtime

    def read(self) -> Hush | None:
        self._loaded = True
        try:
            if not self.path.is_file():
                self._mtime, self._hush = None, None
                return None
            self._mtime = self.path.stat().st_mtime
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cannot read the hush file at %s: %s", self.path, exc)
            self._hush = None
            return None

        self._hush = Hush.from_dict(raw) if isinstance(raw, dict) and raw else None
        return self._hush

    def active(self, now: datetime | None = None) -> Hush | None:
        """The hush in force right now, if there is one. Cheap enough to poll."""
        now = now or datetime.now(UTC)
        if self._changed():
            self.read()
        if self._hush is None:
            return None
        return self._hush if now < self._hush.until else None

    def write(self, hush: Hush | None) -> bool:
        """Set a quiet period, or end one with None."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(hush.as_dict() if hush else {}, indent=1) + "\n", encoding="utf-8"
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            log.error("cannot write the hush file at %s: %s", self.path, exc)
            return False

        self._loaded = False  # force the next read, including our own
        return True


# -------------------------------------------------------------- the alarm --


@dataclass
class LocalAlarm:
    """Sounds while an alert of consequence is standing, and not otherwise.

    Only alarm and emergency by default. A laptop that beeps at every warning
    is a laptop somebody turns the volume down on, and then the drag alarm is
    silent too.
    """

    player: Player
    hush: HushFile | None = None
    wav_path: Path = Path("logs/alarm.wav")
    min_severity: Severity = Severity.ALARM
    repeat_s: float = DEFAULT_REPEAT_S
    enabled: bool = True

    _last: datetime | None = field(default=None, init=False, repr=False)
    _force: bool = field(default=False, init=False, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _mute_complaint: bool = field(default=False, init=False, repr=False)

    @property
    def sounding(self) -> bool:
        return self._last is not None

    def wants(self, severity: Severity) -> bool:
        if not self.enabled:
            return False
        floor = SEVERITY_ORDER[self.min_severity]
        return SEVERITY_ORDER.get(severity, 0) >= floor

    def notice(self, event: AlertEvent) -> None:
        """Something was raised or got worse: make the noise now, not in 20s."""
        if event.kind in ("raised", "escalated") and self.wants(event.alert.severity):
            self._force = True

    async def tick(self, active: Iterable[Alert], now: datetime | None = None) -> bool:
        """Sound if it is due. Returns whether a noise was started.

        Never blocks: the playing happens in a background task, so the rules
        keep being evaluated while the saloon is being woken up.
        """
        if not self.enabled:
            return False
        now = now or datetime.now(UTC)

        loud = [alert for alert in active if self.wants(alert.severity)]
        if not loud:
            if self._last is not None:
                log.info("local alarm quiet again: nothing left standing")
            self._last = None
            self._force = False
            return False

        # A hush holds the trigger rather than clearing it, so an alarm raised
        # during the quiet period sounds the moment the quiet period ends.
        if self.hush is not None:
            quiet = self.hush.active(now)
            if quiet is not None:
                log.debug("local alarm hushed until %s", quiet.until.isoformat(timespec="minutes"))
                return False

        rested = self._last is None or (now - self._last).total_seconds() >= self.repeat_s
        if not self._force and not rested:
            return False
        if self._task is not None and not self._task.done():
            return False  # still beeping from last time

        first = self._last is None
        self._last = now
        self._force = False
        if first:
            log.error("SOUNDING the local alarm - %s", loud[0].message)
        self._task = asyncio.create_task(self._sound())
        return True

    async def _sound(self) -> None:
        """Play it once. Swallows everything: sound is never worth a traceback."""
        try:
            path = build_alarm_wav(self.wav_path)
            if path is None:
                return
            worked = await self.player.play(path)
            if worked:
                if self._mute_complaint:
                    log.info("the local alarm can be heard again, via %s", worked)
                    self._mute_complaint = False
            elif not self._mute_complaint:
                # Once, not every twenty seconds: the point is to be noticed in
                # the log, not to bury the alert that caused it.
                self._mute_complaint = True
                log.error(
                    "nothing on this machine would play the alarm sound, so the boat "
                    "is relying on Signal alone. Install alsa-utils or pipewire, check "
                    "the volume is up, and test it with: python -m agent.main --test-alarm"
                )
        except Exception:
            log.exception("the local alarm failed, carrying on")

    async def drain(self) -> None:
        """Wait for the noise now playing to finish, if any."""
        task = self._task
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    async def aclose(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def build_local_alarm(config: Any) -> LocalAlarm:
    """Pick an alarm from config. Never raises - falls back to sane settings."""
    try:
        min_severity = Severity(config.alarm_min_severity)
    except ValueError:
        log.warning(
            "AGENT_ALARM_MIN_SEVERITY=%r is not a severity, sounding on alarm and above",
            config.alarm_min_severity,
        )
        min_severity = Severity.ALARM

    return LocalAlarm(
        player=Player(override=config.alarm_player),
        hush=HushFile(config.hush_file),
        wav_path=config.alarm_wav_path,
        min_severity=min_severity,
        repeat_s=config.alarm_repeat,
        enabled=config.alarm_sound,
    )


def hush_until(minutes: float, now: datetime | None = None) -> Hush:
    now = now or datetime.now(UTC)
    return Hush(
        until=now + timedelta(minutes=minutes),
        note=f"hushed for {minutes:.0f} min at {hhmm(now)}",
    )

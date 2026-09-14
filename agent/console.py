"""One command that shows the boat and does the four things you actually do.

    boat

The agent runs by itself and needs nobody. What a person needs is a way to see
what it thinks and to tell it four or five things: the hook is down, the hook is
up, be quiet for half an hour, let me hear that again, and stop telling me
anything at all until I ask. Each of those already has a flag, and remembering
five flags at 0200 on a pitching boat is a worse interface than a list of
letters on a screen.

So this is one screen and a prompt. It is not a dashboard to sit and watch -
the agent is already watching, and it will make a noise if something is wrong.
It is the thing you open when you have just let the anchor go.

Two decisions worth writing down.

**It reads a file, not the bus.** The running agent publishes what it can see to
`logs/status.json` every rule tick. This reads that and draws it, which means it
opens instantly, needs no Signal K connection of its own, cannot compete with
the agent for the one it has, and works over an SSH link too poor to hold a
websocket open. The price is that the picture can be one tick old, so the age
is always on the screen. If the agent is not running there is no file, and it
says so rather than drawing an empty boat that looks calm.

**The actions are the same actions.** Every one of them goes through ui.Actions,
which writes the same `logs/anchor.json` that `--anchor-down` writes. There is
one mechanism for arming a watch on this boat and this is a third doorway to
it, not a second implementation of it.

The one thing drawn from a file other than the status payload is the silence,
read straight from `logs/silence.json`. It has to be right on the screen that
is up when the agent is NOT running, and it has to appear the instant somebody
presses the key rather than one rule tick later - this is the single line that
decides whether the crew believes the boat can reach them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

from .anchor import DEFAULT_RADIUS_M
from .ui import Actions, read_status
from .units import dayhhmm, hhmm

# ANSI, and only when talking to a terminal. Piped to a file or a pager this
# writes plain text, which is what you want when pasting it into a message.
DIM = "\033[2m"
BOLD = "\033[1m"
AMBER = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
OFF = "\033[0m"

# Past this the picture is not current, and saying so matters more than
# drawing it. Four rule ticks at the default interval.
STALE_SECONDS = 20.0

# After a letter that writes a file, how long to wait for the agent to notice
# and republish before drawing again. Two rule ticks and a little. Without it
# the screen redraws from the payload written before the tap and says "no watch
# set" one line under "watch set, 40 m circle", which reads as a failure.
SETTLE_SECONDS = 12.0
SETTLE_POLL = 0.4

# systemctl on a healthy machine answers at once. This is only here so a wedged
# one cannot hold the screen.
SYSTEMCTL_TIMEOUT = 20.0

SEVERITY_COLOUR = {
    "alert": AMBER,
    "warn": AMBER,
    "alarm": RED,
    "emergency": RED,
}


class Screen:
    """Writes to the terminal, or plainly to anything else."""

    def __init__(self, stream: Any = None, colour: bool | None = None) -> None:
        self.out = stream or sys.stdout
        if colour is None:
            colour = hasattr(self.out, "isatty") and self.out.isatty()
        self.colour = colour

    def paint(self, text: str, *codes: str) -> str:
        return f"{''.join(codes)}{text}{OFF}" if self.colour and codes else text

    def line(self, text: str = "") -> None:
        print(text, file=self.out)


def age_of(payload: dict[str, Any], now: datetime | None = None) -> float | None:
    """Seconds since the agent published this, or None if it cannot be read."""
    raw = payload.get("generated_at")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - stamp).total_seconds()


def render(
    payload: dict[str, Any] | None,
    screen: Screen,
    now: datetime | None = None,
    silence: Any = None,
) -> None:
    """Draw the boat. Absent readings are drawn absent, never as zero."""
    # Above everything, including "the agent is not running", because it
    # changes what every line under it means. A silenced boat draws a perfectly
    # calm screen, and without this the screen would be a lie by omission.
    if silence is not None:
        screen.line()
        screen.line(screen.paint(f"  {silence.describe(now)}", RED, BOLD))
        screen.line(screen.paint("  Press z to turn them back on.", RED))

    if payload is None:
        screen.line()
        screen.line(screen.paint("  The agent is not running, or has never run.", RED))
        screen.line(screen.paint("  Press x to start it, or q and then:", DIM))
        screen.line(screen.paint("      systemctl --user start boat-agent", DIM))
        screen.line()
        return

    age = age_of(payload, now)
    shown = hhmm(payload.get("generated_at"))
    when = shown
    if age is not None and age > STALE_SECONDS:
        when = screen.paint(f"{shown}, {age / 60:.0f} min ago - the agent may be down", RED)

    screen.line()
    boat = payload.get("boat", "the boat")
    state = str(payload.get("state", "unknown"))
    screen.line(f"  {screen.paint(boat, BOLD)}  {screen.paint(state, AMBER, BOLD)}    {when}")

    reason = payload.get("reason") or ""
    confidence = payload.get("confidence") or ""
    if reason:
        screen.line(screen.paint(f"  {confidence} · {reason}", DIM))

    # Alerts first and always: this is the only thing here that is urgent.
    alerts = payload.get("alerts") or []
    if alerts:
        screen.line()
        for alert in alerts:
            colour = SEVERITY_COLOUR.get(str(alert.get("severity")), AMBER)
            head = screen.paint(f"  {str(alert.get('severity','')).upper():<9}", colour, BOLD)
            screen.line(f"{head}{alert.get('message', '')}")
            trail = f"           {alert.get('rule')}, since {alert.get('since')}"
            screen.line(screen.paint(trail, DIM))

    screen.line()
    gps = payload.get("gps") or {}
    where = gps.get("text") or payload.get("position") or "no GPS position"
    # Red when there is no fix: the anchor watch is only as good as this line.
    if not gps.get("fix"):
        where = screen.paint(where, RED)
    screen.line(f"  {screen.paint('GPS     ', DIM)}{where}")

    anchor = payload.get("anchor") or {}
    if anchor.get("set"):
        far = anchor.get("distance")
        distance = "no fix, so the distance is unknown" if far is None else f"{far} off"
        screen.line(
            f"  {screen.paint('Anchor  ', DIM)}{anchor.get('radius_m')} m circle · "
            f"{distance} · set {anchor.get('set_at')}"
        )
    else:
        screen.line(f"  {screen.paint('Anchor  ', DIM)}no watch set")

    forecast = payload.get("forecast") or {}
    summary = forecast.get("summary") or "nothing"
    hours = forecast.get("hours")
    text = f"next {hours:.0f} h: {summary}" if hours else summary
    screen.line(f"  {screen.paint('Weather ', DIM)}{text}")

    hush = payload.get("hush")
    speaker = f"hushed until {hush}" if hush else "will sound if an alarm is raised"
    if silence is not None:
        speaker = screen.paint("silenced, along with Signal and this screen", RED)
    screen.line(f"  {screen.paint('Speaker ', DIM)}{speaker}")

    readings = payload.get("readings") or []
    if readings:
        screen.line()
        cells = []
        for reading in readings:
            value = reading.get("value")
            shown = (
                screen.paint("-", DIM)
                if value is None
                else f"{value}{screen.paint(reading.get('unit', ''), DIM)}"
            )
            label = screen.paint(f"{reading.get('label', ''):<8}", DIM)
            cells.append(f"{label}{shown}")
        for row in range(0, len(cells), 3):
            screen.line("  " + "".join(f"{cell:<34}" for cell in cells[row : row + 3]).rstrip())

    screen.line()


# Two rows, not one: the nav station terminal is a laptop half-buried under a
# chart and this has to stay readable when the window is eighty columns.
# The anchor pair first, because that is what this is opened for.
MENU = (
    (("a", "anchor down"), ("u", "anchor up"), ("w", "weather")),
    (("h", "hush 30 min"), ("s", "let it sound"), ("r", "refresh"), ("q", "quit")),
)


def menu_rows(silenced: bool) -> tuple[tuple[tuple[str, str], ...], ...]:
    """The letters, with the silence key labelled for which way it will move.

    One key rather than two, and never a blind toggle: the banner above says
    which state the boat is in, and the label here says what pressing it will
    do about that. Arming it also asks for a letter of confirmation. Turning
    the alarms back on never does - that direction is always safe, and a boat
    that made you confirm it would be one you hesitated over.
    """
    switch = (
        ("z", "TURN THE ALARMS BACK ON") if silenced else ("z", "silence every alert")
    )
    return (*MENU, (switch, ("x", "restart the agent")))

# The user unit, which is the whole point: no password to restart the thing
# that watches the boat. deploy/install-command.sh puts it there.
UNIT = "boat-agent.service"


def render_menu(screen: Screen, silenced: bool = False) -> None:
    for row in menu_rows(silenced):
        keys = "   ".join(f"{screen.paint('[' + key + ']', BOLD)} {label}" for key, label in row)
        screen.line(f"  {keys}")
    screen.line()


def _wait_for_the_agent(config: Any, before: Any, sleep: Any) -> None:
    """Hold until the agent republishes, so the next screen shows what you did.

    The tap wrote a file; the agent reads it on its next rule tick and puts the
    result in the status file. Redrawing before that shows the boat as it was a
    moment ago, which after "watch set" reads as the watch having failed.

    Capped, and never an error: if the agent is not running there is nothing to
    wait for, and the screen after this says so.
    """
    # Counted, not timed. The pacing is entirely in `sleep`, so a test can
    # hand in one that does not sleep and the loop finishes at once instead of
    # spinning on a clock for twelve real seconds.
    for _ in range(max(1, int(SETTLE_SECONDS / SETTLE_POLL))):
        if (read_status(config.status_path) or {}).get("generated_at") != before:
            return
        sleep(SETTLE_POLL)


def run_console(
    config: Any, ask: Any = input, screen: Screen | None = None, sleep: Any = time.sleep
) -> int:
    """Draw, take one letter, do it, draw again. Ends on q or Ctrl-D."""
    screen = screen or Screen()
    actions = Actions(config)

    while True:
        # Read here rather than taken from the payload: it must be right on the
        # screen that is up when the agent is down, and it must change the
        # instant the key is pressed rather than one rule tick later.
        held = actions.silence.active()
        render(read_status(config.status_path), screen, silence=held)
        render_menu(screen, silenced=held is not None)
        try:
            choice = (ask("  > ") or "").strip().lower()
        except (EOFError, KeyboardInterrupt):
            screen.line()
            return 0

        if choice in ("q", "quit", "exit"):
            return 0
        if choice in ("", "r"):
            continue

        before = (read_status(config.status_path) or {}).get("generated_at")
        said = _do(choice, actions, config, ask, screen)
        if said is not None:
            ok, message = said
            screen.line("  " + screen.paint(message, GREEN if ok else RED))
            screen.line()
            if ok:
                _wait_for_the_agent(config, before, sleep)


def _do(
    choice: str, actions: Actions, config: Any, ask: Any, screen: Screen
) -> tuple[bool, str] | None:
    """One letter to one action. Anything unknown is ignored, not punished."""
    if choice == "a":
        payload = read_status(config.status_path) or {}
        fix = payload.get("fix")
        if not fix:
            return False, "no position, so the anchor cannot be set here (use --anchor-down --at)"
        radius = _ask_radius(ask, screen)
        if radius is None:
            return False, "that is not a number of metres"
        return actions.anchor_down(
            (float(fix[0]), float(fix[1])), radius, note="set from the console"
        )
    if choice == "u":
        return actions.anchor_up()
    if choice == "h":
        return actions.hush_for(30)
    if choice == "s":
        return actions.unhush()
    if choice == "z":
        if actions.silence.active() is not None:
            return actions.unsilence()
        if not _confirm_silence(ask, screen):
            return False, "left as it was: the alarms are still on"
        return actions.silence_all(note="silenced from the console")
    if choice == "w":
        return None if _show_forecast(config, screen) else (False, "no forecast")
    if choice == "x":
        return restart_agent()
    return None


def _confirm_silence(ask: Any, screen: Screen) -> bool:
    """One letter of friction before the boat stops being able to reach anybody.

    Not a nuisance and not a lecture: three lines that say exactly what is
    about to be switched off and that nothing will switch it back on. This is
    the only action in the console that can leave a person believing they are
    being watched when they are not, so it is the only one that asks.
    """
    screen.line()
    screen.line(screen.paint("  This turns off the speaker, Signal AND this screen.", RED, BOLD))
    screen.line(
        screen.paint("  It does not expire. Nothing reaches you until you press z again.", RED)
    )
    screen.line(screen.paint("  The boat keeps watching and keeps its log either way.", DIM))
    try:
        answer = (ask("  press y to silence her: ") or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


def restart_agent(run: Any = None) -> tuple[bool, str]:
    """Restart the agent without a password, or say why that is not possible.

    Only ever the user unit. `systemctl restart` on a system unit prompts for a
    password through polkit, which in a console like this means either a hang
    or a confusing second prompt, and the whole point of the user unit is that
    neither happens.
    """
    if run is None:
        # The lookup belongs to the real runner, not to the argument. A caller
        # that hands in its own runner is not asking systemctl anything, and
        # whether this machine happens to have it says nothing about that.
        if shutil.which("systemctl") is None:
            return False, "no systemctl here, so there is nothing to restart"
        run = _systemctl

    ok, detail = run(["--user", "restart", UNIT])
    if ok:
        return True, "restarted; give it a few seconds and press r"
    if "not loaded" in detail.lower() or "not found" in detail.lower():
        return False, "no user service installed. Run ./deploy/install-command.sh"
    return False, f"could not restart it: {detail}"


def _systemctl(args: list[str]) -> tuple[bool, str]:
    try:
        done = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stderr or done.stdout).strip()


def _ask_radius(ask: Any, screen: Screen) -> float | None:
    prompt = f"  circle in metres [{DEFAULT_RADIUS_M:.0f}]: "
    try:
        raw = (ask(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return DEFAULT_RADIUS_M
    try:
        return float(raw)
    except ValueError:
        return None


def _show_forecast(config: Any, screen: Screen) -> bool:
    """The hourly table, fetched now. The one thing here that needs a link."""
    from .geo import as_position
    from .units import cardinal, knots, zone_name
    from .weather import WeatherError, fetch_forecast

    payload = read_status(config.status_path) or {}
    fix = payload.get("fix")
    position = as_position(
        {"latitude": fix[0], "longitude": fix[1]} if fix else None
    )
    if position is None:
        screen.line("  " + screen.paint("no position, so there is nowhere to forecast for", RED))
        screen.line()
        return True

    screen.line(screen.paint("  fetching…", DIM))
    try:
        forecast = fetch_forecast(position)
    except WeatherError as exc:
        screen.line("  " + screen.paint(f"no forecast: {exc}", RED))
        screen.line()
        return True

    now = datetime.now(UTC)
    screen.line()
    header = zone_name() or "local"
    screen.line(screen.paint(f"  {header:<14}{'wind':>6}{'gust':>7}{'dir':>6}{'sea':>7}", DIM))
    for hour in forecast.window(config.anchor_outlook_hours, now):
        wind = "-" if hour.wind_ms is None else f"{knots(hour.wind_ms):.0f}kn"
        gust = "-" if hour.gust_ms is None else f"{knots(hour.gust_ms):.0f}kn"
        sea = "-" if hour.wave_m is None else f"{hour.wave_m:.1f}m"
        point = cardinal(hour.direction_rad) or "-"
        when = dayhhmm(hour.time)
        screen.line(f"  {when:<14}{wind:>6}{gust:>7}{point:>6}{sea:>7}")
    screen.line()
    return True

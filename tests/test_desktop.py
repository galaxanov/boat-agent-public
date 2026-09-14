from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.config import Config
from agent.desktop import DesktopNotifier, build_desktop_notifier, session_env
from agent.notify import format_alert
from agent.rules import Alert, AlertEvent, Severity

T0 = datetime(2026, 8, 22, 2, 55, tzinfo=UTC)


def event(
    kind: str = "raised",
    severity: Severity = Severity.ALARM,
    rule_id: str = "anchor_drag",
) -> AlertEvent:
    return AlertEvent(
        kind,
        Alert(
            rule_id=rule_id,
            severity=severity,
            message="Dragging: 222 m from the anchor, watch circle 30 m",
            since=T0,
            data={"distance_m": 222.4},
        ),
    )


def fake_notify_send(
    tmp_path: Path, exit_code: int = 0, printed_id: str = "42"
) -> tuple[Path, Path]:
    """A stand-in for notify-send that records the arguments it was given.

    Arguments are recorded one per line with a record separator between calls,
    because the body of a notification has newlines in it.
    """
    calls = tmp_path / "calls"
    script = tmp_path / "notify-send"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'{{ printf "%s\\x1f" "$@"; printf "\\x1e"; }} >> "{calls}"\n'
        f'echo "{printed_id}"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script, calls


def calls_made(calls: Path) -> list[list[str]]:
    if not calls.is_file():
        return []
    raw = calls.read_text(encoding="utf-8")
    return [
        record.split("\x1f")[:-1] for record in raw.split("\x1e") if record
    ]


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


# ------------------------------------------------------------- what is shown --


def test_an_alarm_reaches_the_screen_and_stays_there(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path)
    desktop = DesktopNotifier(command=str(script))

    assert asyncio.run(desktop.handle(event(), T0)) is True

    (argv,) = calls_made(calls)
    assert flag(argv, "--urgency") == "critical"
    # Zero means it waits to be dismissed rather than fading while nobody is
    # looking, which is the whole point for an alarm.
    assert flag(argv, "--expire-time") == "0"
    assert flag(argv, "--app-name") == "Seabird"


def test_a_warning_does_not_stick_to_the_screen(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path)
    desktop = DesktopNotifier(command=str(script))

    asyncio.run(desktop.handle(event(severity=Severity.WARN), T0))

    (argv,) = calls_made(calls)
    assert flag(argv, "--urgency") == "normal"
    assert int(flag(argv, "--expire-time")) > 0


def test_the_screen_says_the_same_thing_as_the_signal_message(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path)
    desktop = DesktopNotifier(command=str(script))
    raised = event()

    asyncio.run(desktop.handle(raised, T0))

    (argv,) = calls_made(calls)
    summary, body = argv[-2], argv[-1]
    assert f"{summary}\n{body}" == format_alert(raised, now=T0)
    assert "222 m from the anchor" in body


def test_quiet_alerts_are_left_off_the_screen(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path)
    desktop = DesktopNotifier(command=str(script), min_severity=Severity.WARN)

    assert asyncio.run(desktop.handle(event(severity=Severity.ALERT), T0)) is False
    assert calls_made(calls) == []


def test_a_turned_off_notifier_shows_nothing(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path)
    desktop = DesktopNotifier(command=str(script), enabled=False)

    assert asyncio.run(desktop.handle(event(), T0)) is False
    assert calls_made(calls) == []


# ------------------------------------------------------------- replacing it --


def test_one_rule_keeps_one_notification(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path, printed_id="99")
    desktop = DesktopNotifier(command=str(script))

    async def drive() -> None:
        await desktop.handle(event("raised"), T0)
        await desktop.handle(event("escalated", Severity.EMERGENCY), T0)

    asyncio.run(drive())

    first, second = calls_made(calls)
    assert flag(first, "--replace-id") is None
    # The worsening replaces the raise rather than sitting beside it
    # disagreeing about the state of the boat.
    assert flag(second, "--replace-id") == "99"


def test_clearing_lets_go_of_the_notification(tmp_path: Path) -> None:
    script, calls = fake_notify_send(tmp_path, printed_id="99")
    desktop = DesktopNotifier(command=str(script))

    async def drive() -> None:
        await desktop.handle(event("raised"), T0)
        await desktop.handle(event("cleared"), T0)
        await desktop.handle(event("raised"), T0)

    asyncio.run(drive())

    _raised, cleared, again = calls_made(calls)
    assert flag(cleared, "--replace-id") == "99"
    assert flag(cleared, "--urgency") == "normal", "a clear must not stick to the screen"
    assert flag(again, "--replace-id") is None


def test_a_notifier_that_says_nothing_useful_does_not_break_replacement(
    tmp_path: Path,
) -> None:
    script, calls = fake_notify_send(tmp_path, printed_id="not-a-number")
    desktop = DesktopNotifier(command=str(script))

    async def drive() -> None:
        await desktop.handle(event("raised"), T0)
        await desktop.handle(event("raised"), T0)

    asyncio.run(drive())

    _first, second = calls_made(calls)
    assert flag(second, "--replace-id") is None


# ------------------------------------------------------------- when it fails --


def test_a_missing_notify_send_is_said_once_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    desktop = DesktopNotifier(command=str(tmp_path / "there-is-no-such-program"))

    async def drive() -> None:
        for _ in range(3):
            assert await desktop.handle(event(), T0) is False

    with caplog.at_level("WARNING"):
        asyncio.run(drive())

    complaints = [r for r in caplog.records if "reaching this machine's screen" in r.message]
    assert len(complaints) == 1


def test_a_failing_notify_send_is_reported_not_raised(tmp_path: Path) -> None:
    script, _calls = fake_notify_send(tmp_path, exit_code=1)
    desktop = DesktopNotifier(command=str(script))

    assert asyncio.run(desktop.handle(event(), T0)) is False


def test_a_hanging_notify_send_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "notify-send"
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")
    script.chmod(0o755)
    desktop = DesktopNotifier(command=str(script), timeout=0.5)

    assert asyncio.run(desktop.handle(event(), T0)) is False


def test_the_message_bus_is_found_for_a_systemd_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    env = session_env()
    if Path(f"/run/user/{os.getuid()}/bus").exists():
        assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/run/user/")


# ------------------------------------------------------------------- config --


def test_the_notifier_is_built_from_config_and_survives_a_bad_severity() -> None:
    desktop = build_desktop_notifier(Config(desktop_min_severity="shouting"))
    assert desktop.min_severity is Severity.ALERT
    assert desktop.enabled is True


def test_config_reads_the_desktop_settings_from_the_environment() -> None:
    config = Config.from_env(
        {
            "AGENT_DESKTOP_NOTIFY": "no",
            "AGENT_DESKTOP_MIN_SEVERITY": "WARN",
            "AGENT_NOTIFY_SEND": "/usr/local/bin/notify-send",
        }
    )
    assert config.desktop_notify is False
    assert config.desktop_min_severity == "warn"
    assert config.notify_send_path == "/usr/local/bin/notify-send"

    assert Config.from_env({}).desktop_notify is True
    assert Config.from_env({}).desktop_min_severity == "alert"

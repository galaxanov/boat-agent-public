from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.notify import (
    AlertNotifier,
    LoggingNotifier,
    NotifyError,
    Outbox,
    SignalCliNotifier,
    build_notifier,
    format_alert,
    parse_recipients,
    stamp_delay,
)
from agent.rules import Alert, AlertEvent, Severity

T0 = datetime(2026, 8, 22, 9, 15, tzinfo=UTC)


def event(kind: str = "raised", severity: Severity = Severity.ALARM) -> AlertEvent:
    return AlertEvent(
        kind,
        Alert(
            rule_id="anchor_drag",
            severity=severity,
            message="Dragging: 222 m from the anchor, watch circle 30 m",
            since=T0,
            data={"distance_m": 222.4},
        ),
    )


def fake_cli(tmp_path: Path, exit_code: int = 0, stderr: str = "", sleep: float = 0) -> Path:
    """A stand-in for signal-cli that records how it was called."""
    script = tmp_path / "fake-signal-cli"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{tmp_path}/calls.txt"\n'
        f"sleep {sleep}\n"
        f'>&2 echo "{stderr}"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def picky_cli(tmp_path: Path, rejects: str) -> Path:
    """A signal-cli that works for every number except one."""
    script = tmp_path / "picky-signal-cli"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{tmp_path}/calls.txt"\n'
        f'if [[ "${{@: -1}}" == "{rejects}" ]]; then\n'
        '  >&2 echo "Failed to send: unregistered user"\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls.txt"
    return path.read_text().splitlines() if path.exists() else []


# ---------------------------------------------------------------- signal-cli --


def test_command_puts_the_account_before_the_subcommand() -> None:
    """-a means --account globally but --attachment after `send`."""
    notifier = SignalCliNotifier(account="+301", recipients=("+302",), cli_path="signal-cli")
    command = notifier._command("hello")
    assert command[:5] == ["signal-cli", "-a", "+301", "send", "-m"]
    assert command[-1] == "+302"
    assert command.index("-a") < command.index("send")


def test_group_takes_precedence_over_recipient() -> None:
    notifier = SignalCliNotifier(account="+301", recipients=("+302",), group_id="Z2lk")
    command = notifier._command("hi")
    assert "-g" in command and command[command.index("-g") + 1] == "Z2lk"
    assert "+302" not in command


def test_needs_a_destination() -> None:
    with pytest.raises(ValueError):
        SignalCliNotifier(account="+301")


def test_parse_recipients_accepts_commas_spaces_and_repeats() -> None:
    assert parse_recipients("+301, +302") == ("+301", "+302")
    assert parse_recipients(" +301   +302 ") == ("+301", "+302")
    assert parse_recipients("+301,+301") == ("+301",)
    assert parse_recipients("") == ()


def test_a_bare_string_still_works_as_one_recipient() -> None:
    notifier = SignalCliNotifier(account="+301", recipients="+302")
    assert notifier.recipients == ("+302",)


def test_every_recipient_gets_their_own_copy(tmp_path: Path) -> None:
    notifier = SignalCliNotifier(
        account="+301", recipients=("+302", "+303"), cli_path=str(fake_cli(tmp_path))
    )
    asyncio.run(notifier.send("dragging"))
    recorded = calls(tmp_path)
    assert "+302" in recorded and "+303" in recorded
    assert recorded.count("dragging") == 2


def test_one_bad_number_does_not_stop_the_others(tmp_path: Path) -> None:
    """The crew phone must still hear the alarm if the other number is wrong."""
    notifier = SignalCliNotifier(
        account="+301",
        recipients=("+302", "+303"),
        cli_path=str(picky_cli(tmp_path, rejects="+302")),
    )
    asyncio.run(notifier.send("dragging"))  # no raise: it would duplicate for +303
    assert "+303" in calls(tmp_path)


def test_every_recipient_failing_raises_so_the_outbox_retries(tmp_path: Path) -> None:
    notifier = SignalCliNotifier(
        account="+301",
        recipients=("+302", "+303"),
        cli_path=str(fake_cli(tmp_path, exit_code=1, stderr="Failed to send")),
    )
    with pytest.raises(NotifyError, match="exited 1"):
        asyncio.run(notifier.send("dragging"))


def test_successful_send(tmp_path: Path) -> None:
    notifier = SignalCliNotifier(
        account="+301", recipients=("+302",), cli_path=str(fake_cli(tmp_path))
    )
    asyncio.run(notifier.send("boat is fine"))
    assert "boat is fine" in calls(tmp_path)


def test_failed_send_raises_notify_error(tmp_path: Path) -> None:
    notifier = SignalCliNotifier(
        account="+301",
        recipients=("+302",),
        cli_path=str(fake_cli(tmp_path, exit_code=1, stderr="Failed to send")),
    )
    with pytest.raises(NotifyError, match="exited 1"):
        asyncio.run(notifier.send("nope"))


def test_timeout_raises_and_does_not_hang(tmp_path: Path) -> None:
    notifier = SignalCliNotifier(
        account="+301",
        recipients=("+302",),
        cli_path=str(fake_cli(tmp_path, sleep=5)),
        timeout=0.3,
    )
    with pytest.raises(NotifyError, match="timed out"):
        asyncio.run(notifier.send("slow"))


def test_missing_binary_raises_notify_error(tmp_path: Path) -> None:
    notifier = SignalCliNotifier(
        account="+301", recipients=("+302",), cli_path=str(tmp_path / "not-installed")
    )
    with pytest.raises(NotifyError, match="cannot run"):
        asyncio.run(notifier.send("hello"))


# -------------------------------------------------------------------- outbox --


def test_outbox_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    outbox = Outbox(path)
    outbox.add("alarm one", T0)
    outbox.add("alarm two", T0)

    reloaded = Outbox(path)
    assert [i.text for i in reloaded.items] == ["alarm one", "alarm two"]


def test_outbox_removes_sent_messages(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.json")
    outbox.add("one", T0)
    outbox.remove(outbox.items[0])
    assert len(outbox) == 0
    assert len(Outbox(tmp_path / "outbox.json")) == 0


def test_outbox_expires_stale_messages(tmp_path: Path) -> None:
    """A three-hour-old shoaling warning is not worth delivering."""
    outbox = Outbox(tmp_path / "outbox.json", max_age=3600)
    outbox.add("old news", T0)
    assert outbox.expire(T0 + timedelta(seconds=7200)) == 1
    assert len(outbox) == 0


def test_outbox_is_bounded(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.json", max_items=3)
    for i in range(10):
        outbox.add(f"message {i}", T0)
    assert len(outbox) == 3
    assert [i.text for i in outbox.items] == ["message 7", "message 8", "message 9"]


def test_corrupt_outbox_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text("{ this is not json")
    outbox = Outbox(path)
    assert len(outbox) == 0
    outbox.add("still works", T0)
    assert len(outbox) == 1


def test_outbox_skips_unreadable_entries(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    path.write_text(
        json.dumps(
            [
                {"text": "good", "created": T0.isoformat(), "attempts": 0},
                {"text": "no timestamp"},
                {"created": T0.isoformat()},
                "not even a dict",
            ]
        )
    )
    assert [i.text for i in Outbox(path).items] == ["good"]


# ---------------------------------------------------------------- formatting --


def test_format_alert_reads_sensibly() -> None:
    text = format_alert(event(), boat="Seabird", now=T0)
    assert text.startswith("ALARM - Seabird")
    assert "Dragging: 222 m" in text
    assert "since 09:15 UTC" in text


def test_format_cleared_says_cleared() -> None:
    text = format_alert(event(kind="cleared"), now=T0)
    assert text.startswith("Cleared - Seabird")


def test_delay_is_stamped_only_when_it_matters() -> None:
    assert stamp_delay("x", T0, T0 + timedelta(seconds=30)) == "x"
    delayed = stamp_delay("x", T0, T0 + timedelta(minutes=20))
    assert "delayed 20 min" in delayed


# ------------------------------------------------------------------ service --


class Recorder:
    """A notifier that can be told to fail, for testing the retry path."""

    name = "recorder"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send(self, text: str) -> None:
        if self.fail:
            raise NotifyError("link is down")
        self.sent.append(text)


def service(tmp_path: Path, notifier, min_severity=Severity.ALERT, now=T0) -> AlertNotifier:
    return AlertNotifier(
        notifier=notifier,
        outbox=Outbox(tmp_path / "outbox.json"),
        min_severity=min_severity,
        clock=lambda: now,
    )


def test_alert_is_sent(tmp_path: Path) -> None:
    recorder = Recorder()
    svc = service(tmp_path, recorder)
    asyncio.run(svc.handle(event()))
    assert len(recorder.sent) == 1
    assert len(svc.outbox) == 0


def test_low_severity_is_filtered_out(tmp_path: Path) -> None:
    recorder = Recorder()
    svc = service(tmp_path, recorder, min_severity=Severity.ALARM)
    asyncio.run(svc.handle(event(severity=Severity.ALERT)))
    assert recorder.sent == []
    assert len(svc.outbox) == 0  # filtered, not queued


def test_failure_keeps_the_message_for_later(tmp_path: Path) -> None:
    """An alarm raised while Starlink is down must not be lost."""
    down = Recorder(fail=True)
    svc = service(tmp_path, down)
    asyncio.run(svc.handle(event()))

    assert down.sent == []
    assert len(svc.outbox) == 1
    assert svc.outbox.items[0].attempts == 1


def test_queued_messages_go_out_when_the_link_returns(tmp_path: Path) -> None:
    down = Recorder(fail=True)
    svc = service(tmp_path, down)
    asyncio.run(svc.handle(event()))
    assert len(svc.outbox) == 1

    svc.notifier = Recorder()  # link back up
    svc.clock = lambda: T0 + timedelta(minutes=20)
    sent = asyncio.run(svc.flush())

    assert sent == 1
    assert len(svc.outbox) == 0
    # It must not arrive looking like it just happened.
    assert "delayed 20 min" in svc.notifier.sent[0]


def test_a_restart_during_an_outage_still_delivers(tmp_path: Path) -> None:
    down = Recorder(fail=True)
    svc = service(tmp_path, down)
    asyncio.run(svc.handle(event()))

    # Agent restarts: fresh objects, same outbox file.
    recovered = AlertNotifier(
        notifier=Recorder(),
        outbox=Outbox(tmp_path / "outbox.json"),
        clock=lambda: T0 + timedelta(minutes=5),
    )
    assert asyncio.run(recovered.flush()) == 1
    assert "Dragging" in recovered.notifier.sent[0]


def test_order_is_preserved_and_the_queue_stops_at_the_first_failure(tmp_path: Path) -> None:
    class FailsAfterOne(Recorder):
        async def send(self, text: str) -> None:
            if self.sent:
                raise NotifyError("link dropped mid-flush")
            self.sent.append(text)

    svc = service(tmp_path, FailsAfterOne())
    svc.outbox.add("first", T0)
    svc.outbox.add("second", T0)
    svc.outbox.add("third", T0)

    assert asyncio.run(svc.flush()) == 1
    assert [i.text for i in svc.outbox.items] == ["second", "third"]


def test_an_unexpected_error_does_not_escape(tmp_path: Path) -> None:
    class Exploding:
        name = "boom"

        async def send(self, text: str) -> None:
            raise RuntimeError("something unforeseen")

    svc = service(tmp_path, Exploding())
    asyncio.run(svc.handle(event()))  # must not raise
    assert len(svc.outbox) == 1


# ------------------------------------------------------------------- config --


def test_unconfigured_falls_back_to_logging() -> None:
    from agent.config import Config

    notifier = build_notifier(Config.from_env({}))
    assert isinstance(notifier, LoggingNotifier)
    asyncio.run(notifier.send("goes nowhere"))  # never fails


def test_configured_builds_a_signal_notifier() -> None:
    from agent.config import Config

    config = Config.from_env({"SIGNAL_ACCOUNT": "+301", "SIGNAL_RECIPIENT": "+302"})
    notifier = build_notifier(config)
    assert isinstance(notifier, SignalCliNotifier)
    assert notifier.name == "signal"


def test_config_reads_several_numbers_from_one_variable() -> None:
    from agent.config import Config

    config = Config.from_env({"SIGNAL_ACCOUNT": "+301", "SIGNAL_RECIPIENT": "+302, +303"})
    assert config.signal_recipients == ("+302", "+303")
    notifier = build_notifier(config)
    assert isinstance(notifier, SignalCliNotifier)
    assert notifier.recipients == ("+302", "+303")


def test_account_without_a_destination_falls_back_rather_than_crashing() -> None:
    from agent.config import Config

    config = Config.from_env({"SIGNAL_ACCOUNT": "+301"})
    assert isinstance(build_notifier(config), LoggingNotifier)


def test_outbox_survives_an_unwritable_directory(tmp_path: Path) -> None:
    """A read-only disk must not take the agent down."""
    blocked = tmp_path / "ro"
    blocked.mkdir()
    (blocked / "outbox.json").write_text("[]")
    os.chmod(blocked, 0o500)
    try:
        outbox = Outbox(blocked / "outbox.json")
        outbox.add("cannot be saved", T0)  # must not raise
        assert len(outbox) == 1
    finally:
        os.chmod(blocked, 0o700)

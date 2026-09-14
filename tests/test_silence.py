"""Turning every alert channel off, and the promises that come with it.

Four things matter here, and they are the four the design rests on. Off means
all three channels and not just the speaker. It does not expire, so the file is
the only thing that ends it. It nags, because it does not expire. And lifting
it re-announces whatever is still standing, so a drag alarm raised during a
silence cannot vanish into the log.

The rule loop is driven directly rather than through main(), with recording
fakes for the three channels, because what is being asserted is exactly which
of them was reached.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import main as M
from agent.config import Config
from agent.logbook import Logbook
from agent.rules import Alert, AlertEvent, RuleEngine, Severity
from agent.silence import Nag, Silence, SilenceFile, silence_now
from agent.state import BoatState

NOW = datetime(2026, 9, 8, 21, 40, tzinfo=UTC)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        anchor_file=tmp_path / "anchor.json",
        hush_file=tmp_path / "hush.json",
        silence_file=tmp_path / "silence.json",
        status_path=tmp_path / "status.json",
        outbox_path=tmp_path / "outbox.json",
        log_dir=tmp_path / "logs",
        rule_interval=0.01,
        weather=False,
    )


# -------------------------------------------------------------------- file --


def test_an_absent_file_means_the_alarms_are_on(config) -> None:
    assert SilenceFile(config.silence_file).active() is None


def test_it_survives_being_written_and_read_back(config) -> None:
    written = SilenceFile(config.silence_file)
    assert written.write(silence_now("hauled out at the yard", now=NOW))

    read = SilenceFile(config.silence_file).active()
    assert read is not None
    assert read.since == NOW
    assert read.note == "hauled out at the yard"


def test_it_does_not_expire(config) -> None:
    """The whole difference from a hush, and the reason for everything else."""
    silence_file = SilenceFile(config.silence_file)
    silence_file.write(silence_now(now=NOW - timedelta(days=40)))

    held = SilenceFile(config.silence_file).active()
    assert held is not None
    assert held.held_for(NOW) == "40 days"


def test_clearing_it_gives_every_channel_back(config) -> None:
    silence_file = SilenceFile(config.silence_file)
    silence_file.write(silence_now(now=NOW))
    assert silence_file.write(None)
    assert silence_file.active() is None


def test_a_file_written_by_somebody_else_is_picked_up_without_a_restart(config) -> None:
    """The file is the interface: the CLI writes it and the agent notices."""
    watching = SilenceFile(config.silence_file)
    assert watching.active() is None

    SilenceFile(config.silence_file).write(silence_now(now=NOW))
    assert watching.active() is not None


def test_an_unreadable_file_leaves_the_alarms_on(config) -> None:
    """Every other guess ends with a boat that has quietly stopped talking."""
    config.silence_file.parent.mkdir(parents=True, exist_ok=True)
    config.silence_file.write_text("{not json", encoding="utf-8")
    assert SilenceFile(config.silence_file).active() is None


def test_a_file_with_no_since_is_not_a_silence(config) -> None:
    config.silence_file.parent.mkdir(parents=True, exist_ok=True)
    config.silence_file.write_text(json.dumps({"note": "hmm"}), encoding="utf-8")
    assert SilenceFile(config.silence_file).active() is None


def test_a_naive_stamp_is_read_as_utc() -> None:
    held = Silence.from_dict({"since": "2026-09-08T21:40:00"})
    assert held is not None
    assert held.since == NOW


def test_the_banner_says_what_is_off_and_for_how_long() -> None:
    held = silence_now("laid up", now=NOW - timedelta(hours=6))
    line = held.describe(NOW)
    assert "SILENCED" in line
    assert "6 h ago" in line
    assert "no alarm will reach you" in line
    assert "laid up" in line


# --------------------------------------------------------------------- nag --


def test_the_nag_speaks_at_once_then_holds_its_tongue() -> None:
    nag = Nag(interval_s=1800)
    assert nag.due(NOW)
    assert not nag.due(NOW + timedelta(minutes=20))
    assert nag.due(NOW + timedelta(minutes=31))


def test_a_reset_nag_speaks_again_immediately() -> None:
    nag = Nag(interval_s=1800)
    nag.due(NOW)
    nag.reset()
    assert nag.due(NOW + timedelta(seconds=1))


# ---------------------------------------------------------------- channels --


class Recorder:
    """Stands in for a channel and remembers what it was asked to say."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def handle(self, event) -> bool:
        self.said.append(f"{event.kind}:{event.alert.rule_id}")
        return True


class FakeAlarm:
    """The speaker, counted rather than heard."""

    enabled = True
    hush = None

    def __init__(self) -> None:
        self.soundings = 0
        self.noticed: list[str] = []

    def notice(self, event) -> None:
        self.noticed.append(event.alert.rule_id)

    async def tick(self, active, now=None) -> bool:
        if list(active):
            self.soundings += 1
            return True
        return False


class FakeNotifier(Recorder):
    """AlertNotifier's shape, minus signal-cli and the JVM behind it."""

    def __init__(self) -> None:
        super().__init__()
        self.outbox: list[str] = []
        self.flushes = 0

    async def flush(self) -> int:
        self.flushes += 1
        return 0


class OneRule:
    """Fires while the flag is up. Enough to make a transition happen."""

    id = "drill_rule"
    for_seconds = 0.0
    clear_after = 0.0

    def __init__(self) -> None:
        self.firing = True

    def check(self, state, active):
        from agent.rules import Finding

        if not self.firing:
            return None
        return Finding(severity=Severity.ALARM, message="the test alarm", data={})


async def one_tick(config, engine, notifier, desktop, alarm, logbook, stop):
    """Run rules_loop for exactly one pass and stop it."""

    async def halt() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    from agent.derived import StateDeriver

    await asyncio.gather(
        M.rules_loop(
            BoatState(),
            logbook,
            engine,
            notifier,
            desktop,
            alarm,
            StateDeriver(),
            config,
            stop,
        ),
        halt(),
    )


def run_loop(config, engine, notifier, desktop, alarm):
    stop = asyncio.Event()
    with Logbook(config.log_dir) as logbook:
        asyncio.run(one_tick(config, engine, notifier, desktop, alarm, logbook, stop))


def test_a_silence_stops_the_speaker_signal_and_the_screen_together(config) -> None:
    """The point of the whole feature: off means all three, not just the noise."""
    SilenceFile(config.silence_file).write(silence_now(now=NOW))
    engine = RuleEngine([OneRule()])
    notifier, desktop, alarm = FakeNotifier(), Recorder(), FakeAlarm()

    run_loop(config, engine, notifier, desktop, alarm)

    assert engine.active, "the rule must still fire: only the ways out are shut"
    assert notifier.said == []
    assert desktop.said == []
    assert alarm.soundings == 0
    assert notifier.flushes == 0


def test_without_a_silence_all_three_channels_are_used(config) -> None:
    engine = RuleEngine([OneRule()])
    notifier, desktop, alarm = FakeNotifier(), Recorder(), FakeAlarm()

    run_loop(config, engine, notifier, desktop, alarm)

    # One transition, so one message and one notification. The speaker sounds
    # on what is standing rather than on what changed, so it keeps going.
    assert notifier.said == ["raised:drill_rule"]
    assert desktop.said == ["raised:drill_rule"]
    assert alarm.soundings >= 1


def test_a_silenced_boat_still_writes_everything_down(config) -> None:
    """Watching, deciding and recording are untouched. Only telling stops."""
    SilenceFile(config.silence_file).write(silence_now("alongside", now=NOW))
    engine = RuleEngine([OneRule()])

    run_loop(config, engine, FakeNotifier(), Recorder(), FakeAlarm())

    written = [
        json.loads(line)
        for path in sorted(config.log_dir.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [r.get("event") for r in written]
    assert "alert_raised" in kinds, "the alert is logged even though nobody was told"
    assert "silenced" in kinds, "and the silence is logged, so the recap can say so"
    noted = next(r for r in written if r.get("event") == "silenced")
    assert noted["note"] == "alongside"


def test_lifting_it_re_announces_whatever_is_still_standing(config) -> None:
    """The failure this exists to prevent: an alarm nobody is ever told about.

    Signal and the screen only see transitions. An alert raised during a
    silence had its transition while nobody was listening, so turning the
    alarms back on has to say it again or it is never said at all.
    """
    engine = RuleEngine([OneRule()])
    engine.evaluate(BoatState())  # raise it, off the record
    assert engine.active

    notifier, desktop = FakeNotifier(), Recorder()
    standing = asyncio.run(M.resume_after_silence(engine, notifier, desktop))

    assert standing == 1
    assert notifier.said == ["raised:drill_rule"]
    assert desktop.said == ["raised:drill_rule"]


def test_lifting_it_with_nothing_standing_says_nothing(config) -> None:
    notifier, desktop = FakeNotifier(), Recorder()
    standing = asyncio.run(M.resume_after_silence(RuleEngine([]), notifier, desktop))

    assert standing == 0
    assert notifier.said == []


# -------------------------------------------------------------- the command --


def test_the_command_writes_the_file_and_says_it_does_not_expire(config, caplog) -> None:
    assert M.silence(config, "ashore for a week") == 0

    held = SilenceFile(config.silence_file).active()
    assert held is not None
    assert held.note == "ashore for a week"
    assert "does NOT expire" in caplog.text
    assert "boat --unsilence" in caplog.text


def test_silencing_twice_does_not_restart_the_clock(config) -> None:
    """How long she has been quiet is the figure the nag is built on."""
    SilenceFile(config.silence_file).write(silence_now(now=NOW - timedelta(hours=9)))

    assert M.silence(config, "again") == 0
    held = SilenceFile(config.silence_file).active()
    assert held is not None
    assert held.since == NOW - timedelta(hours=9)


def test_the_other_command_turns_them_back_on(config) -> None:
    SilenceFile(config.silence_file).write(silence_now(now=NOW))
    assert M.unsilence(config) == 0
    assert SilenceFile(config.silence_file).active() is None


def test_turning_them_back_on_when_they_were_already_on_is_not_an_error(config) -> None:
    assert M.unsilence(config) == 0


def test_the_flag_takes_an_optional_note() -> None:
    assert M.parse_args(["--silence"]).silence == ""
    assert M.parse_args(["--silence", "laid up"]).silence == "laid up"
    assert M.parse_args(["--anchor-up"]).silence is None
    assert M.parse_args(["--unsilence"]).unsilence is True


# ------------------------------------------------------------------- drill --


def test_a_drill_fails_while_she_is_silenced(config) -> None:
    """A green table under a standing silence is the worst thing it could print."""
    SilenceFile(config.silence_file).write(silence_now(now=NOW))
    assert asyncio.run(M.drill(config)) == 1


def test_the_drill_result_records_the_silence(config) -> None:
    SilenceFile(config.silence_file).write(silence_now(now=NOW))
    asyncio.run(M.drill(config))

    drills = [
        json.loads(line)
        for path in sorted(config.log_dir.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == "drill"
    ]
    assert drills and drills[-1]["silenced"] is True


def test_an_alert_event_is_what_gets_replayed() -> None:
    """Guards the shape resume_after_silence builds by hand."""
    event = AlertEvent(
        "raised",
        Alert(rule_id="anchor_drag", severity=Severity.ALARM, message="dragging", since=NOW),
    )
    assert event.as_dict()["event"] == "alert_raised"

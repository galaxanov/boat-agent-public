from __future__ import annotations

import asyncio
import json
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import sound
from agent.rules import Alert, AlertEvent, Severity
from agent.sound import (
    Hush,
    HushFile,
    LocalAlarm,
    Player,
    audio_env,
    build_alarm_wav,
    build_local_alarm,
    hush_until,
)

T0 = datetime(2026, 8, 22, 2, 55, tzinfo=UTC)


def alert(severity: Severity = Severity.ALARM, rule_id: str = "anchor_drag") -> Alert:
    return Alert(
        rule_id=rule_id,
        severity=severity,
        message="Dragging: 222 m from the anchor, watch circle 30 m",
        since=T0,
    )


class StubPlayer:
    """Counts what it was asked to play, without making a sound."""

    def __init__(self, works: bool = True) -> None:
        self.works = works
        self.plays: list[Path] = []

    def specs(self) -> list[tuple[str, ...]]:
        return [("stub", "{file}")]

    async def play(self, path: Path) -> str:
        self.plays.append(Path(path))
        return "stub" if self.works else ""


def make_alarm(tmp_path: Path, player: StubPlayer | None = None, **kwargs) -> LocalAlarm:
    return LocalAlarm(
        player=player or StubPlayer(),
        hush=kwargs.pop("hush", None),
        wav_path=tmp_path / "alarm.wav",
        **kwargs,
    )


# ------------------------------------------------------------------ the wav --


def test_the_alarm_sound_is_a_real_playable_wav(tmp_path: Path) -> None:
    path = build_alarm_wav(tmp_path / "alarm.wav")
    assert path is not None

    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == sound.SAMPLE_RATE
        seconds = handle.getnframes() / handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    # Long enough to be heard through a bulkhead, short enough to leave gaps
    # between soundings for someone to shout over.
    assert 2.0 < seconds < 5.0
    assert any(frames), "the alarm is silence"


def test_the_sound_is_written_once_and_then_reused(tmp_path: Path) -> None:
    path = build_alarm_wav(tmp_path / "alarm.wav")
    assert path is not None
    stamp = path.stat().st_mtime_ns

    assert build_alarm_wav(path) == path
    assert path.stat().st_mtime_ns == stamp


def test_an_unwritable_sound_is_reported_not_raised(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    assert build_alarm_wav(blocked / "alarm.wav") is None


# ------------------------------------------------------------- the players --


def fake_player(tmp_path: Path, name: str, exit_code: int = 0) -> Path:
    """A stand-in for aplay that records the arguments it was given."""
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/{name}.calls"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


def test_an_explicit_player_is_used_with_the_file_substituted(tmp_path: Path) -> None:
    script = fake_player(tmp_path, "myplayer")
    player = Player(override=f"{script} --loud {{file}}")

    assert asyncio.run(player.play(tmp_path / "alarm.wav")) == str(script)
    called = (tmp_path / "myplayer.calls").read_text().strip()
    assert called == f"--loud {tmp_path / 'alarm.wav'}"


def test_a_player_without_a_placeholder_still_gets_the_file(tmp_path: Path) -> None:
    script = fake_player(tmp_path, "myplayer")
    player = Player(override=str(script))

    assert asyncio.run(player.play(tmp_path / "alarm.wav"))
    assert (tmp_path / "myplayer.calls").read_text().strip() == str(tmp_path / "alarm.wav")


def test_a_failing_player_falls_through_to_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_player(tmp_path, "brokenplayer", exit_code=1)
    fake_player(tmp_path, "goodplayer")
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr(
        sound, "PLAYERS", (("brokenplayer", "{file}"), ("goodplayer", "{file}"))
    )

    player = Player()
    assert asyncio.run(player.play(tmp_path / "alarm.wav")) == "goodplayer"

    # And the one that worked is tried first next time, rather than walking
    # past the broken one on every sounding.
    assert player.specs()[0][0] == "goodplayer"


def test_no_player_at_all_is_reported_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sound, "PLAYERS", (("definitely-not-installed", "{file}"),))
    player = Player()
    assert player.specs() == []
    assert asyncio.run(player.play(tmp_path / "alarm.wav")) == ""


def test_a_player_that_hangs_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "sleepy"
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")
    script.chmod(0o755)

    player = Player(override=str(script), timeout=0.5)
    assert asyncio.run(player.play(tmp_path / "alarm.wav")) == ""


def test_the_sound_server_socket_is_found_for_a_systemd_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    env = audio_env()
    # Only on a machine that has one - a Pi under systemd may not.
    if Path(f"/run/user/{__import__('os').getuid()}").is_dir():
        assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")


# ---------------------------------------------------------------- the hush --


def test_a_hush_expires(tmp_path: Path) -> None:
    hush_file = HushFile(tmp_path / "hush.json")
    assert hush_file.write(Hush(until=T0 + timedelta(minutes=30)))

    assert hush_file.active(T0) is not None
    assert hush_file.active(T0 + timedelta(minutes=29)) is not None
    assert hush_file.active(T0 + timedelta(minutes=31)) is None


def test_a_hush_can_be_ended_early(tmp_path: Path) -> None:
    hush_file = HushFile(tmp_path / "hush.json")
    hush_file.write(Hush(until=T0 + timedelta(hours=8)))
    assert hush_file.active(T0) is not None

    assert hush_file.write(None)
    assert hush_file.active(T0) is None


def test_a_damaged_hush_file_does_not_silence_anything(tmp_path: Path) -> None:
    path = tmp_path / "hush.json"
    path.write_text("{ this is not json")
    assert HushFile(path).active(T0) is None

    path.write_text(json.dumps({"until": "not a date"}))
    assert HushFile(path).active(T0) is None

    assert HushFile(tmp_path / "never-written.json").active(T0) is None


def test_hush_until_counts_from_now() -> None:
    entry = hush_until(45, now=T0)
    assert entry.until == T0 + timedelta(minutes=45)


# --------------------------------------------------------------- the alarm --


def test_nothing_sounds_when_nothing_is_wrong(tmp_path: Path) -> None:
    player = StubPlayer()
    alarm = make_alarm(tmp_path, player)

    assert asyncio.run(alarm.tick([], T0)) is False
    assert player.plays == []


def test_a_warning_is_not_loud_enough_to_wake_anyone(tmp_path: Path) -> None:
    player = StubPlayer()
    alarm = make_alarm(tmp_path, player)

    assert asyncio.run(alarm.tick([alert(Severity.WARN)], T0)) is False
    assert asyncio.run(alarm.tick([alert(Severity.ALERT)], T0)) is False
    assert player.plays == []


def test_an_alarm_sounds_and_keeps_sounding_until_it_clears(tmp_path: Path) -> None:
    player = StubPlayer()
    alarm = make_alarm(tmp_path, player, repeat_s=20.0)
    standing = [alert()]

    async def drive() -> None:
        await alarm.tick(standing, T0)
        await alarm.drain()
        # Too soon: one long bleat rather than a rhythm helps nobody.
        await alarm.tick(standing, T0 + timedelta(seconds=10))
        await alarm.drain()
        await alarm.tick(standing, T0 + timedelta(seconds=21))
        await alarm.drain()
        # Cleared, so it stops on its own.
        await alarm.tick([], T0 + timedelta(seconds=45))
        await alarm.drain()
        await alarm.tick([], T0 + timedelta(seconds=90))
        await alarm.drain()

    asyncio.run(drive())
    assert len(player.plays) == 2
    assert alarm.sounding is False


def test_something_getting_worse_sounds_at_once(tmp_path: Path) -> None:
    player = StubPlayer()
    alarm = make_alarm(tmp_path, player, repeat_s=300.0)
    standing = [alert()]

    async def drive() -> None:
        await alarm.tick(standing, T0)
        await alarm.drain()
        alarm.notice(AlertEvent("escalated", alert(Severity.EMERGENCY)))
        await alarm.tick(standing, T0 + timedelta(seconds=2))
        await alarm.drain()

    asyncio.run(drive())
    assert len(player.plays) == 2, "an escalation waited for the repeat timer"


def test_a_quiet_warning_does_not_trigger_the_next_sounding(tmp_path: Path) -> None:
    player = StubPlayer()
    alarm = make_alarm(tmp_path, player, repeat_s=300.0)

    alarm.notice(AlertEvent("raised", alert(Severity.WARN)))
    assert asyncio.run(alarm.tick([alert(Severity.WARN)], T0)) is False
    assert player.plays == []


def test_a_hush_stops_the_noise_but_the_alarm_resumes_when_it_expires(
    tmp_path: Path,
) -> None:
    player = StubPlayer()
    hush_file = HushFile(tmp_path / "hush.json")
    hush_file.write(Hush(until=T0 + timedelta(minutes=30)))
    alarm = make_alarm(tmp_path, player, hush=hush_file, repeat_s=20.0)
    standing = [alert()]

    async def drive() -> None:
        alarm.notice(AlertEvent("raised", alert()))
        await alarm.tick(standing, T0)
        await alarm.drain()
        assert player.plays == [], "hushed, and it sounded anyway"
        # The trigger is held rather than dropped, so the moment the quiet
        # period ends the standing alarm is heard.
        await alarm.tick(standing, T0 + timedelta(minutes=31))
        await alarm.drain()

    asyncio.run(drive())
    assert len(player.plays) == 1


def test_a_turned_off_alarm_stays_silent(tmp_path: Path) -> None:
    player = StubPlayer()
    alarm = make_alarm(tmp_path, player, enabled=False)

    alarm.notice(AlertEvent("raised", alert()))
    assert asyncio.run(alarm.tick([alert()], T0)) is False
    assert player.plays == []


def test_a_machine_that_cannot_make_a_noise_does_not_stop_the_agent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    player = StubPlayer(works=False)
    alarm = make_alarm(tmp_path, player, repeat_s=0.0)

    async def drive() -> None:
        for offset in (0, 1, 2):
            await alarm.tick([alert()], T0 + timedelta(seconds=offset))
            await alarm.drain()

    with caplog.at_level("ERROR"):
        asyncio.run(drive())

    assert len(player.plays) == 3
    # Said once, so it cannot bury the alert that caused it.
    complaints = [r for r in caplog.records if "would play the alarm sound" in r.message]
    assert len(complaints) == 1


def test_a_player_that_explodes_is_swallowed(tmp_path: Path) -> None:
    class Exploding(StubPlayer):
        async def play(self, path: Path) -> str:
            raise RuntimeError("the sound card caught fire")

    alarm = make_alarm(tmp_path, Exploding())

    async def drive() -> None:
        await alarm.tick([alert()], T0)
        await alarm.drain()

    asyncio.run(drive())  # no raise


def test_the_alarm_is_built_from_config_and_survives_a_bad_severity(
    tmp_path: Path,
) -> None:
    from agent.config import Config

    config = Config(
        alarm_min_severity="deafening",
        alarm_repeat=15.0,
        hush_file=tmp_path / "hush.json",
        silence_file=tmp_path / "silence.json",
        alarm_wav_path=tmp_path / "alarm.wav",
    )
    alarm = build_local_alarm(config)

    assert alarm.min_severity is Severity.ALARM
    assert alarm.repeat_s == 15.0
    assert alarm.enabled is True


def test_config_reads_the_alarm_settings_from_the_environment() -> None:
    from agent.config import Config

    config = Config.from_env(
        {
            "AGENT_ALARM_SOUND": "off",
            "AGENT_ALARM_MIN_SEVERITY": "WARN",
            "AGENT_ALARM_REPEAT": "45",
            "AGENT_ALARM_PLAYER": "aplay -q {file}",
        }
    )
    assert config.alarm_sound is False
    assert config.alarm_min_severity == "warn"
    assert config.alarm_repeat == 45.0
    assert config.alarm_player == "aplay -q {file}"

    assert Config.from_env({}).alarm_sound is True

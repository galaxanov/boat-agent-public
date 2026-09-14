"""The drill: does the boat's alarm actually reach anybody?

--test-alarm proves the speaker and nothing else. This proves the chain, and
the half that matters is the failure reporting: a drill that can only report
success is theatre. Every test here is about what it says when something is
broken or switched off.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent import main as M
from agent.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        log_dir=tmp_path / "logs",
        outbox_path=tmp_path / "outbox.json",
        alarm_wav_path=tmp_path / "alarm.wav",
        hush_file=tmp_path / "hush.json",
        silence_file=tmp_path / "silence.json",
        anchor_file=tmp_path / "anchor.json",
        status_path=tmp_path / "status.json",
        desktop_notify=False,
        alarm_sound=False,
    )


def run(config: Config) -> int:
    return asyncio.run(M.drill(config))


def outcomes(monkeypatch, config: Config) -> dict[str, M.DrillResult]:
    """Run the drill and collect what each channel reported."""
    seen: list[M.DrillResult] = []
    original_signal, original_screen, original_speaker = (
        M._drill_signal, M._drill_screen, M._drill_speaker
    )

    async def spy(fn, *args):
        result = await fn(*args)
        seen.append(result)
        return result

    monkeypatch.setattr(M, "_drill_signal", lambda c, e: spy(original_signal, c, e))
    monkeypatch.setattr(M, "_drill_screen", lambda c, e: spy(original_screen, c, e))
    monkeypatch.setattr(M, "_drill_speaker", lambda c, e: spy(original_speaker, c, e))
    run(config)
    return {r.channel: r for r in seen}


# ------------------------------------------------------------------- off --


def test_a_channel_switched_off_is_reported_not_hidden(monkeypatch, config) -> None:
    got = outcomes(monkeypatch, config)

    assert got["Signal"].outcome == "off"
    assert "install-signal-cli" in got["Signal"].detail  # says how to fix it
    assert got["Screen"].outcome == "off"
    assert got["Speaker"].outcome == "off"


def test_everything_off_is_a_failure_even_though_nothing_broke(config) -> None:
    """Three channels all switched off is a boat that cannot raise the alarm,
    and reporting that as a pass would be the worst possible outcome."""
    assert run(config) == 1


def test_the_speaker_being_off_says_what_it_now_depends_on(monkeypatch, config) -> None:
    got = outcomes(monkeypatch, config)
    assert "depends on the link" in got["Speaker"].detail


# ---------------------------------------------------------------- broken --


def test_a_player_that_will_not_play_is_a_failure(monkeypatch, config) -> None:
    loud = replace(config, alarm_sound=True, alarm_player="/bin/false {file}")
    got = outcomes(monkeypatch, loud)

    assert got["Speaker"].outcome == "failed"
    assert got["Speaker"].failed
    assert run(loud) == 1


def test_a_message_that_only_queues_is_a_failure_not_a_success(monkeypatch, config) -> None:
    """The outbox holding it means the link is down. It will go later, and
    "later" is not what a drill is asking about."""
    sending = replace(config, signal_account="+15555550123", signal_group="g=")

    class Refuses:
        name = "signal"

        async def send(self, text: str) -> None:
            from agent.notify import NotifyError

            raise NotifyError("no link")

    monkeypatch.setattr(M, "build_notifier", lambda _c: Refuses())
    got = outcomes(monkeypatch, sending)

    assert got["Signal"].outcome == "failed"
    assert "queued but not delivered" in got["Signal"].detail


def test_a_channel_that_raises_is_caught_and_reported(monkeypatch, config) -> None:
    """A bug in one channel must not stop the other two being tested."""
    sending = replace(config, signal_account="+15555550123")

    def explode(_c):
        raise RuntimeError("boom")

    monkeypatch.setattr(M, "build_notifier", explode)
    got = outcomes(monkeypatch, sending)

    assert got["Signal"].outcome == "failed"
    assert "boom" in got["Signal"].detail
    assert "could not be set up" in got["Signal"].detail
    assert "Speaker" in got  # the rest still ran


# ---------------------------------------------------------------- record --


def test_the_drill_is_written_down_as_an_event_not_as_an_alarm(config) -> None:
    """It must not appear in the ship's log among the night's real alarms."""
    run(config)

    lines = sorted((config.log_dir).glob("*.jsonl"))[0].read_text()
    assert '"event": "drill"' in lines
    assert '"type": "alert"' not in lines


def test_it_always_says_what_it_cannot_prove(config, caplog) -> None:
    """The caveat is the most important line it prints."""
    with caplog.at_level("INFO"):
        run(config)
    assert "cannot prove a message woke you" in caplog.text

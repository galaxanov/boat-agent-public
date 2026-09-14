"""The one command: what it draws, and what a letter does.

Driven with a scripted `ask` and a StringIO screen, so the whole loop runs
without a terminal. The things worth checking are that an absent reading is
drawn absent, that a stale picture says so instead of looking calm, and that a
letter writes the same file the CLI writes.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import console as C
from agent import ui as U
from agent.anchor import AnchorFile
from agent.config import Config
from agent.derived import Confidence, Derived, VesselState
from agent.rules import Alert, Severity
from agent.state import BoatState

from .conftest import push

NOW = datetime(2026, 9, 8, 19, 30, tzinfo=UTC)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        anchor_file=tmp_path / "anchor.json",
        hush_file=tmp_path / "hush.json",
        silence_file=tmp_path / "silence.json",
        status_path=tmp_path / "status.json",
        log_dir=tmp_path / "logs",
    )


def screen() -> tuple[C.Screen, io.StringIO]:
    buffer = io.StringIO()
    return C.Screen(buffer, colour=False), buffer


def derived() -> Derived:
    return Derived(
        vessel=VesselState.ANCHORED,
        confidence=Confidence.LIKELY,
        reason="stopped inside the anchor circle",
    )


def payload(config: Config, clock, **over):
    state = BoatState(clock=clock)
    push(state, {"navigation.speedOverGround": 0.1, "environment.depth.belowTransducer": 8.4})
    built = U.build_payload(state, derived(), [], None, None, None, config, now=NOW)
    built.update(over)
    return built


# ------------------------------------------------------------------ drawing --


def test_no_status_file_says_the_agent_is_not_running(config) -> None:
    sc, out = screen()
    C.render(U.read_status(config.status_path), sc)

    assert "not running" in out.getvalue()
    assert "systemctl --user start boat-agent" in out.getvalue()


def test_a_stale_picture_says_so_rather_than_looking_calm(config, clock) -> None:
    """The worst failure here is a screen that looks fine because it is old."""
    sc, out = screen()
    C.render(payload(config, clock), sc, now=NOW + timedelta(minutes=9))

    text = out.getvalue()
    assert "min ago" in text
    assert "agent may be down" in text


def test_a_fresh_picture_just_shows_the_time(config, clock) -> None:
    sc, out = screen()
    C.render(payload(config, clock), sc, now=NOW + timedelta(seconds=4))

    assert "19:30 UTC" in out.getvalue()
    assert "may be down" not in out.getvalue()


def test_an_instrument_that_said_nothing_is_drawn_absent(config, clock) -> None:
    sc, out = screen()
    C.render(payload(config, clock), sc, now=NOW)

    text = out.getvalue()
    assert "Depth" in text and "8.4" in text
    # Battery was never reported, so it is a dash and never a zero.
    assert "Battery -" in text.replace("  ", " ")
    assert "0.00" not in text


def test_alerts_are_drawn_first_and_say_how_long(config, clock) -> None:
    alert = Alert(
        rule_id="anchor_drag",
        severity=Severity.ALARM,
        message="Dragging: 62 m from the anchor, watch circle 40 m",
        since=NOW - timedelta(minutes=3),
    )
    sc, out = screen()
    state = BoatState(clock=clock)
    C.render(
        U.build_payload(state, derived(), [alert], None, None, None, config, now=NOW), sc, now=NOW
    )

    text = out.getvalue()
    assert "ALARM" in text
    assert "Dragging" in text
    assert text.index("ALARM") < text.index("Anchor")  # above everything else


def test_the_menu_offers_the_things_you_actually_do_and_fits_a_narrow_window() -> None:
    sc, out = screen()
    C.render_menu(sc)

    text = out.getvalue()
    for key in ("[a]", "[u]", "[h]", "[s]", "[w]", "[q]", "[z]"):
        assert key in text
    # Eighty columns is the width of a laptop half-buried under a chart.
    assert all(len(line) <= 80 for line in text.splitlines())


def test_the_silence_key_says_which_way_it_will_move() -> None:
    """A key that toggles blind is a key that turns the alarms off by accident."""
    sc, out = screen()
    C.render_menu(sc, silenced=False)
    assert "silence every alert" in out.getvalue()

    sc, out = screen()
    C.render_menu(sc, silenced=True)
    assert "TURN THE ALARMS BACK ON" in out.getvalue()


def test_a_silenced_boat_says_so_above_everything_else(config, clock) -> None:
    """The screen is otherwise identical to a calm one, which is the danger."""
    from agent.silence import silence_now

    sc, out = screen()
    C.render(
        payload(config, clock), sc, now=NOW, silence=silence_now("laid up", now=NOW)
    )

    text = out.getvalue()
    assert "SILENCED" in text
    assert "laid up" in text
    assert text.index("SILENCED") < text.index("Anchor")
    assert "silenced, along with Signal and this screen" in text


def test_it_says_so_even_when_the_agent_is_not_running(config) -> None:
    from agent.silence import silence_now

    sc, out = screen()
    C.render(None, sc, silence=silence_now(now=NOW))

    text = out.getvalue()
    assert "SILENCED" in text
    assert "not running" in text


# ------------------------------------------------------------------ letters --


def nowait(_seconds: float) -> None:
    """The settle wait, with the waiting taken out."""


def scripted(*answers: str):
    """An `ask` that replies with each answer in turn, then stops."""
    replies = list(answers)

    def ask(_prompt: str) -> str:
        if not replies:
            raise EOFError
        return replies.pop(0)

    return ask


def test_a_arms_the_watch_and_writes_the_same_file_the_cli_writes(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock, fix=[36.83, 10.30]))
    sc, out = screen()

    assert C.run_console(config, sleep=nowait, ask=scripted("a", "55", "q"), screen=sc) == 0

    fix = AnchorFile(config.anchor_file).read()
    assert fix is not None
    assert fix.radius_m == 55.0
    assert "watch set" in out.getvalue()


def test_a_bare_return_at_the_radius_takes_the_default(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock, fix=[36.83, 10.30]))
    sc, _ = screen()

    C.run_console(config, sleep=nowait, ask=scripted("a", "", "q"), screen=sc)
    assert AnchorFile(config.anchor_file).read().radius_m == 35.0


def test_arming_with_no_fix_is_refused_rather_than_guessed(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock))  # no fix in it
    sc, out = screen()

    C.run_console(config, sleep=nowait, ask=scripted("a", "q"), screen=sc)
    assert "no position" in out.getvalue()
    assert not config.anchor_file.exists()


def test_u_clears_the_watch(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock, fix=[36.83, 10.30]))
    sc, _ = screen()

    C.run_console(config, sleep=nowait, ask=scripted("a", "40", "u", "q"), screen=sc)
    assert AnchorFile(config.anchor_file).read() is None


def test_h_and_s_hush_the_speaker_and_let_it_sound_again(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock))
    sc, out = screen()

    C.run_console(config, sleep=nowait, ask=scripted("h", "s", "q"), screen=sc)
    text = out.getvalue()
    assert "quiet until" in text
    assert "may sound again" in text


def test_z_silences_everything_but_only_after_a_confirmation(config, clock) -> None:
    from agent.silence import SilenceFile

    U.write_status(config.status_path, payload(config, clock))
    sc, out = screen()

    C.run_console(config, sleep=nowait, ask=scripted("z", "y", "q"), screen=sc)

    assert SilenceFile(config.silence_file).active() is not None
    text = out.getvalue()
    assert "This turns off the speaker, Signal AND this screen." in text
    assert "does not expire" in text


def test_declining_the_confirmation_leaves_the_alarms_on(config, clock) -> None:
    from agent.silence import SilenceFile

    U.write_status(config.status_path, payload(config, clock))
    sc, out = screen()

    C.run_console(config, sleep=nowait, ask=scripted("z", "n", "q"), screen=sc)

    assert SilenceFile(config.silence_file).active() is None
    assert "still on" in out.getvalue()


def test_z_again_turns_them_back_on_without_asking(config, clock) -> None:
    """That direction can only make her louder, so it never needs confirming."""
    from agent.silence import SilenceFile, silence_now

    SilenceFile(config.silence_file).write(silence_now(now=NOW))
    U.write_status(config.status_path, payload(config, clock))
    sc, out = screen()

    C.run_console(config, sleep=nowait, ask=scripted("z", "q"), screen=sc)

    assert SilenceFile(config.silence_file).active() is None
    assert "back on" in out.getvalue()


def test_a_letter_that_means_nothing_is_ignored_not_punished(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock))
    sc, _ = screen()

    assert C.run_console(config, sleep=nowait, ask=scripted("z", "?", "q"), screen=sc) == 0


def test_ctrl_d_leaves_quietly(config, clock) -> None:
    U.write_status(config.status_path, payload(config, clock))
    sc, _ = screen()

    assert C.run_console(config, sleep=nowait, ask=scripted(), screen=sc) == 0


# ------------------------------------------------------------------- status --


def test_the_status_file_survives_being_read_mid_write(config, clock) -> None:
    """Written beside and renamed over, so a reader never sees half of it."""
    U.write_status(config.status_path, payload(config, clock))
    assert U.read_status(config.status_path) is not None
    assert not config.status_path.with_suffix(".tmp").exists()


def test_rubbish_in_the_status_file_reads_as_nothing(config) -> None:
    config.status_path.parent.mkdir(parents=True, exist_ok=True)
    config.status_path.write_text("{not json", encoding="utf-8")

    assert U.read_status(config.status_path) is None


# ---------------------------------------------------------------------- gps --


def gps_payload(config, clock, position=None, age_s=0.0):
    state = BoatState(clock=clock)
    if position is not None:
        push(state, {"navigation.position": position})
        clock.advance(age_s)
    return U.build_payload(state, derived(), [], None, None, None, config, now=NOW)


def test_a_real_fix_is_shown_on_its_own_line(config, clock) -> None:
    sc, out = screen()
    C.render(
        gps_payload(config, clock, {"latitude": 36.8312, "longitude": 10.3034}), sc, now=NOW
    )

    text = out.getvalue()
    assert "GPS" in text
    # Degrees and decimal minutes, the same format the plot readout and the
    # text replies use. One number, one way of writing it.
    assert "36\u00b0 49.872\u2032 N" in text


def test_a_receiver_that_never_reported_says_so(config, clock) -> None:
    sc, out = screen()
    C.render(gps_payload(config, clock), sc, now=NOW)

    assert "no GPS position: nothing has ever reported one" in out.getvalue()


def test_a_receiver_reporting_with_no_fix_reads_differently(config, clock) -> None:
    """Null Island is thrown away by as_position, so this is the live case."""
    sc, out = screen()
    C.render(gps_payload(config, clock, {"latitude": 0.0, "longitude": 0.0}), sc, now=NOW)

    assert "the receiver is reporting, but has no fix" in out.getvalue()


def test_a_receiver_that_has_gone_quiet_says_how_long(config, clock) -> None:
    """A cable out and a view of the sky are different faults with different fixes."""
    sc, out = screen()
    payload = gps_payload(
        config, clock, {"latitude": 36.8312, "longitude": 10.3034}, age_s=900.0
    )
    C.render(payload, sc, now=NOW)

    assert "stopped reporting 15 min ago" in out.getvalue()


def test_a_stale_fix_is_not_a_position_anywhere(config, clock) -> None:
    """The one that matters. A boat that dragged half a mile fifteen minutes
    ago would otherwise sit quietly on a fix from before it moved."""
    state = BoatState(clock=clock)
    push(state, {"navigation.position": {"latitude": 36.8312, "longitude": 10.3034}})
    clock.advance(900)
    built = U.build_payload(state, derived(), [], None, None, None, config, now=NOW)

    assert built["gps"]["fix"] is False
    assert built["position"] is None
    # And nothing can arm a new watch on it, because there is nothing to arm on.
    assert built["fix"] is None


def test_a_distance_reads_at_a_glance_at_every_scale(config, clock) -> None:
    """Seen live: an anchor left set on the boat while the laptop was on another continent
    rendered as '8123456 m off', which looks like line noise rather than a
    number. Metres inside the anchor watch's own range, kilometres past it."""
    assert U._distance(0) == "0 m"
    assert U._distance(62) == "62 m"
    assert U._distance(999) == "999 m"
    assert U._distance(1500) == "1.5 km"
    assert U._distance(8_123_456) == "8123 km"


def test_the_anchor_line_uses_it(config, clock) -> None:
    from agent.anchor import AnchorFix

    state = BoatState(clock=clock)
    push(state, {"navigation.position": {"latitude": 36.8315, "longitude": 10.3034}})
    fix = AnchorFix(latitude=36.8312, longitude=10.3034, radius_m=40, set_at=NOW)

    sc, out = screen()
    C.render(
        U.build_payload(state, derived(), [], fix, None, None, config, now=NOW), sc, now=NOW
    )
    assert "m off" in out.getvalue()


def test_after_a_tap_it_waits_for_the_agent_before_redrawing(config, clock) -> None:
    """Seen live: "watch set, 40 m circle" and then "no watch set" one line
    below it, because the screen redrew from the payload written before the
    tap. It reads as a failure, and the action had worked."""
    U.write_status(config.status_path, payload(config, clock, fix=[36.83, 10.30]))
    ticks = []

    def agent_ticks(_seconds: float) -> None:
        # Stand in for the agent noticing the file and republishing.
        ticks.append(1)
        if len(ticks) == 2:
            later = payload(config, clock, fix=[36.83, 10.30])
            later["generated_at"] = "2026-09-08T19:31:00+00:00"
            U.write_status(config.status_path, later)

    sc, _ = screen()
    C.run_console(config, sleep=agent_ticks, ask=scripted("a", "40", "q"), screen=sc)
    assert len(ticks) == 2  # waited, then stopped as soon as the agent spoke


def test_the_wait_gives_up_rather_than_hanging_if_the_agent_is_gone(config, clock) -> None:
    """An agent that is not running never republishes, and the console must
    still come back to the prompt rather than sitting there forever."""
    U.write_status(config.status_path, payload(config, clock, fix=[36.83, 10.30]))
    polls = []
    sc, _ = screen()

    C.run_console(
        config, sleep=lambda s: polls.append(s), ask=scripted("u", "q"), screen=sc
    )
    assert polls  # it did wait
    assert len(polls) == int(C.SETTLE_SECONDS / C.SETTLE_POLL)  # and then stopped


# ------------------------------------------------------------------ restart --


def test_restarting_only_ever_touches_the_user_unit() -> None:
    """A system unit would prompt for a password through polkit, which in a
    console means a hang or a second confusing prompt. The user unit exists
    precisely so neither happens."""
    seen = []

    def run(args):
        seen.append(args)
        return True, ""

    ok, message = C.restart_agent(run=run)
    assert ok
    assert seen == [["--user", "restart", "boat-agent.service"]]
    assert "restarted" in message


def test_no_user_service_says_how_to_get_one() -> None:
    ok, message = C.restart_agent(run=lambda _a: (False, "Unit boat-agent.service not loaded."))
    assert not ok
    assert "install-command.sh" in message


def test_any_other_failure_is_reported_rather_than_swallowed() -> None:
    ok, message = C.restart_agent(run=lambda _a: (False, "Job failed. See journal."))
    assert not ok
    assert "Job failed" in message


def test_a_machine_without_systemctl_says_so_rather_than_trying(monkeypatch) -> None:
    """The Pi has systemctl and a Mac does not, so the real runner has to say
    which it is. The check sits with that runner and not with the argument,
    which is why the three tests above can run anywhere."""
    monkeypatch.setattr(C.shutil, "which", lambda _name: None)

    ok, message = C.restart_agent()
    assert not ok
    assert "nothing to restart" in message


def test_x_is_on_the_menu(config) -> None:
    sc, out = screen()
    C.render_menu(sc)
    assert "[x]" in out.getvalue()


def test_the_not_running_screen_does_not_ask_for_sudo(config) -> None:
    sc, out = screen()
    C.render(None, sc)

    text = out.getvalue()
    assert "sudo" not in text
    assert "systemctl --user start boat-agent" in text


def test_the_anchor_file_records_which_doorway_it_came_through(config, clock) -> None:
    """The file is read by a person as often as by the agent, and "set from the
    phone" is a lie when somebody typed it at the nav station."""
    U.write_status(config.status_path, payload(config, clock, fix=[36.83, 10.30]))
    sc, _ = screen()

    C.run_console(config, sleep=nowait, ask=scripted("a", "40", "q"), screen=sc)
    assert AnchorFile(config.anchor_file).read().note == "set from the console"

"""Asking the boat a question.

The risk here is entirely in what gets listened to. A boat that answers
questions from strangers is a boat that tells strangers it is empty, and a
boat that answers its own messages is a boat in a loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import inbox
from agent import ui as U
from agent.config import Config
from agent.derived import Confidence, Derived, VesselState
from agent.rules import Alert, Severity
from agent.state import BoatState

ACCOUNT = "+15555550100"
GROUP = "00adeqLvDIDxu59ltfKAxXk1DgOAj0sqfpD3xMXVgLQ="
CREW = "+15555550101"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        signal_account=ACCOUNT,
        signal_group=GROUP,
        status_path=tmp_path / "status.json",
        hush_file=tmp_path / "hush.json",
        silence_file=tmp_path / "silence.json",
        anchor_file=tmp_path / "anchor.json",
        log_dir=tmp_path / "logs",
    )


def envelope(text: str, source: str = CREW, group: str | None = GROUP, **over) -> str:
    message: dict = {"message": text, "timestamp": 1}
    if group is not None:
        message["groupInfo"] = {"groupId": group, "type": "DELIVER"}
    env: dict = {"sourceNumber": source, "sourceName": "Sam", "dataMessage": message}
    env.update(over)
    return json.dumps({"envelope": env, "account": ACCOUNT})


# ------------------------------------------------------------- who is heard --


def test_a_question_in_the_crew_group_is_heard() -> None:
    assert inbox.questions(envelope("status"), GROUP, ACCOUNT) == [("Sam", "status")]


def test_a_stranger_in_another_group_is_ignored() -> None:
    """Not answered, not even to refuse: a boat that replies to strangers has
    told them somebody is listening."""
    assert inbox.questions(envelope("status", group="g-someone-else="), GROUP, ACCOUNT) == []


def test_a_direct_message_is_ignored_when_a_group_is_configured() -> None:
    assert inbox.questions(envelope("status", group=None), GROUP, ACCOUNT) == []


def sync(text: str, group: str | None = GROUP) -> str:
    """A message the OWNER sent, in the shape signal-cli really produces.

    Copied from an envelope captured off the wire, not invented: the first
    version of this file guessed, and the guess was what stopped the skipper
    being heard at all.
    """
    sent: dict = {
        "destination": None, "destinationNumber": None, "destinationUuid": None,
        "timestamp": 1767225600000, "message": text, "expiresInSeconds": 0,
        "isExpirationUpdate": False, "viewOnce": False,
    }
    if group is not None:
        sent["groupInfo"] = {"groupId": group, "groupName": "Seabird",
                             "revision": 0, "type": "DELIVER"}
    return json.dumps({"envelope": {
        "source": ACCOUNT, "sourceNumber": ACCOUNT, "sourceName": "Alex",
        "sourceUuid": "7f052077-6ed7-4a09-a039-dfe41b34422b", "sourceDevice": 1,
        "timestamp": 1767225600000, "syncMessage": {"sentMessage": sent},
    }, "account": ACCOUNT})


def test_the_owner_is_heard_even_though_they_arrive_as_sync_messages() -> None:
    """signal-cli is a LINKED device, so anything the phone sends reaches it as
    a sync of what the primary sent, never as incoming mail. Dropping syncs
    drops the skipper and nobody else, which is exactly backwards."""
    assert inbox.questions(sync("status"), GROUP, ACCOUNT) == [("Alex", "status")]


def test_the_boat_does_not_answer_its_own_replies() -> None:
    """Replies go out through the same account. Answering one would be a loop
    that runs until Signal rate-limits it. A sync only counts as a question
    when it opens with a word this understands, and no reply does."""
    reply = "Seabird, 09:12 EEST\nNo alarms.\nStopped, no watch set."
    assert inbox.questions(sync(reply), GROUP, ACCOUNT) == []
    assert inbox.questions(sync("Next 18 h: up to 30 kn"), GROUP, ACCOUNT) == []


def test_the_owner_asking_in_another_group_is_still_ignored() -> None:
    assert inbox.questions(sync("status", group="g-elsewhere="), GROUP, ACCOUNT) == []
    assert inbox.questions(sync("status", group=None), GROUP, ACCOUNT) == []


def test_a_receipt_from_the_crew_is_not_a_question() -> None:
    """Sam reading a message must not look like Sam asking one."""
    receipt = json.dumps({"envelope": {
        "source": CREW, "sourceNumber": CREW, "sourceName": "sam",
        "receiptMessage": {"when": 1767225600000, "isDelivery": True},
    }})
    assert inbox.questions(receipt, GROUP, ACCOUNT) == []


def test_receipts_and_reactions_are_not_questions() -> None:
    for junk in (
        json.dumps({"envelope": {"sourceNumber": CREW, "receiptMessage": {"when": 1}}}),
        json.dumps({"envelope": {"sourceNumber": CREW, "typingMessage": {"action": "STARTED"}}}),
        envelope(""),
        envelope("   "),
    ):
        assert inbox.questions(junk, GROUP, ACCOUNT) == []


def test_one_unreadable_line_does_not_lose_the_rest() -> None:
    raw = "not json at all\n" + envelope("status") + "\n{oops\n" + envelope("weather")
    assert [q for _, q in inbox.questions(raw, GROUP, ACCOUNT)] == ["status", "weather"]


# ------------------------------------------------------------ what it says --


def payload(config: Config, clock, **over) -> None:
    state = BoatState(clock=clock)
    derived = Derived(vessel=VesselState.ANCHORED, confidence=Confidence.LIKELY,
                      reason="stopped inside the circle")
    built = U.build_payload(state, derived, [], None, None, None, config, now=clock.now)
    built.update(over)
    U.write_status(config.status_path, built)


def test_status_answers_from_the_same_file_the_console_reads(config, clock) -> None:
    payload(config, clock)
    said = inbox.answer("status", config)
    assert "Seabird" in said
    assert "No alarms." in said


def test_an_alarm_is_the_first_thing_in_the_reply(config, clock) -> None:
    state = BoatState(clock=clock)
    alert = Alert(rule_id="anchor_drag", severity=Severity.ALARM,
                  message="Dragging: 62 m from the anchor", since=clock.now)
    derived = Derived(vessel=VesselState.ANCHORED, confidence=Confidence.LIKELY, reason="x")
    U.write_status(
        config.status_path,
        U.build_payload(state, derived, [alert], None, None, None, config, now=clock.now),
    )
    said = inbox.answer("status", config)
    assert said.splitlines()[1].startswith("ALARM: Dragging")


def test_with_no_agent_running_it_says_so_rather_than_nothing(config) -> None:
    said = inbox.answer("status", config)
    assert "not running" in said
    assert "systemctl --user start" in said


def test_it_refuses_to_touch_the_anchor(config, clock) -> None:
    """Arming a watch by text means arming it at a position nobody has checked,
    and clearing one means disabling an alarm from a bar."""
    payload(config, clock)
    for asked in ("anchor", "anchor down 40", "anchor up"):
        said = inbox.answer(asked, config)
        assert "will not set one by text" in said or "no anchor watch" in said.lower()
    assert not config.anchor_file.exists()


def test_hush_is_allowed_because_it_expires(config, clock) -> None:
    payload(config, clock)
    said = inbox.answer("hush 20", config)
    assert "quiet until" in said and "speaker only" in said

    assert "did not understand" not in inbox.answer("sound", config)


def test_a_hush_that_is_not_a_number_asks_again(config, clock) -> None:
    payload(config, clock)
    assert "Hush how long" in inbox.answer("hush soon", config)


def test_it_will_not_silence_itself_by_text(config, clock) -> None:
    """That switch does not expire, so it is only thrown where you can see
    what you are turning off."""
    from agent.silence import SilenceFile

    payload(config, clock)
    for asked in ("silence", "mute", "standby"):
        said = inbox.answer(asked, config)
        assert "will not silence myself by text" in said
    assert SilenceFile(config.silence_file).active() is None


def test_it_will_turn_the_alarms_back_on_from_anywhere(config, clock) -> None:
    """The only direction that can make her louder, so the only one allowed."""
    from agent.silence import SilenceFile, silence_now

    SilenceFile(config.silence_file).write(silence_now(now=clock.now))
    payload(config, clock)

    said = inbox.answer("alarms on", config)
    assert "back on" in said
    assert SilenceFile(config.silence_file).active() is None


def test_a_silence_leads_the_status_reply(config, clock) -> None:
    """Somebody asking after her from ashore has to be told first."""
    from agent.silence import silence_now

    payload(config, clock, silence={"text": "SILENCED since 21:40 UTC, 6 h ago"})
    said = inbox.answer("status", config)
    assert "SILENCED" in said.splitlines()[1]
    assert "alarms on" in said
    assert silence_now  # the helper the agent uses to write that payload


def test_the_refusals_are_still_recognised_as_questions() -> None:
    """The skipper's own 'silence' arrives as a sync message, and a word this
    does not know would be dropped rather than refused."""
    for word in ("silence", "mute", "off", "standby", "alarms", "on"):
        assert word in inbox.COMMANDS


def test_anything_it_does_not_know_lists_what_it_does(config, clock) -> None:
    payload(config, clock)
    said = inbox.answer("open the pod bay doors", config)
    assert "did not understand" in said
    assert "status" in said


def test_a_reply_is_trimmed_to_something_a_phone_can_show(config) -> None:
    assert len(inbox._trim("x" * 5000)) <= inbox.MAX_REPLY


# --------------------------------------------------------------- the fetch --


def test_a_failed_receive_raises_rather_than_returning_junk(config) -> None:
    def broken(_command):
        raise OSError("signal-cli is not installed")

    with pytest.raises(inbox.InboxError):
        inbox.receive(config, run=broken)


def test_the_command_asks_the_right_account_in_json(config) -> None:
    seen: list[list[str]] = []
    inbox.receive(config, run=lambda cmd: seen.append(cmd) or "")
    assert "--output=json" in seen[0]
    assert ACCOUNT in seen[0]
    assert "receive" in seen[0]


# ---------------------------------------------------------------- battery --


def mppt(config: Config, clock, **paths) -> None:
    """A status file with the MPPT reporting whatever is given."""
    state = BoatState(clock=clock)
    if paths:
        state.apply_delta({"updates": [{"$source": "victron.ble", "values": [
            {"path": f"electrical.solar.mppt.{k}", "value": v} for k, v in paths.items()
        ]}]})
    derived = Derived(vessel=VesselState.ANCHORED, confidence=Confidence.LIKELY, reason="x")
    U.write_status(
        config.status_path,
        U.build_payload(state, derived, [], None, None, None, config, now=clock.now),
    )


def test_the_bank_is_always_reported_even_with_nothing_to_report(config, clock) -> None:
    """A status message with no battery line reads as "the bank is fine" and
    means "I have no idea". That is the wrong way round for the one number you
    cannot see from ashore."""
    mppt(config, clock)
    said = inbox.answer("status", config)
    assert "no reading from the MPPT" in said
    assert "Bluetooth" in said  # says where to go looking


def test_a_charging_bank_says_what_is_going_into_it(config, clock) -> None:
    mppt(config, clock, voltage=13.82, current=18.4, panelPower=505.0, yieldToday=4_680_000.0)
    said = inbox.answer("status", config)

    assert "Bank 13.82 V" in said
    assert "charging at 18.4 A, 505 W from the panels" in said
    assert "1300 Wh today" in said


def test_a_bank_at_night_says_it_is_not_charging(config, clock) -> None:
    mppt(config, clock, voltage=13.05, current=0.0, panelPower=0.0)
    said = inbox.answer("status", config)
    assert "Bank 13.05 V" in said
    assert "not charging" in said


def test_the_reading_never_pretends_to_be_a_state_of_charge(config, clock) -> None:
    """The only voltage available is the MPPT's own battery-side reading:
    charger output while the sun is up, absent at night. A number that looked
    authoritative would be worse than one that says what it is."""
    mppt(config, clock, voltage=13.05)
    assert "(MPPT, rough)" in inbox.answer("status", config)
    assert "SOC" not in inbox.answer("status", config)


def test_a_trickle_is_not_charging(config, clock) -> None:
    """A tenth of an amp is noise, not a charge."""
    mppt(config, clock, voltage=12.9, current=0.2, panelPower=1.0)
    assert "not charging" in inbox.answer("status", config)

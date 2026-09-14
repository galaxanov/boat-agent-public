"""Asking the boat a question, and getting an answer.

Everything else here is one-way: the boat decides something is worth saying and
says it. This is the other direction. You are ashore having dinner, the wind
has got up, and you would like to know whether she is still where you left her.
Text the crew group and she answers.

That is the on-demand question answering the design notes always wanted, except
over the channel already in your pocket rather than a terminal you would have
to be sitting at. The whole point is that it works from anywhere, and the
terminal does not.

Four decisions worth writing down.

**It answers from the published status file**, the same one `--console` reads,
rather than reaching into the running state model. One picture of the boat, one
place it is built, and the text you get back cannot disagree with the screen. It
also means this can never slow a rule down: it reads a file.

**Polling, not a daemon.** signal-cli takes about four seconds to start a JVM,
so an answer takes up to a minute rather than arriving instantly. That is the
right trade on a boat: a daemon is another process to supervise, to restart, and
to discover has been dead since Tuesday. A minute is fine for "how is she doing";
nothing here is an alarm, and the alarms do not come this way.

Polling also fixes something we were not doing. signal-cli warns that the Signal
protocol expects incoming messages to be received regularly, and until now this
account only ever sent. Receiving on a timer keeps the session healthy, which
the sending side quietly depends on.

**It only listens to the crew.** Messages from anywhere but the configured group
are ignored without reply, and its own sent messages, which come back as sync
messages because this is a linked device, are dropped. A boat that answers
questions from strangers is a boat that tells strangers it is empty.

**It will not weigh an anchor.** Reading is safe from anywhere. Arming a watch by
text means arming it at a position nobody has verified, and clearing one by text
means disabling an alarm from a bar. Hushing is allowed because a hush always
expires and only silences the speaker. Everything else is a question.

The same rule decides what this does about a silence, and it decides it both
ways. It will NOT silence the boat by text: that switch does not expire, and
the one place it must never be thrown from is somewhere the person cannot see
what they are turning off. It WILL turn the alarms back on, because that
direction can only ever make the boat louder, and because somebody who realises
over dinner that they left her silenced should be able to fix it from where
they are. Every status reply says so while a silence is standing.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from typing import Any

from .ui import Actions, read_status

log = logging.getLogger(__name__)

# How long signal-cli waits on the server for something to arrive before giving
# up. Long-polling: it costs nothing while it waits, and it means a question
# asked just after a cycle started is not sitting for the whole interval.
RECEIVE_TIMEOUT = 8.0

# Beyond this the JVM has hung or the link is a disaster. Generous, because a
# cold start on a laptop that has been asleep is genuinely slow.
RUN_TIMEOUT = 120.0

# A phone is not a terminal. Anything longer than this gets trimmed rather than
# arriving as a wall of text on a lock screen.
MAX_REPLY = 1200

HELP = (
    "Ask me: status, weather, anchor, hush 30, sound, alarms on, help. "
    "I will not raise or weigh an anchor by text, and I will not silence "
    "myself by text - only turn the alarms back on."
)

# Every word this answers to. Used twice: to route a question, and to decide
# whether one of our OWN messages was meant as one.
COMMANDS = frozenset(
    {
        "status", "boat", "how", "where", "ok",
        "weather", "forecast", "wind",
        "anchor", "watch",
        "hush",
        "sound", "unhush", "unmute", "on", "alarms", "unsilence", "wake",
        "silence", "mute", "off", "standby",
        "help", "commands", "?",
    }
)  # fmt: skip


def verb(text: str) -> str:
    """The first word, as a command. Punctuation and case do not matter."""
    word, _, _ = text.strip().partition(" ")
    return word.lower().strip("?!.,")


class InboxError(Exception):
    """Reading the inbox failed. Always non-fatal."""


# ------------------------------------------------------------------ reading --


def _envelopes(raw: str) -> list[dict[str, Any]]:
    """One JSON object per line, and anything unreadable is skipped.

    signal-cli emits a line per envelope. A malformed one is not a reason to
    drop the rest, and it is certainly not a reason to stop listening.
    """
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            log.debug("skipping an unreadable envelope: %.80s", line)
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def questions(raw: str, group: str, account: str) -> list[tuple[str, str]]:
    """The messages worth answering, as (who asked, what they said).

    Two shapes arrive, and missing the second is the mistake that made the
    first version of this answer nobody at all.

    A message from ANOTHER PERSON is a dataMessage: ordinary incoming mail.

    A message from the OWNER of this account is a syncMessage. signal-cli is a
    linked secondary device, so when the phone sends something the linked
    devices are told what the primary sent - they never see it as incoming.
    Dropping sync messages therefore drops the skipper and nobody else, which
    is precisely backwards: the person most likely to ask the boat a question
    is the person who owns the number it answers from.

    They cannot be treated identically, because this account's own REPLIES go
    out the same way. If a reply came back as a sync and were answered, the
    boat would talk to itself until Signal rate-limited it. So a sync message
    counts as a question only when it opens with a word this understands, and
    no reply ever does.

    Everything else is dropped without a reply: receipts, typing indicators,
    reactions, and anything from outside the crew group. A boat that answers
    strangers is a boat that tells strangers it is empty.
    """
    found: list[tuple[str, str]] = []
    for item in _envelopes(raw):
        envelope = item.get("envelope")
        if not isinstance(envelope, dict):
            continue

        source = str(envelope.get("sourceNumber") or envelope.get("source") or "")
        who = str(envelope.get("sourceName") or source or "someone")

        message = envelope.get("dataMessage")
        own = False
        if not isinstance(message, dict):
            sync = envelope.get("syncMessage")
            sent = sync.get("sentMessage") if isinstance(sync, dict) else None
            if not isinstance(sent, dict):
                continue
            message, own = sent, True

        text = message.get("message")
        if not isinstance(text, str) or not text.strip():
            continue

        # Our own traffic only counts when it was plainly meant as a question.
        if own and verb(text) not in COMMANDS:
            continue

        info = message.get("groupInfo")
        asked_in = info.get("groupId") if isinstance(info, dict) else None
        if group:
            if asked_in != group:
                continue  # a different group, or a direct message
        elif asked_in is not None:
            continue  # no group configured: only direct messages

        found.append((who, text.strip()))
    return found


def receive(config: Any, run: Any = None) -> str:
    """Fetch whatever is waiting. Blocking: call it with asyncio.to_thread."""
    command = [
        *shlex.split(config.signal_cli_path),
        "-a",
        config.signal_account,
        "--output=json",
        "receive",
        "-t",
        str(RECEIVE_TIMEOUT),
    ]
    runner = run or _run
    # The guard belongs here rather than only inside _run, because the seam is
    # the point: whatever is doing the running, this function's contract is
    # that it raises InboxError and nothing else.
    try:
        return runner(command)
    except InboxError:
        raise
    except Exception as exc:
        raise InboxError(str(exc)) from exc


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InboxError(str(exc)) from exc
    if done.returncode != 0:
        raise InboxError((done.stderr or done.stdout or "").strip()[:200])
    return done.stdout


# ----------------------------------------------------------------- answering --


def answer(text: str, config: Any) -> str:
    """What to say back. Never raises, and never returns nothing."""
    _, _, rest = text.strip().partition(" ")
    word = verb(text)

    if word in ("status", "boat", "how", "where", "ok"):
        return _status(config)
    if word in ("weather", "forecast", "wind"):
        return _weather(config)
    if word in ("anchor", "watch"):
        return _anchor(config)
    if word == "hush":
        return _hush(config, rest)
    if word in ("sound", "unhush", "unmute", "on", "alarms", "unsilence", "wake"):
        return _alarms_on(config)
    if word in ("silence", "mute", "off", "standby"):
        return (
            "I will not silence myself by text. That switch turns off the "
            "speaker, Signal and the screen together and it does not expire, "
            "so it is only thrown from aboard where you can see what you are "
            "turning off. Use the console: boat, then z."
        )
    if word in ("help", "commands", "?"):
        return HELP
    return f"I did not understand {word!r}. {HELP}"


def _payload(config: Any) -> dict[str, Any] | None:
    return read_status(config.status_path)


def _status(config: Any) -> str:
    """The whole boat in the space of a lock screen."""
    s = _payload(config)
    if s is None:
        return (
            "The agent is not running, so nothing is watching the boat. "
            "Start it: systemctl --user start boat-agent"
        )

    lines = [f"{s.get('boat', 'Seabird')}, {s.get('clock', '')}".strip()]

    # First line under the name, before the alarms, because it is the reason
    # this message is the only way anybody is hearing about them.
    held = s.get("silence")
    if isinstance(held, dict):
        lines.append(
            f"{held.get('text', 'SILENCED')}. Say 'alarms on' and I will turn "
            "them back on from here."
        )

    alerts = s.get("alerts") or []
    for alert in alerts[:3]:
        lines.append(f"{str(alert.get('severity', '')).upper()}: {alert.get('message', '')}")
    if not alerts:
        lines.append("No alarms.")

    anchor = s.get("anchor") or {}
    if anchor.get("set"):
        far = anchor.get("distance") or "no fix"
        lines.append(
            f"Anchored, {far} off a {anchor.get('radius_m')} m circle "
            f"set {anchor.get('set_at')}."
        )
        if anchor.get("furthest"):
            lines.append(f"Furthest {anchor['furthest']} in {anchor.get('watching', '')}.".strip())
    else:
        lines.append(f"{str(s.get('state', 'unknown')).capitalize()}, no watch set.")

    gps = s.get("gps") or {}
    lines.append(gps.get("text") or "no GPS position")

    readings = {r["label"]: r for r in s.get("readings") or []}
    said = [
        f"{label} {readings[label]['value']} {readings[label]['unit']}"
        for label in ("Depth", "Wind")
        if readings.get(label, {}).get("value") is not None
    ]
    if said:
        lines.append(", ".join(said) + ".")

    # Always, even when there is nothing to report. A status message with no
    # battery line reads as "the bank is fine" and means "I have no idea",
    # and that is the wrong way round for the one number you cannot see from
    # ashore.
    battery = s.get("battery") or {}
    lines.append(battery.get("text") or "No battery reading.")

    forecast = s.get("forecast") or {}
    if forecast.get("hours"):
        lines.append(f"Next {forecast['hours']:.0f} h: {forecast.get('summary', '')}")

    return _trim("\n".join(line for line in lines if line))


def _weather(config: Any) -> str:
    s = _payload(config)
    if s is None:
        return "The agent is not running, so it has no forecast."
    f = s.get("forecast") or {}
    if not f.get("hours"):
        return f.get("summary") or "No forecast."

    lines = [f"Next {f['hours']:.0f} h: {f.get('summary', '')}"]
    sea = f.get("sea")
    if sea:
        steep = ", short and steep" if sea.get("steep") else ""
        period = f" at {sea['period_s']} s" if sea.get("period_s") else ""
        lines.append(f"Sea to {sea.get('height_m')} m{period}{steep}, worst {sea.get('at')}.")
    if f.get("dark_for"):
        lines.append(f"Dark for another {f['dark_for']} h.")
    if f.get("now"):
        lines.append(f"Now {f['now']}, fetched {f.get('age_min')} min ago.")
    return _trim("\n".join(lines))


def _anchor(config: Any) -> str:
    s = _payload(config)
    anchor = (s or {}).get("anchor") or {}
    if not anchor.get("set"):
        return (
            "No anchor watch is set. I will not set one by text: it would be "
            "armed at a position nobody has checked. Use the console aboard."
        )
    far = anchor.get("distance") or "no fix, so the distance is unknown"
    return _trim(
        f"Watch set {anchor.get('set_at')}, {anchor.get('radius_m')} m circle, "
        f"{far} off, bearing {anchor.get('bearing_deg')}. "
        f"Furthest {anchor.get('furthest') or 'not yet measured'} "
        f"in {anchor.get('watching') or 'no time at all'}."
    )


def _hush(config: Any, rest: str) -> str:
    """The one thing this will change, because a hush always expires."""
    minutes = 30.0
    if rest.strip():
        try:
            minutes = float(rest.split()[0])
        except ValueError:
            return "Hush how long? Say 'hush 30' for half an hour."
    return Actions(config).hush_for(minutes)[1] + " (the speaker only)"


def _alarms_on(config: Any) -> str:
    """End a hush and a silence together. The only direction that is safe.

    "sound" used to mean unhush and nothing else. It now means everything the
    boat has for reaching a person, because a crew member who texts asking for
    the alarms back wants all of them and should not have to know there were
    two switches.
    """
    actions = Actions(config)
    said = [actions.unsilence()[1], actions.unhush()[1]]
    return ". ".join(part for part in said if part).capitalize() + "."


def _trim(text: str) -> str:
    if len(text) <= MAX_REPLY:
        return text
    return text[: MAX_REPLY - 1].rsplit("\n", 1)[0] + "…"

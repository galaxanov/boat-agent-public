"""Agent entry point.

Connects to Signal K, keeps a state model, evaluates the alert rules and writes
a daily log, sends alerts over Signal, puts them on this machine's screen and
sounds a local alarm here for the ones that matter. Fetches a forecast for
wherever the boat is, and writes the daily entry in the ship's log, in plain
English where there is an API key for it.

    python -m agent.main               # normal
    python -m agent.main --discover    # log every path the bus publishes
    python -m agent.main --once        # connect, snapshot once, exit
    python -m agent.main --console     # one screen, and the four things you do
    python -m agent.main --forecast    # print the forecast for here, exit
    python -m agent.main --drill       # test every alarm channel, exit
    python -m agent.main --silence     # every alert channel off until --unsilence
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .access import check_read_access, request_token
from .anchor import DEFAULT_RADIUS_M, AnchorFile, AnchorFix
from .config import REPO_ROOT, Config, load_env_file
from .console import run_console
from .derived import StateDeriver
from .desktop import DesktopNotifier, build_desktop_notifier
from .digest import build_digest, render_entry, update_file
from .geo import as_position
from .inbox import InboxError, answer, questions, receive
from .logbook import Logbook
from .notify import AlertNotifier, Outbox, build_notifier
from .rules import (
    FORECAST_GUST_WARN_MS,
    FORECAST_MAX_AGE,
    FORECAST_WIND_WARN_MS,
    Alert,
    AlertEvent,
    RuleEngine,
    Severity,
    build_default_rules,
)
from .signalk import SignalKClient
from .silence import Nag, SilenceFile, silence_now
from .sound import (
    DEFAULT_HUSH_MINUTES,
    HushFile,
    LocalAlarm,
    build_alarm_wav,
    build_local_alarm,
    hush_until,
)
from .state import BoatState
from .ui import Dashboard, Track, build_payload, build_ui, warn_if_open, write_status
from .units import (
    cardinal,
    dayhhmm,
    format_position,
    format_status,
    hhmm,
    knots,
    set_display_timezone,
    zone_name,
)
from .weather import (
    NO_POSITION,
    Forecast,
    Outlook,
    WeatherError,
    WeatherStore,
    fetch_forecast,
)

log = logging.getLogger("agent")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent", description="Boat agent")
    parser.add_argument("--host", help="Signal K host (default from SIGNALK_HOST)")
    parser.add_argument("--port", type=int, help="Signal K port")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="subscribe to every self path and report what turns up",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="take one snapshot after --settle seconds, then exit",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=10.0,
        help="seconds to collect data before the --once snapshot (default 10)",
    )
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--request-access",
        action="store_true",
        help="ask Signal K for a read token, wait for approval, and save it to .env",
    )
    parser.add_argument(
        "--anchor-down",
        action="store_true",
        help="tell the agent the hook is down here, and start the drag watch",
    )
    parser.add_argument(
        "--anchor-up",
        action="store_true",
        help="stop the drag watch: the anchor is aboard",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=DEFAULT_RADIUS_M,
        help=f"watch circle in metres for --anchor-down (default {DEFAULT_RADIUS_M:.0f})",
    )
    parser.add_argument(
        "--at",
        metavar="LAT,LON",
        help="where the anchor went down, if not the boat's current position",
    )
    parser.add_argument(
        "--ships-log",
        action="store_true",
        help="write today's ship's log entry from the daily log now, then exit",
    )
    parser.add_argument(
        "--test-alarm",
        action="store_true",
        help="make the alarm noise now and say what played it",
    )
    parser.add_argument(
        "--drill",
        action="store_true",
        help=(
            "send a real test alert down every channel and report what got "
            "through. Non-zero if a configured channel failed"
        ),
    )
    parser.add_argument(
        "--hush",
        nargs="?",
        type=float,
        const=DEFAULT_HUSH_MINUTES,
        metavar="MIN",
        help=(
            f"silence the local alarm for MIN minutes (default {DEFAULT_HUSH_MINUTES:.0f}). "
            "Signal alerts and the log carry on regardless"
        ),
    )
    parser.add_argument(
        "--unhush",
        action="store_true",
        help="end a hush early: the local alarm may sound again",
    )
    parser.add_argument(
        "--silence",
        nargs="?",
        const="",
        metavar="NOTE",
        help=(
            "turn every alert channel off - speaker, Signal and this machine's "
            "screen - until --unsilence. Does NOT expire. The boat keeps "
            "watching and keeps its log; it just tells nobody. NOTE says why, "
            "and is shown on every screen until the silence is lifted"
        ),
    )
    parser.add_argument(
        "--unsilence",
        action="store_true",
        help=(
            "turn every alert channel back on. Anything still standing is "
            "announced again at once"
        ),
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="show the boat and the things you can do about it, then wait for a letter",
    )
    parser.add_argument(
        "--forecast",
        action="store_true",
        help=(
            "fetch the forecast for the boat's position and print it, then exit. "
            "Use --at LAT,LON for somewhere else"
        ),
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    load_env_file()
    config = Config.from_env()
    overrides: dict[str, object] = {}
    if args.host:
        overrides["signalk_host"] = args.host
    if args.port:
        overrides["signalk_port"] = args.port
    if args.discover:
        overrides["discover"] = True
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    return replace(config, **overrides) if overrides else config


def setup_display_timezone(config: Config) -> None:
    """Pick the zone every time on a screen is shown in, once, at startup.

    Nothing stored changes: the daily log, the anchor file and every timestamp
    a rule reasons about stay UTC, because a boat crosses zones and a log in
    local time cannot be compared with itself. This is the conversion on the
    way out, and only that.
    """
    settled = set_display_timezone(config.timezone)
    if config.timezone and not settled:
        log.warning(
            "AGENT_TIMEZONE=%r is not a zone this machine knows, so times are "
            "shown in its own zone instead. Use an IANA name like Europe/Helsinki.",
            config.timezone,
        )
    log.info(
        "times on screen are %s; everything written down stays UTC", zone_name() or "local"
    )


def setup_logging(level: str) -> None:
    # No timestamps: journald adds its own, and duplicating them makes the log
    # harder to read. Running by hand you get them from journalctl anyway.
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)


def night_ahead(outlook: Outlook | None) -> tuple[str, str | None]:
    """What the boat is anchoring into, and whether it wants a second look.

    Returns the line to say, and what crosses the line the forecast rule uses,
    or None when nothing does. Naming which of the two crossed matters: a night
    of 14 knots gusting 35 is a different night from a steady 30, and telling
    the crew "F6 or more" when the sustained wind never gets near F6 is the
    kind of small inaccuracy that teaches people to discount the next one.

    A missing forecast gets a sentence of its own, because believing somebody
    checked the weather is worse than knowing nobody did.
    """
    if outlook is None:
        return ("no forecast to check tonight against", None)
    if outlook.empty:
        return (f"the forecast says nothing about the next {outlook.hours:.0f} h", None)

    windy = (outlook.wind_ms or 0.0) >= FORECAST_WIND_WARN_MS
    gusty = (outlook.gust_ms or 0.0) >= FORECAST_GUST_WARN_MS
    gust_kn = knots(FORECAST_GUST_WARN_MS) or 0.0
    reason = None
    if windy and gusty:
        reason = f"F6 or more sustained, with gusts over {gust_kn:.0f} kn"
    elif windy:
        reason = "F6 or more sustained"
    elif gusty:
        reason = f"gusts over {gust_kn:.0f} kn"

    when = ""
    if outlook.windiest is not None:
        when = f", peaking {hhmm(outlook.windiest.time)}"
    return (f"next {outlook.hours:.0f} h: {outlook.summary()}{when}", reason)


def note_the_night(
    logbook: Logbook, weather: WeatherStore | None, config: Config
) -> None:
    """Record what the weather was expected to do when the hook went down.

    Written whether the news is good or bad. An entry saying the night was
    forecast quiet is what makes the entry saying it was not worth reading, and
    a watch armed against no forecast at all is worth admitting to.

    This does not alert: the forecast rule does that, and does it again if the
    model changes its mind at midnight. This is the record of what was known at
    the moment the crew committed to the spot.
    """
    now = datetime.now(UTC)
    outlook: Outlook | None = None
    if weather is None:
        line, reason = "the forecast is turned off, so nothing has looked at tonight", None
    elif (missing := weather.explain(FORECAST_MAX_AGE, now)) is not None:
        # Three absences that look the same from outside: nothing tried yet,
        # nothing to try, and tried and failed. Only the last means nobody is
        # going to check tonight, and only the last should read that way.
        line, reason = missing, None
    else:
        forecast = weather.fresh(FORECAST_MAX_AGE, now)
        assert forecast is not None  # explain() returned None, so there is one
        outlook = forecast.outlook(config.anchor_outlook_hours, now)
        line, reason = night_ahead(outlook)
    # No preamble: this lands directly under the "anchor watch armed" line.
    log.log(logging.WARNING if reason else logging.INFO, "%s", line)
    logbook.write(
        {
            "type": "event",
            "event": "anchor_forecast",
            "summary": line,
            "over_threshold": reason,
            **(outlook.as_dict() if outlook is not None else {}),
        }
    )


def apply_anchor_file(
    anchor: AnchorFile,
    state: BoatState,
    logbook: Logbook,
    weather: WeatherStore | None = None,
    config: Config | None = None,
) -> None:
    """Feed a hand-set anchor into the state model, as the plugin would.

    Re-fed on every tick rather than once, so the reading never ages out from
    under a rule that checks staleness. Cheap: two values into a dict.
    """
    if anchor.changed():
        was = anchor.fix
        now = anchor.read()
        if now is not None and (was is None or now.set_at != was.set_at):
            log.info(
                "anchor watch armed at %.5f, %.5f with a %.0f m circle",
                now.latitude,
                now.longitude,
                now.radius_m,
            )
            logbook.write({"type": "event", "event": "anchor_set", **now.as_dict()})
            # The one moment when the answer is still cheap: more chain, a
            # different cove, or leave before dark.
            if config is not None:
                note_the_night(logbook, weather, config)
        elif now is None and was is not None:
            log.info("anchor watch cleared")
            logbook.write({"type": "event", "event": "anchor_weighed"})
            # The drag rule deliberately never expires the anchor, so clearing
            # the file is not enough on its own: the last position and radius
            # are still sitting in the state model and would keep the watch
            # armed around a hook that is now on the bow roller. Publishing a
            # zero radius is how the anchor plugin says "no circle", and the
            # rule reads it the same way whoever sent it.
            state.apply_delta(
                {
                    "updates": [
                        {
                            "$source": "anchor.file",
                            "values": [{"path": "navigation.anchor.maxRadius", "value": 0}],
                        }
                    ]
                }
            )

    if anchor.fix is not None:
        state.apply_delta(anchor.fix.as_delta())


async def snapshot_loop(
    state: BoatState, logbook: Logbook, deriver: StateDeriver, config: Config, stop: asyncio.Event
) -> None:
    """Write the state to the logbook, and a readable line to the journal."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.snapshot_interval)
            return
        except TimeoutError:
            pass

        derived = deriver.derive(state)
        snapshot = state.snapshot(stale_after=config.stale_after)
        logbook.write({"type": "snapshot", "derived": derived.as_dict(), **snapshot})
        log.info("[%s] %s", derived.vessel, format_status(state))

        if config.discover:
            log.info(
                "discover: %d paths seen, top: %s",
                len(state.path_counts),
                ", ".join(f"{p}({n})" for p, n in state.path_counts.most_common(8)),
            )


SEVERITY_LOG_LEVEL = {
    Severity.NORMAL: logging.INFO,
    Severity.ALERT: logging.WARNING,
    Severity.WARN: logging.WARNING,
    Severity.ALARM: logging.ERROR,
    Severity.EMERGENCY: logging.CRITICAL,
}


async def rules_loop(
    state: BoatState,
    logbook: Logbook,
    engine: RuleEngine,
    notifier: AlertNotifier,
    desktop: DesktopNotifier,
    alarm: LocalAlarm,
    deriver: StateDeriver,
    config: Config,
    stop: asyncio.Event,
    weather: WeatherStore | None = None,
    dashboard: Dashboard | None = None,
    track: Track | None = None,
) -> None:
    """Evaluate the rules, record what changed, and send it on.

    Only transitions go out - an alert that is still active says nothing new.
    The logbook write happens before the notification attempt, so the record
    exists even when Signal is unreachable.
    """
    anchor = AnchorFile(config.anchor_file)
    silence_file = SilenceFile(config.silence_file)
    nag = Nag()
    was_silenced = False

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.rule_interval)
            return
        except TimeoutError:
            pass

        apply_anchor_file(anchor, state, logbook, weather, config)
        _derived, transition = deriver.update(state)
        if transition is not None:
            logbook.write({"type": "state", **transition.as_dict()})

        events = engine.evaluate(state)

        # Read once and used for the whole tick, so nobody can write the file
        # between two channels and get an alert half delivered.
        quiet = silence_file.active()
        if quiet is not None:
            if not was_silenced:
                nag.reset()  # say it at once, then twice an hour after that
            if nag.due():
                # WARNING and repeated, because this line is the only thing
                # standing between a quiet journal and a crew believing the
                # boat is watching for them.
                log.warning("%s. Turn them back on with: boat --unsilence", quiet.describe())
                # Written on every nag rather than only when the switch is
                # thrown, so a silence that outlasts a whole 24 h window still
                # leaves a mark inside it and the ship's log cannot miss it.
                logbook.event(
                    "silenced",
                    note=quiet.note,
                    since=quiet.since.isoformat(timespec="seconds"),
                )
        elif was_silenced:
            nag.reset()
            logbook.event("unsilenced")
            standing = await resume_after_silence(engine, notifier, desktop)
            log.warning(
                "alerts are back on; %d still standing and re-announced", standing
            )
        was_silenced = quiet is not None

        # A silence holds all three channels here, at the one place every alert
        # passes through, and nowhere else. The rules above and the log below
        # do not know it exists: a silenced boat is watched and written down
        # exactly as a loud one is, and only the ways out are shut.

        # The noise comes first, before anything that can wait on the world.
        # signal-cli starts a JVM for every message and a send with no link
        # sits there until it times out, so a drag alarm queued behind it can
        # be minutes late. tick() only starts a background task, so putting it
        # here costs the rest of this loop nothing.
        if quiet is None:
            for event in events:
                alarm.notice(event)
            await alarm.tick(engine.active)

        for event in events:
            logbook.write({"type": "alert", **event.as_dict()})
            # On screen before Signal, for the same reason the noise came
            # before both: this one is instant and the send is not.
            if quiet is None:
                await desktop.handle(event)
            level = SEVERITY_LOG_LEVEL.get(event.alert.severity, logging.WARNING)
            if event.kind == "cleared":
                log.info("CLEARED  %s: %s", event.alert.rule_id, event.alert.message)
            else:
                log.log(
                    level,
                    "%s %s: %s",
                    event.kind.upper(),
                    event.alert.rule_id,
                    event.alert.message,
                )
            if quiet is None:
                await notifier.handle(event)

        # Retry anything the link was down for. Cheap when the outbox is empty.
        # Held while silenced: a backlog arriving the moment the link returns is
        # exactly what somebody asked not to happen.
        if quiet is None and len(notifier.outbox):
            await notifier.flush()

        # One payload, built here on this thread, for everything that wants to
        # show the boat to a person: the page over HTTP, and the file the
        # console reads. Neither of them ever touches a live object, so neither
        # can slow this loop down.
        if track is not None:
            where = as_position(state.value("navigation.position"))
            track.add(where, datetime.now(UTC))
            fix = anchor.fix
            track.watch(
                None if fix is None else (fix.latitude, fix.longitude),
                None if fix is None else fix.set_at,
                where,
                datetime.now(UTC),
            )
        payload = build_payload(
            state,
            _derived,
            engine.active,
            anchor.fix,
            weather,
            alarm.hush.active() if alarm.hush else None,
            config,
            track=track,
            silence=quiet,
        )
        if dashboard is not None:
            dashboard.publish(payload)
        write_status(config.status_path, payload)


async def resume_after_silence(
    engine: RuleEngine, notifier: AlertNotifier, desktop: DesktopNotifier
) -> int:
    """The alarms are back on with something still standing: say it now.

    Signal and the screen only ever see transitions, and an alert raised during
    a silence had its transition while nobody was listening. Without this,
    turning the alarms back on would leave a drag alarm standing in the log, on
    the console, and in no place a person would actually meet it - which is the
    exact failure the silence was supposed not to cause.

    The speaker needs no help: it sounds on what is standing rather than on
    what has just changed, so it starts again by itself on the next tick. This
    is for the two channels that work the other way.
    """
    standing = list(engine.active)
    for alert in standing:
        event = AlertEvent("raised", alert)
        await desktop.handle(event)
        await notifier.handle(event)
    return len(standing)


async def inbox_loop(
    logbook: Logbook, notifier: AlertNotifier, config: Config, stop: asyncio.Event
) -> None:
    """Answer questions asked in the crew group.

    Off the alarm path in both directions, like the forecast and the ship's log.
    signal-cli is blocking so it runs in a worker thread, a failure is the
    normal state at sea and gets logged once rather than every cycle, and a bug
    in here cannot stop a rule firing.
    """
    failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.signal_poll)
            return
        except TimeoutError:
            pass

        try:
            raw = await asyncio.to_thread(receive, config)
        except InboxError as exc:
            failures += 1
            # Said once, then dropped: no link is routine, and a line every
            # minute would bury the things that matter.
            if failures == 1:
                log.warning("cannot read the Signal inbox: %s", exc)
            else:
                log.debug("still cannot read the Signal inbox (%d): %s", failures, exc)
            continue
        except Exception:
            log.exception("the Signal inbox failed unexpectedly, carrying on")
            continue
        failures = 0

        for who, asked in questions(raw, config.signal_group, config.signal_account):
            log.info("%s asked: %s", who, asked)
            try:
                reply = answer(asked, config)
                await notifier.notifier.send(reply)
            except Exception:
                log.exception("could not answer %r", asked)
                continue
            logbook.write(
                {"type": "event", "event": "asked", "who": who, "question": asked}
            )


# How often the weather loop wakes up to ask whether a fetch is due. The store
# decides whether it actually is; this is only how often the question is put.
WEATHER_TICK = 60.0


async def weather_loop(
    state: BoatState,
    logbook: Logbook,
    store: WeatherStore,
    config: Config,
    stop: asyncio.Event,
) -> None:
    """Keep a forecast for wherever the boat currently is.

    Off the alarm path in both directions: nothing here can stop a rule firing,
    and no rule waits on it. With no link there is no forecast, and the one
    rule that reads it says nothing rather than guessing.

    The fetch is blocking urllib, so it goes to a worker thread. Everything
    else in the agent keeps running while a satellite link thinks about it.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=WEATHER_TICK)
            return
        except TimeoutError:
            pass

        # No position, no question worth asking. Nothing is fetched for a
        # guessed position - a forecast for the wrong island is worse than none.
        position = as_position(state.value("navigation.position"))
        if position is None:
            # Recorded rather than passed over in silence: "nothing to ask
            # about" and "asked and got nothing" look identical from outside
            # and mean different things to somebody arming an anchor watch.
            store.blocked = NO_POSITION
            continue
        store.blocked = None

        now = datetime.now(UTC)
        if not store.due(position, now):
            continue

        try:
            forecast = await asyncio.to_thread(fetch_forecast, position)
        except WeatherError as exc:
            store.record_failure(str(exc), now)
            # Said once, then dropped to debug. Days with no sky are normal on
            # this boat and a journal full of them buries the lines that matter.
            if store.failures == 1:
                log.warning("no forecast: %s", exc)
                logbook.event("forecast_failed", reason=str(exc))
            else:
                log.debug("still no forecast after %d tries: %s", store.failures, exc)
            continue
        except Exception:
            log.exception("the forecast fetch failed unexpectedly, carrying on")
            store.record_failure("unexpected error", now)
            continue

        store.record(forecast, now)
        # Into the state model as though a plugin had published it, so the
        # both the snapshots and the ship's log see one forecast.
        delta = forecast.as_delta(now)
        if delta is not None:
            state.apply_delta(delta)
        logbook.write({"type": "forecast", **forecast.as_dict(now)})
        log.info("forecast: %s", forecast.describe(now))


def seconds_until(hour: int, now: datetime) -> float:
    """Seconds from now to the next time it is `hour` o'clock, UTC."""
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def ships_log_loop(
    logbook: Logbook,
    config: Config,
    stop: asyncio.Event,
) -> None:
    """Write one entry a day into the ship's log, from the logbook.

    Off the alarm path: the figures come from the log file on disk, so this
    loop reads nothing live and can fail without touching anything that
    matters.
    """
    while not stop.is_set():
        wait = seconds_until(config.ships_log_hour, datetime.now(UTC))
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
            return
        except TimeoutError:
            pass

        write_ships_log(logbook, config)


def write_ships_log(logbook: Logbook, config: Config) -> bool:
    """Build today's entry and put it at the top of the file."""
    facts = build_digest(config.log_dir)
    written = update_file(
        config.ships_log_path, render_entry(facts), keep=config.ships_log_entries
    )
    if written:
        logbook.write(
            {
                "type": "ships_log",
                "path": str(config.ships_log_path),
                "alerts": len(facts.alerts),
                "snapshots": facts.snapshots,
            }
        )
    return written


async def run(config: Config, once: bool = False, settle: float = 10.0) -> int:
    state = BoatState()
    weather = WeatherStore(interval=config.weather_interval) if config.weather else None
    made = build_ui(config)
    dashboard, ui_server = made if made else (None, None)
    track = Track()
    engine = RuleEngine(
        build_default_rules(
            weather, config.weather_outlook_hours, config.anchor_outlook_hours
        )
    )
    deriver = StateDeriver()
    notifier = AlertNotifier(
        notifier=build_notifier(config),
        outbox=Outbox(config.outbox_path),
        min_severity=Severity(config.notify_min_severity),
        boat=config.boat_name,
    )
    # Nothing to listen to unless Signal is actually wired up: the log notifier
    # has no inbox, and polling one that cannot exist would be a JVM a minute
    # for nothing.
    listening = bool(
        config.signal_listen
        and config.signal_account
        and notifier.notifier.name == "signal"
    )
    desktop = build_desktop_notifier(config)
    alarm = build_local_alarm(config)
    stop = asyncio.Event()

    with Logbook(config.log_dir, retention_days=config.log_retention_days) as logbook:

        async def on_connect() -> None:
            logbook.event("signalk_connected", host=config.signalk_host)

        async def on_disconnect(reason: str) -> None:
            logbook.event("signalk_disconnected", reason=reason)
            log.warning("disconnected from Signal K: %s", reason)

        client = SignalKClient(config, state, on_connect=on_connect, on_disconnect=on_disconnect)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        logbook.event("agent_started", host=config.signalk_host, discover=config.discover)
        log.info("boat agent starting, Signal K at %s", config.ws_url)

        # Being refused is not an error the stream reports: an unauthorised
        # client can hold a subscription open and simply be told nothing. A
        # silently blind agent raises no alarms, so say it loudly, once.
        probe = await asyncio.to_thread(check_read_access, config.rest_url, config.headers)
        if probe.unauthorized:
            log.error(
                "Signal K is refusing to let this agent read anything (401). It will "
                "connect and see nothing, and no rule can fire. Get a token with: "
                "python -m agent.main --request-access"
            )
            logbook.event("signalk_unauthorized", url=config.rest_url)
        elif not probe.ok:
            log.warning("could not check read access (%s); carrying on", probe.detail)
        log.info(
            "alerts at %s and above go out over %s",
            config.notify_min_severity,
            notifier.notifier.name,
        )
        if desktop.enabled:
            log.info(
                "alerts at %s and above also appear on this machine's screen",
                config.desktop_min_severity,
            )
        if alarm.enabled:
            log.info(
                "this machine sounds its own alarm at %s and above, every %.0fs "
                "until it clears",
                config.alarm_min_severity,
                config.alarm_repeat,
            )
        else:
            log.warning(
                "the local alarm is off, so every alert depends on Starlink being up"
            )
        # Last of the startup lines and the loudest, because it makes every one
        # above it untrue. Said at startup as well as on the tick, so a restart
        # while silenced cannot look like a normal one.
        held = SilenceFile(config.silence_file).active()
        if held is not None:
            log.error(
                "%s. The boat will watch itself and write everything down, and "
                "tell nobody. Turn the alarms back on with: boat --unsilence",
                held.describe(),
            )
        if ui_server is not None:
            warn_if_open(config)
            if ui_server.start():
                log.info("the page is up at %s", ui_server.url)
        if listening:
            log.info(
                "listening for questions in the crew group every %.0fs; "
                "reading only, and it will not weigh an anchor by text",
                config.signal_poll,
            )
        if weather is None:
            log.info("the forecast is off (AGENT_WEATHER=0): no wind warnings")
        else:
            log.info(
                "forecast from Open-Meteo every %.0f min, looking %.0f h ahead",
                config.weather_interval / 60,
                config.weather_outlook_hours,
            )

        if once:
            async with asyncio.TaskGroup() as group:
                task = group.create_task(client.run(stop))
                await asyncio.sleep(settle)
                stop.set()
                await asyncio.wait([task])
        else:
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(client.run(stop))
                    group.create_task(snapshot_loop(state, logbook, deriver, config, stop))
                    group.create_task(
                        rules_loop(
                            state,
                            logbook,
                            engine,
                            notifier,
                            desktop,
                            alarm,
                            deriver,
                            config,
                            stop,
                            weather,
                            dashboard,
                            track,
                        )
                    )
                    if weather is not None:
                        group.create_task(weather_loop(state, logbook, weather, config, stop))
                    if listening:
                        group.create_task(inbox_loop(logbook, notifier, config, stop))
                    group.create_task(ships_log_loop(logbook, config, stop))
            finally:
                await alarm.aclose()
                if ui_server is not None:
                    ui_server.stop()

        logbook.snapshot(state, stale_after=config.stale_after)
        logbook.event("agent_stopped", deltas=state.deltas_seen, paths=len(state))

    log.info(
        "stopped after %d deltas across %d paths", state.deltas_seen, len(state.path_counts)
    )
    log.info("%s", format_status(state))

    if once:
        derived = deriver.derive(state)
        log.info("derived state: %s (%s) - %s", derived.vessel, derived.confidence, derived.reason)
        active = engine.evaluate(state)
        for event in active:
            log.warning("%s %s: %s", event.kind, event.alert.rule_id, event.alert.message)
        for path in state.paths():
            sample = state.get(path)
            assert sample is not None
            log.info("  %-52s %s  [%s]", path, sample.value, sample.source)
        return 0 if state.deltas_seen else 1
    return 0


def write_env_token(token: str, path: Path | None = None) -> bool:
    """Put SIGNALK_TOKEN in .env, replacing any line already there."""
    path = path or REPO_ROOT / ".env"
    line = f"SIGNALK_TOKEN={token}"
    try:
        existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        kept = [row for row in existing if not row.strip().startswith("SIGNALK_TOKEN=")]
        if kept and kept[-1].strip():
            kept.append("")
        path.write_text("\n".join([*kept, line, ""]), encoding="utf-8")
    except OSError as exc:
        log.error("could not write the token to %s: %s", path, exc)
        return False

    # Tightening the permissions is a separate question from saving the token,
    # and failing at it is not failing to save. Not every filesystem the repo
    # can sit on supports chmod: a gvfs SMB mount raises ENOTSUP, and the token
    # is on disk regardless. Reporting that as a write failure sends the crew
    # off to paste it in by hand over a file that already has it.
    try:
        path.chmod(0o600)
    except OSError as exc:
        log.warning(
            "the token is saved in %s, but its permissions could not be tightened "
            "to 0600 (%s). Check who can read it: %s",
            path,
            exc,
            oct(path.stat().st_mode & 0o777),
        )
    return True


def request_access(config: Config) -> int:
    """Get a read token from Signal K and save it, so the agent can see the bus."""
    base = f"http://{config.signalk_host}:{config.signalk_port}"
    token, detail = request_token(base)
    if token is None:
        log.error("no token: %s", detail)
        return 1

    log.info("token received (%s)", detail)
    if not write_env_token(token):
        log.error("save it by hand instead:\n\nSIGNALK_TOKEN=%s\n", token)
        return 1

    log.info(
        "saved to .env. Restart the agent to use it: sudo systemctl restart boat-agent"
    )
    return 0


def parse_latlon(text: str) -> tuple[float, float] | None:
    """"36.83,10.30" from the command line, or nothing."""
    parts = text.replace(" ", "").split(",")
    if len(parts) != 2:
        return None
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def current_position(config: Config) -> tuple[float, float] | None:
    """Ask Signal K where the boat is. None if it cannot say."""
    url = f"{config.rest_url}/navigation/position/value"
    request = urllib.request.Request(url, headers=config.headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.error("cannot read the boat's position from Signal K: %s", exc)
        return None
    return as_position(payload)


def anchor_down(config: Config, radius_m: float, at: str | None = None) -> int:
    """Arm the drag watch, here or at a given position."""
    if at:
        fix = parse_latlon(at)
        if fix is None:
            log.error("could not read %r as LAT,LON in decimal degrees", at)
            return 2
        source = "given on the command line"
    else:
        fix = current_position(config)
        if fix is None:
            log.error(
                "no position from Signal K, so the anchor cannot be set here. "
                "Pass --at LAT,LON to set it by hand."
            )
            return 1
        source = "the boat's position when the anchor was set"

    anchor = AnchorFile(config.anchor_file)
    written = anchor.write(
        AnchorFix(
            latitude=fix[0],
            longitude=fix[1],
            radius_m=radius_m,
            set_at=datetime.now(UTC),
            note=source,
        )
    )
    if not written:
        return 1

    log.info(
        "anchor watch set at %s, circle %.0f m. The agent picks it up within "
        "a few seconds and will keep it across a restart.",
        format_position({"latitude": fix[0], "longitude": fix[1]}),
        radius_m,
    )
    check_the_night(config, fix)
    return 0


def check_the_night(config: Config, position: tuple[float, float]) -> None:
    """Look at the weather at the moment the hook goes down.

    Printed here rather than only in the running agent journal, because this is
    what the crew is actually looking at when they arm the watch, and it is the
    last cheap moment to do something about the answer: more chain, a different
    cove, or leave while there is light.

    It never stops the watch being set. A forecast that cannot be fetched is
    reported as not fetched, which is worth knowing before turning in.
    """
    if not config.weather:
        return

    try:
        forecast = fetch_forecast(position)
    except WeatherError as exc:
        log.warning(
            "could not check the forecast for tonight (%s). The watch is set either "
            "way, but nothing has looked at the weather.",
            exc,
        )
        return

    now = datetime.now(UTC)
    line, reason = night_ahead(forecast.outlook(config.anchor_outlook_hours, now))
    if reason is None:
        log.info("%s", line)
        return

    log.warning("%s", line)
    log.warning(
        "that is %s before morning. Worth checking the scope, the swinging room, "
        "and where the shore is if the wind veers.",
        reason,
    )


def anchor_up(config: Config) -> int:
    """Stand the drag watch down."""
    anchor = AnchorFile(config.anchor_file)
    if anchor.read() is None:
        log.info("no anchor watch was set; nothing to clear")
        return 0
    if not anchor.write(None):
        return 1
    log.info("anchor watch cleared")
    return 0


async def ships_log_now(config: Config) -> int:
    """--ships-log: write an entry from what is already on disk, and exit.

    Touches no instruments and needs no Signal K, so it works on a laptop with
    a copy of the logs as happily as on the Pi.
    """
    with Logbook(config.log_dir, retention_days=config.log_retention_days) as logbook:
        written = write_ships_log(logbook, config)
    if written:
        log.info("wrote %s", config.ships_log_path)
    return 0 if written else 1


async def forecast_now(config: Config, at: str | None) -> int:
    """--forecast: fetch the forecast for here, print it, and exit.

    For the evening before, when the question is whether this cove is still a
    good idea tomorrow. The agent does the same fetch on its own every hour;
    this one prints the hours rather than reducing them to a single alert.
    """
    if at:
        position = parse_latlon(at)
        if position is None:
            log.error("could not read %r as LAT,LON in decimal degrees", at)
            return 2
    else:
        position = current_position(config)
        if position is None:
            log.error(
                "no position from Signal K, so there is nowhere to forecast for. "
                "Pass --at LAT,LON."
            )
            return 1

    try:
        forecast = await asyncio.to_thread(fetch_forecast, position)
    except WeatherError as exc:
        log.error("no forecast: %s", exc)
        return 1

    now = datetime.now(UTC)
    where = format_position({"latitude": position[0], "longitude": position[1]})
    log.info("forecast for %s", where)
    log.info("%s", forecast.describe(now))
    if not forecast.waves:
        log.info("no wave forecast for this point: wind only")

    log.info("  %-16s %5s %5s %5s %6s", zone_name() or "local", "wind", "gust", "dir", "sea")
    for hour in forecast.window(config.weather_outlook_hours, now):
        log.info(
            "  %-16s %5s %5s %5s %6s",
            dayhhmm(hour.time),
            "-" if hour.wind_ms is None else f"{knots(hour.wind_ms):.0f}kn",
            "-" if hour.gust_ms is None else f"{knots(hour.gust_ms):.0f}kn",
            cardinal(hour.direction_rad) or "-",
            "-" if hour.wave_m is None else f"{hour.wave_m:.1f}m",
        )

    outlook = forecast.window(config.weather_outlook_hours, now)
    windiest = Forecast.peak(outlook, "wind_ms")
    if windiest is None or windiest.wind_ms is None:
        return 0
    if windiest.wind_ms >= FORECAST_WIND_WARN_MS:
        log.warning(
            "that is F6 or more inside %.0f h. The running agent would say so too.",
            config.weather_outlook_hours,
        )
    return 0


# What a drill can and cannot establish. Worth printing every time, because
# the whole point of the exercise is not to come away more confident than the
# evidence supports.
DRILL_CAVEAT = (
    "A drill proves the boat can reach each channel. It cannot prove a message "
    "woke you, that anyone looked at the screen, or that the noise was audible "
    "from a bunk. Mute a Signal group or turn the volume down and this still "
    "passes."
)


@dataclass(frozen=True)
class DrillResult:
    """One channel, and what actually happened on it."""

    channel: str
    outcome: str  # "sent" | "off" | "failed"
    detail: str

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


async def drill(config: Config) -> int:
    """--drill: push a real alert down every channel and report what got through.

    --test-alarm proves the speaker and nothing else. This proves the chain:
    the message that reaches a phone, the notification on the screen, and the
    noise in the saloon, using the same code an actual drag alarm uses rather
    than a rehearsal of it. You test the flares and the liferaft before you
    need them; this is the same idea, and it should be runnable from the
    cockpit before dark.

    It exits non-zero if any channel that is CONFIGURED did not work. A channel
    that is off by choice is reported loudly and is not a failure - though
    finding out that Signal was never connected is exactly what this is for.
    """
    now = datetime.now(UTC)
    event = AlertEvent(
        "raised",
        Alert(
            rule_id="drill",
            severity=Severity.ALARM,
            message=(
                "DRILL. This is a test of the boat's alarms, not a real one. "
                "Nothing is wrong and no action is needed."
            ),
            since=now,
        ),
    )

    results: list[DrillResult] = [
        await _drill_signal(config, event),
        await _drill_screen(config, event),
        await _drill_speaker(config, event),
    ]

    width = max(len(r.channel) for r in results)
    log.info("")
    for result in results:
        mark = {"sent": "OK  ", "off": "OFF ", "failed": "FAIL"}[result.outcome]
        log.info("  %s  %-*s  %s", mark, width, result.channel, result.detail)
    log.info("")
    log.info("%s", DRILL_CAVEAT)

    # The drill itself goes out regardless: a test a silence could hide would
    # be worthless, and this is run precisely to find out what the boat can do.
    # But a passing table under a standing silence is the most misleading thing
    # this command could print, so it is called out and it fails the drill.
    held = SilenceFile(config.silence_file).active()
    if held is not None:
        log.error("")
        log.error(
            "%s. Whatever the table above says, a real alarm right now would "
            "reach nobody. Turn them back on with: boat --unsilence",
            held.describe(),
        )

    with Logbook(config.log_dir, retention_days=config.log_retention_days) as logbook:
        logbook.write(
            {
                "type": "event",
                "event": "drill",
                "silenced": held is not None,
                "results": {r.channel: {"outcome": r.outcome, "detail": r.detail}
                            for r in results},
            }
        )

    broken = [r for r in results if r.failed]
    if broken:
        log.error(
            "%d of %d channels failed. The boat cannot raise the alarm the way "
            "you think it can.",
            len(broken),
            len(results),
        )
        return 1
    if all(r.outcome == "off" for r in results):
        log.error("every channel is off. Nothing would reach anybody.")
        return 1
    # Same verdict as every channel being off, because that is what it is.
    if held is not None:
        return 1
    return 0


async def _drill_signal(config: Config, event: AlertEvent) -> DrillResult:
    """The only channel that reaches somebody who is not aboard."""
    # Building the channel is inside the guard, not outside it. A drill exists
    # to survive one broken channel and still test the other two, and a
    # constructor that blows up is exactly the sort of breakage worth finding.
    try:
        notifier = AlertNotifier(
            notifier=build_notifier(config),
            outbox=Outbox(config.outbox_path),
            min_severity=Severity(config.notify_min_severity),
            boat=config.boat_name,
        )
    except Exception as exc:
        log.exception("the drill could not build the Signal channel")
        return DrillResult("Signal", "failed", f"could not be set up: {exc}")

    if notifier.notifier.name == "log":
        return DrillResult(
            "Signal",
            "off",
            "not configured: alerts are written to the journal and sent nowhere. "
            "See deploy/install-signal-cli.sh",
        )

    waiting = len(notifier.outbox)
    try:
        await notifier.handle(event)
    except Exception as exc:  # pragma: no cover - a bug here must still report
        log.exception("the drill's Signal send raised")
        return DrillResult("Signal", "failed", f"raised: {exc}")

    if len(notifier.outbox) > waiting:
        return DrillResult(
            "Signal",
            "failed",
            "queued but not delivered: the link is down. It will go when the "
            "link returns, marked with how late it is",
        )
    where = "the crew group" if config.signal_group else ", ".join(config.signal_recipients)
    return DrillResult("Signal", "sent", f"delivered to {where or 'nobody configured'}")


async def _drill_screen(config: Config, event: AlertEvent) -> DrillResult:
    """The weakest channel: it needs a session, a daemon, and someone looking."""
    try:
        desktop = build_desktop_notifier(config)
    except Exception as exc:
        log.exception("the drill could not build the screen channel")
        return DrillResult("Screen", "failed", f"could not be set up: {exc}")

    if not desktop.enabled:
        return DrillResult("Screen", "off", "turned off (AGENT_DESKTOP_NOTIFY=0)")
    try:
        shown = await desktop.handle(event)
    except Exception as exc:  # pragma: no cover
        log.exception("the drill's desktop notification raised")
        return DrillResult("Screen", "failed", f"raised: {exc}")
    if not shown:
        return DrillResult(
            "Screen",
            "failed",
            "notify-send would not run: no session, no notification daemon, or "
            "the agent cannot reach the desktop bus",
        )
    return DrillResult("Screen", "sent", "notification shown on this machine")


async def _drill_speaker(config: Config, event: AlertEvent) -> DrillResult:
    """The one channel that does not depend on Starlink."""
    try:
        alarm = build_local_alarm(config)
    except Exception as exc:
        log.exception("the drill could not build the speaker channel")
        return DrillResult("Speaker", "failed", f"could not be set up: {exc}")

    if not alarm.enabled:
        return DrillResult(
            "Speaker",
            "off",
            "turned off (AGENT_ALARM_SOUND=0): every alert now depends on the link",
        )

    quiet = alarm.hush.active() if alarm.hush else None
    path = build_alarm_wav(config.alarm_wav_path)
    if path is None:
        return DrillResult("Speaker", "failed", "could not write the alarm sound")
    if not alarm.player.specs():
        return DrillResult(
            "Speaker",
            "failed",
            "nothing on this machine can play a sound. Install one: "
            "sudo apt install alsa-utils",
        )

    played = await alarm.player.play(path)
    await alarm.aclose()
    if not played:
        return DrillResult("Speaker", "failed", "no player would play it")
    note = f"played with {played}"
    if quiet is not None:
        # A hush silences a real alarm and deliberately does not silence this,
        # so say so rather than let the drill imply the boat is louder than it is.
        note += (
            f"; NOTE a hush is in force until {hhmm(quiet.until)}, "
            "and a real alarm would be silent"
        )
    return DrillResult("Speaker", "sent", note)


async def test_alarm(config: Config) -> int:
    """--test-alarm: prove the boat can make a noise before trusting it to.

    Worth running after every install and at the start of every season. An
    alarm nobody has heard is a guess, and the failure modes are quiet ones:
    the volume down, the output on an HDMI socket with nothing plugged into it,
    a service with no way to reach the sound server.
    """
    try:
        alarm = build_local_alarm(config)
    except Exception as exc:
        log.exception("the drill could not build the speaker channel")
        return DrillResult("Speaker", "failed", f"could not be set up: {exc}")

    if not alarm.enabled:
        log.warning("the local alarm is turned off (AGENT_ALARM_SOUND=0) - testing it anyway")

    path = build_alarm_wav(config.alarm_wav_path)
    if path is None:
        return 1

    quiet = alarm.hush.active() if alarm.hush else None
    if quiet is not None:
        log.warning(
            "a hush is in force until %s; it does not apply to this test",
            hhmm(quiet.until),
        )
    held = SilenceFile(config.silence_file).active()
    if held is not None:
        log.error(
            "%s; it does not apply to this test, so hearing the noise now "
            "proves nothing about what a real alarm would do",
            held.describe(),
        )

    tried = alarm.player.specs()
    if not tried:
        log.error(
            "no way to play a sound on this machine. Install one: "
            "sudo apt install alsa-utils (aplay), or pipewire (pw-play)."
        )
        return 1

    log.info("playing %s with %s", path, ", ".join(spec[0] for spec in tried))
    worked = await alarm.player.play(path)
    if not worked:
        log.error("nothing would play it. Run one of them by hand to see the error.")
        return 1

    log.info(
        "%s played it. If you heard nothing the volume is down or the sound is "
        "going to an output with nothing on the end of it.",
        worked,
    )
    return 0


def hush(config: Config, minutes: float) -> int:
    """--hush: quiet the speaker for a while, and only the speaker."""
    if minutes <= 0:
        log.error("a hush needs a positive number of minutes")
        return 2
    entry = hush_until(minutes)
    if not HushFile(config.hush_file).write(entry):
        return 1
    log.info(
        "local alarm hushed until %s (%.0f min). Alerts still go out over "
        "Signal and still reach the log; only the noise stops.",
        hhmm(entry.until),
        minutes,
    )
    return 0


def unhush(config: Config) -> int:
    """--unhush: end a hush early."""
    hush_file = HushFile(config.hush_file)
    if hush_file.active() is None:
        log.info("the local alarm was not hushed; nothing to end")
        return 0
    if not hush_file.write(None):
        return 1
    log.info("hush ended: the local alarm may sound again")
    return 0


def silence(config: Config, note: str) -> int:
    """--silence: every channel off, and unlike a hush it does not expire."""
    silence_file = SilenceFile(config.silence_file)
    already = silence_file.active()
    if already is not None:
        log.warning("already %s", already.describe())
        return 0

    entry = silence_now(note or "silenced from the command line")
    if not silence_file.write(entry):
        return 1

    # ERROR rather than INFO. The person reading this is about to walk away
    # believing something about the boat, and what they should believe is that
    # nothing will be told to them until they come back and say so.
    log.error(
        "SILENCED. The speaker, Signal and this machine's screen are all off, "
        "and this does NOT expire. The boat keeps watching and writes "
        "everything down; it will simply tell nobody. Turn the alarms back on "
        "with: boat --unsilence"
    )
    if entry.note:
        log.info("noted: %s", entry.note)
    return 0


def unsilence(config: Config) -> int:
    """--unsilence: every channel back on."""
    silence_file = SilenceFile(config.silence_file)
    held = silence_file.active()
    if held is None:
        log.info("the alarms were already on; nothing to turn back on")
        return 0
    if not silence_file.write(None):
        return 1
    log.info(
        "alerts back on after %s. A running agent picks it up within a rule "
        "tick and re-announces anything still standing.",
        held.held_for(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = build_config(args)
    # The console draws a screen. The startup lines that belong in journalctl
    # would push the top of it off, so they are held back to warnings unless
    # somebody has asked for more.
    setup_logging("WARNING" if args.console and not args.log_level else config.log_level)
    setup_display_timezone(config)
    try:
        if args.request_access:
            return request_access(config)
        if args.anchor_down:
            return anchor_down(config, args.radius, args.at)
        if args.anchor_up:
            return anchor_up(config)
        if args.ships_log:
            return asyncio.run(ships_log_now(config))
        if args.test_alarm:
            return asyncio.run(test_alarm(config))
        if args.drill:
            return asyncio.run(drill(config))
        if args.hush is not None:
            return hush(config, args.hush)
        if args.unhush:
            return unhush(config)
        if args.silence is not None:
            return silence(config, args.silence)
        if args.unsilence:
            return unsilence(config)
        if args.forecast:
            return asyncio.run(forecast_now(config, args.at))
        if args.console:
            return run_console(config)
        return asyncio.run(run(config, once=args.once, settle=args.settle))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

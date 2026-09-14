"""Agent configuration, from environment variables.

On the Pi systemd loads .env via EnvironmentFile. Running from a laptop there is
no systemd, so load_env_file() reads the same file directly. Neither one
overwrites a variable that is already set.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .notify import parse_recipients

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """Read simple KEY=VALUE lines into os.environ. Never executes the file."""
    path = path or REPO_ROOT / ".env"
    if not path.is_file():
        return

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, default))
    except ValueError:
        log.warning("%s=%r is not a number, using %s", key, env.get(key), default)
        return default


def _int(env: dict[str, str], key: str, default: int) -> int:
    return int(_float(env, key, default))


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Defaults assume the agent runs on the Pi alongside Signal K. Point
    # SIGNALK_HOST at boat-pi.local to run it from the laptop.
    signalk_host: str = "localhost"
    signalk_port: int = 3000
    signalk_token: str | None = None

    # Subscribe to every self path instead of the curated list in paths.py.
    # For finding out what the bus actually publishes; not for normal running.
    discover: bool = False

    snapshot_interval: float = 60.0
    stale_after: float = 300.0
    # Rules run far more often than snapshots: shallow water has to be
    # caught in seconds, while a snapshot every 5s would bloat the log.
    rule_interval: float = 5.0

    reconnect_min: float = 1.0
    reconnect_max: float = 60.0

    log_dir: Path = REPO_ROOT / "logs"
    log_level: str = "INFO"
    # Days of daily logs to keep. Finished days are gzipped either way;
    # 0 or less keeps them forever.
    log_retention_days: int = 90
    # The ship's log: one markdown entry a day, newest first. The hour is UTC,
    # and 0 to 23 are all valid; the default lands it before breakfast.
    ships_log_path: Path = REPO_ROOT / "logs" / "ships-log.md"
    ships_log_hour: int = 6
    ships_log_entries: int = 90
    # Where the crew says the hook is down. Read by the running agent,
    # written by --anchor-down / --anchor-up.
    anchor_file: Path = REPO_ROOT / "logs" / "anchor.json"

    # Signal, via signal-cli linked as a secondary device.
    # Unset SIGNAL_ACCOUNT means alerts are logged but not sent.
    signal_account: str = ""
    # One number, or several: SIGNAL_RECIPIENT="+301, +302". Each gets its own
    # copy of every alert. Past two or three phones a Signal group is cheaper.
    signal_recipients: tuple[str, ...] = ()
    signal_group: str = ""
    signal_cli_path: str = "signal-cli"
    notify_min_severity: str = "alert"
    # Answer questions asked in the crew group. Off unless Signal is set up,
    # since there is nothing to listen to otherwise. Reading only, plus a hush:
    # this will not arm or weigh an anchor by text.
    signal_listen: bool = True
    # signal-cli takes about four seconds to start a JVM, so an answer arrives
    # within a minute rather than instantly. That is the right trade for "how
    # is she doing"; the alarms do not come this way.
    signal_poll: float = 60.0
    outbox_path: Path = REPO_ROOT / "logs" / "outbox.json"
    boat_name: str = "Seabird"

    # Alerts on this machine's screen (agent/desktop.py), through notify-send.
    # The weakest of the three channels - it needs a session, a notification
    # daemon and someone looking - and the only one that reaches the person
    # already sitting in front of the laptop. Same threshold as Signal by
    # default, so everything that gets a message also gets a notification.
    desktop_notify: bool = True
    desktop_min_severity: str = "alert"
    notify_send_path: str = "notify-send"

    # A noise this machine makes itself (agent/sound.py). Everything Signal
    # sends depends on Starlink being up; the speakers do not, which is the one
    # thing the nav laptop does better than the Pi would have.
    #
    # Alarm and above only. A machine that beeps at every warning is a machine
    # somebody turns the volume down on, and then the drag alarm is silent too.
    alarm_sound: bool = True
    alarm_min_severity: str = "alarm"
    alarm_repeat: float = 20.0
    # Force a player instead of picking one: "aplay -q {file}", say.
    alarm_player: str = ""
    alarm_wav_path: Path = REPO_ROOT / "logs" / "alarm.wav"
    # A quiet period with an expiry, written by --hush and read by the agent.
    hush_file: Path = REPO_ROOT / "logs" / "hush.json"
    # Every channel off at once, written by --silence and read by the agent.
    # Unlike a hush this does not expire, so the agent nags about it in the
    # journal, on the console, on the page and in the ship's log until it goes.
    silence_file: Path = REPO_ROOT / "logs" / "silence.json"
    # What the boat looks like right now, rewritten every rule tick. Read by
    # `--console` and by anything else that wants the picture without opening
    # its own connection to Signal K.
    status_path: Path = REPO_ROOT / "logs" / "status.json"

    # The forecast (agent/weather.py), from Open-Meteo. No key and no account,
    # so it is on by default; it needs a link and the boat's own position, and
    # says nothing without either. Set AGENT_WEATHER=0 to stop it reaching out
    # at all - on a metered link, or when the forecast is coming from a chart
    # plotter and a second opinion is not wanted.
    weather: bool = True
    weather_interval: float = 3600.0
    # How far ahead the forecast rule looks before it decides a blow is coming.
    weather_outlook_hours: float = 12.0
    # And how far ahead it looks once the hook is down, which is a different
    # question: not the next twelve hours but tonight. Also the window checked
    # and reported the moment the anchor watch is armed.
    anchor_outlook_hours: float = 18.0

    # A page for the phone in your pocket (agent/ui.py). Off by default, and
    # bound to localhost when it is on: the controls arm and clear an anchor
    # watch, and the boat's WiFi is not a place to leave those open to whoever
    # is anchored alongside. Set AGENT_UI_BIND=0.0.0.0 to reach it from a phone,
    # and set AGENT_UI_TOKEN when you do.
    # The zone times are SHOWN in. Everything written down stays UTC whatever
    # this says. Empty means the machine's own zone, which on the nav laptop is
    # boat time; set an IANA name like Europe/Helsinki when the laptop's zone is
    # wrong, which after a delivery it usually is.
    timezone: str = ""

    ui: bool = False
    ui_bind: str = "127.0.0.1"
    ui_port: int = 8375
    ui_token: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        return cls(
            signalk_host=env.get("SIGNALK_HOST", cls.signalk_host),
            signalk_port=_int(env, "SIGNALK_PORT", cls.signalk_port),
            signalk_token=env.get("SIGNALK_TOKEN") or None,
            discover=env.get("AGENT_DISCOVER", "").lower() in ("1", "true", "yes"),
            snapshot_interval=_float(env, "AGENT_SNAPSHOT_INTERVAL", cls.snapshot_interval),
            stale_after=_float(env, "AGENT_STALE_AFTER", cls.stale_after),
            rule_interval=_float(env, "AGENT_RULE_INTERVAL", cls.rule_interval),
            reconnect_min=_float(env, "AGENT_RECONNECT_MIN", cls.reconnect_min),
            reconnect_max=_float(env, "AGENT_RECONNECT_MAX", cls.reconnect_max),
            log_dir=Path(env.get("AGENT_LOG_DIR", str(cls.log_dir))),
            log_level=env.get("AGENT_LOG_LEVEL", cls.log_level).upper(),
            log_retention_days=_int(env, "AGENT_LOG_RETENTION_DAYS", cls.log_retention_days),
            ships_log_path=Path(env.get("AGENT_SHIPS_LOG", str(cls.ships_log_path))),
            ships_log_hour=_int(env, "AGENT_SHIPS_LOG_HOUR", cls.ships_log_hour) % 24,
            ships_log_entries=_int(env, "AGENT_SHIPS_LOG_ENTRIES", cls.ships_log_entries),
            anchor_file=Path(env.get("AGENT_ANCHOR_FILE", str(cls.anchor_file))),
            signal_account=env.get("SIGNAL_ACCOUNT", cls.signal_account).strip(),
            signal_recipients=parse_recipients(env.get("SIGNAL_RECIPIENT", "")),
            signal_group=env.get("SIGNAL_GROUP", cls.signal_group).strip(),
            signal_cli_path=env.get("SIGNAL_CLI", cls.signal_cli_path),
            signal_listen=_bool(env, "SIGNAL_LISTEN", cls.signal_listen),
            signal_poll=_float(env, "SIGNAL_POLL", cls.signal_poll),
            notify_min_severity=env.get(
                "AGENT_NOTIFY_MIN_SEVERITY", cls.notify_min_severity
            ).lower(),
            outbox_path=Path(env.get("AGENT_OUTBOX", str(cls.outbox_path))),
            boat_name=env.get("BOAT_NAME", cls.boat_name),
            desktop_notify=_bool(env, "AGENT_DESKTOP_NOTIFY", cls.desktop_notify),
            desktop_min_severity=env.get(
                "AGENT_DESKTOP_MIN_SEVERITY", cls.desktop_min_severity
            ).strip().lower(),
            notify_send_path=env.get("AGENT_NOTIFY_SEND", cls.notify_send_path).strip(),
            alarm_sound=_bool(env, "AGENT_ALARM_SOUND", cls.alarm_sound),
            alarm_min_severity=env.get(
                "AGENT_ALARM_MIN_SEVERITY", cls.alarm_min_severity
            ).strip().lower(),
            alarm_repeat=_float(env, "AGENT_ALARM_REPEAT", cls.alarm_repeat),
            alarm_player=env.get("AGENT_ALARM_PLAYER", cls.alarm_player).strip(),
            alarm_wav_path=Path(env.get("AGENT_ALARM_WAV", str(cls.alarm_wav_path))),
            hush_file=Path(env.get("AGENT_HUSH_FILE", str(cls.hush_file))),
            silence_file=Path(env.get("AGENT_SILENCE_FILE", str(cls.silence_file))),
            status_path=Path(env.get("AGENT_STATUS_FILE", str(cls.status_path))),
            weather=_bool(env, "AGENT_WEATHER", cls.weather),
            weather_interval=_float(env, "AGENT_WEATHER_INTERVAL", cls.weather_interval),
            weather_outlook_hours=_float(
                env, "AGENT_WEATHER_OUTLOOK_HOURS", cls.weather_outlook_hours
            ),
            anchor_outlook_hours=_float(
                env, "AGENT_ANCHOR_OUTLOOK_HOURS", cls.anchor_outlook_hours
            ),
            timezone=env.get("AGENT_TIMEZONE", cls.timezone).strip(),
            ui=_bool(env, "AGENT_UI", cls.ui),
            ui_bind=env.get("AGENT_UI_BIND", cls.ui_bind).strip(),
            ui_port=_int(env, "AGENT_UI_PORT", cls.ui_port),
            ui_token=env.get("AGENT_UI_TOKEN", cls.ui_token).strip(),
        )

    @property
    def ws_url(self) -> str:
        # subscribe=none: the server sends nothing until we ask for specific
        # paths. subscribe=self in discover mode means every self path.
        mode = "self" if self.discover else "none"
        return (
            f"ws://{self.signalk_host}:{self.signalk_port}"
            f"/signalk/v1/stream?subscribe={mode}"
        )

    @property
    def rest_url(self) -> str:
        return f"http://{self.signalk_host}:{self.signalk_port}/signalk/v1/api/vessels/self"

    @property
    def headers(self) -> dict[str, str]:
        if self.signalk_token:
            return {"Authorization": f"Bearer {self.signalk_token}"}
        return {}

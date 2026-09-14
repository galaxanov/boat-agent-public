from __future__ import annotations

import os
from pathlib import Path

from agent.config import Config, load_env_file


def test_defaults_assume_running_on_the_pi() -> None:
    config = Config.from_env({})
    assert config.signalk_host == "localhost"
    assert config.signalk_port == 3000
    assert config.signalk_token is None
    assert config.discover is False


def test_ws_url_subscribes_to_nothing_by_default() -> None:
    config = Config.from_env({"SIGNALK_HOST": "boat-pi.local"})
    assert config.ws_url == (
        "ws://boat-pi.local:3000/signalk/v1/stream?subscribe=none"
    )


def test_discover_mode_subscribes_to_self() -> None:
    config = Config.from_env({"AGENT_DISCOVER": "1"})
    assert config.ws_url.endswith("subscribe=self")
    assert config.discover is True


def test_token_becomes_an_auth_header() -> None:
    assert Config.from_env({}).headers == {}
    assert Config.from_env({"SIGNALK_TOKEN": "abc"}).headers == {
        "Authorization": "Bearer abc"
    }


def test_empty_token_is_treated_as_absent() -> None:
    assert Config.from_env({"SIGNALK_TOKEN": ""}).signalk_token is None


def test_bad_numeric_env_falls_back_to_the_default() -> None:
    config = Config.from_env({"AGENT_SNAPSHOT_INTERVAL": "sixty"})
    assert config.snapshot_interval == 60.0


def test_env_file_is_parsed_not_executed(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "export SIGNALK_TOKEN=from-file\n"
        'VICTRON_MPPT_MAC="dd:ee:ff:00:11:22"\n'
        "NOT A VALID LINE\n"
        "$(touch /tmp/should-not-happen)\n"
    )
    monkeypatch.delenv("SIGNALK_TOKEN", raising=False)
    monkeypatch.delenv("VICTRON_MPPT_MAC", raising=False)

    load_env_file(env_file)

    assert os.environ["SIGNALK_TOKEN"] == "from-file"
    assert os.environ["VICTRON_MPPT_MAC"] == "dd:ee:ff:00:11:22"
    assert not Path("/tmp/should-not-happen").exists()


def test_env_file_does_not_override_the_environment(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SIGNALK_HOST=from-file\n")
    monkeypatch.setenv("SIGNALK_HOST", "from-systemd")

    load_env_file(env_file)

    assert os.environ["SIGNALK_HOST"] == "from-systemd"


def test_missing_env_file_is_fine(tmp_path: Path) -> None:
    load_env_file(tmp_path / "nope.env")


def test_ships_log_settings_come_from_the_environment() -> None:
    config = Config.from_env(
        {
            "AGENT_SHIPS_LOG": "/srv/logs/ships-log.md",
            "AGENT_SHIPS_LOG_HOUR": "7",
            "AGENT_SHIPS_LOG_ENTRIES": "30",
        }
    )
    assert str(config.ships_log_path) == "/srv/logs/ships-log.md"
    assert (config.ships_log_hour, config.ships_log_entries) == (7, 30)


def test_a_nonsense_ships_log_hour_is_wrapped_not_crashed() -> None:
    assert Config.from_env({"AGENT_SHIPS_LOG_HOUR": "26"}).ships_log_hour == 2
    assert Config.from_env({"AGENT_SHIPS_LOG_HOUR": "elevenish"}).ships_log_hour == 6

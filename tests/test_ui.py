"""The page: what it shows, what a tap does, and who is allowed to tap.

The server is started for real on a loopback port and driven over HTTP, because
the things worth checking here are the wiring: that a tap writes the same file
the CLI writes, that a token actually stops somebody without one, and that a
reading nobody has reported comes out as absent rather than zero.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import ui as U
from agent.anchor import AnchorFile, AnchorFix
from agent.config import Config
from agent.derived import Confidence, Derived, VesselState
from agent.rules import Alert, Severity
from agent.state import BoatState
from agent.weather import NO_POSITION, WeatherStore, build_forecast

from .conftest import push

NOW = datetime(2026, 9, 8, 19, 30, tzinfo=UTC)
HOOK = {"latitude": 36.8312, "longitude": 10.3034}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        ui=True,
        ui_bind="127.0.0.1",
        ui_port=0,  # the OS picks a free one
        anchor_file=tmp_path / "anchor.json",
        hush_file=tmp_path / "hush.json",
        silence_file=tmp_path / "silence.json",
        log_dir=tmp_path / "logs",
    )


def derived(state=VesselState.ANCHORED) -> Derived:
    return Derived(vessel=state, confidence=Confidence.LIKELY, reason="stopped in the circle")


# ----------------------------------------------------------------- payload --


def test_a_reading_nobody_reported_is_absent_not_zero(config, clock) -> None:
    state = BoatState(clock=clock)
    push(state, {"navigation.speedOverGround": 2.5})

    payload = U.build_payload(state, derived(), [], None, None, None, config, now=NOW)
    by_label = {r["label"]: r for r in payload["readings"]}

    assert by_label["Speed"]["value"] == "4.9"  # 2.5 m/s in knots
    assert by_label["Depth"]["value"] is None  # never reported
    assert by_label["Battery"]["value"] is None


def test_with_no_fix_the_distance_from_the_anchor_is_unknown_not_zero(config, clock) -> None:
    """The worst possible rendering would be '0 m from the anchor'."""
    state = BoatState(clock=clock)
    fix = AnchorFix(latitude=36.8312, longitude=10.3034, radius_m=40, set_at=NOW)

    payload = U.build_payload(state, derived(), [], fix, None, None, config, now=NOW)
    assert payload["anchor"]["set"] is True
    assert payload["anchor"]["distance_m"] is None

    push(state, {"navigation.position": {"latitude": 36.8315, "longitude": 10.3034}})
    payload = U.build_payload(state, derived(), [], fix, None, None, config, now=NOW)
    assert payload["anchor"]["distance_m"] == pytest.approx(33, abs=2)


def test_the_alerts_come_through_with_their_severity(config, clock) -> None:
    alert = Alert(
        rule_id="anchor_watch_blind",
        severity=Severity.ALARM,
        message="Anchor watch is blind: the GPS has no fix, 15 min ago",
        since=NOW,
    )
    payload = U.build_payload(
        BoatState(clock=clock), derived(), [alert], None, None, None, config, now=NOW
    )
    assert payload["alerts"][0]["severity"] == "alarm"
    assert "blind" in payload["alerts"][0]["message"]


def test_the_forecast_says_why_it_is_missing_rather_than_going_blank(config, clock) -> None:
    state = BoatState(clock=clock)
    store = WeatherStore()
    store.blocked = NO_POSITION

    payload = U.build_payload(state, derived(), [], None, store, None, config, now=NOW)
    assert payload["forecast"]["summary"] == NO_POSITION
    assert payload["forecast"]["hours"] is None


def test_a_live_forecast_is_summarised_for_the_night(config, clock) -> None:
    hours = [(NOW + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(24)]
    payload_in = {
        "latitude": 36.83,
        "longitude": 10.30,
        "hourly": {
            "time": hours,
            "wind_speed_10m": [13.0] * 24,
            "wind_gusts_10m": [18.0] * 24,
            "wind_direction_10m": [0.0] * 24,
            "pressure_msl": [1013.0] * 24,
            "temperature_2m": [26.0] * 24,
        },
    }
    store = WeatherStore()
    store.record(build_forecast((36.83, 10.30), payload_in, None, now=NOW), NOW)

    out = U.build_payload(
        BoatState(clock=clock), derived(), [], None, store, None, config, now=NOW
    )
    assert out["forecast"]["hours"] == 18.0
    assert "kn" in out["forecast"]["summary"]


# ----------------------------------------------------------------- actions --


def test_arming_from_the_page_writes_the_same_file_the_cli_writes(config) -> None:
    actions = U.Actions(config)
    ok, message = actions.anchor_down((36.83, 10.30), 45.0)

    assert ok, message
    fix = AnchorFile(config.anchor_file).read()
    assert fix is not None
    assert fix.radius_m == 45.0
    assert fix.latitude == pytest.approx(36.83)


def test_arming_with_no_fix_is_refused_rather_than_guessed(config) -> None:
    ok, message = U.Actions(config).anchor_down(None, 40.0)
    assert not ok
    assert "no position" in message
    assert not config.anchor_file.exists()


def test_a_silly_circle_is_refused(config) -> None:
    actions = U.Actions(config)
    assert not actions.anchor_down((36.83, 10.30), 1.0)[0]
    assert not actions.anchor_down((36.83, 10.30), 5000.0)[0]


def test_clearing_the_watch_is_safe_to_repeat(config) -> None:
    actions = U.Actions(config)
    actions.anchor_down((36.83, 10.30), 40.0)

    assert actions.anchor_up()[0]
    assert actions.anchor_up()[0]  # already clear, still fine
    assert AnchorFile(config.anchor_file).read() is None


def test_a_hush_always_has_an_expiry_and_a_ceiling(config) -> None:
    actions = U.Actions(config)

    assert not actions.hush_for(0)[0]
    assert not actions.hush_for(U.MAX_HUSH_MINUTES + 1)[0]

    ok, message = actions.hush_for(30)
    assert ok and "quiet until" in message
    assert actions.unhush()[0]


def test_a_silence_is_offered_to_the_page_and_lifted_from_it(config) -> None:
    actions = U.Actions(config)

    ok, message = actions.silence_all()
    assert ok and "no alarm will reach you" in message
    assert actions.silence.active() is not None

    # Asking twice must not restart the clock the nag is built on.
    again, said = actions.silence_all()
    assert again and "already silenced" in said

    ok, message = actions.unsilence()
    assert ok and "back on" in message
    assert actions.silence.active() is None
    assert actions.unsilence()[0], "already on is not an error"


def test_the_payload_carries_the_silence_so_the_page_can_say_so(config, clock) -> None:
    """A silenced boat draws a perfectly calm page unless this is in it."""
    from agent.silence import silence_now

    state = BoatState(clock=clock)
    plain = U.build_payload(state, derived(), [], None, None, None, config, now=NOW)
    assert plain["silence"] is None

    held = silence_now("laid up", now=NOW - timedelta(hours=3))
    quiet = U.build_payload(
        state, derived(), [], None, None, None, config, now=NOW, silence=held
    )
    assert quiet["silence"]["held_for"] == "3 h"
    assert quiet["silence"]["note"] == "laid up"
    assert "SILENCED" in quiet["silence"]["text"]


# ------------------------------------------------------------------ server --


def serve(config: Config):
    dashboard = U.Dashboard(config)
    server = U.UIServer(dashboard)
    assert server.start()
    port = server._server.server_address[1]
    return dashboard, server, f"http://127.0.0.1:{port}"


def get(url: str, token: str = "") -> tuple[int, dict]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Boat-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def post(url: str, body: dict, token: str = "") -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    if token:
        request.add_header("X-Boat-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_the_page_and_the_status_are_served(config, clock) -> None:
    dashboard, server, base = serve(config)
    try:
        dashboard.publish(
            U.build_payload(
                BoatState(clock=clock), derived(), [], None, None, None, config, now=NOW
            )
        )
        code, body = get(f"{base}/api/status")
        assert code == 200
        assert body["state"] == "anchored"

        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            page = response.read().decode()
        assert "<title>Seabird</title>" in page
    finally:
        server.stop()


def test_a_tap_arms_the_watch_over_http(config, clock) -> None:
    dashboard, server, base = serve(config)
    try:
        dashboard.publish({"fix": [36.83, 10.30]})
        code, body = post(f"{base}/api/anchor/down", {"radius_m": 55})
        assert code == 200, body
        assert AnchorFile(config.anchor_file).read().radius_m == 55

        code, _ = post(f"{base}/api/anchor/up", {})
        assert code == 200
        assert AnchorFile(config.anchor_file).read() is None
    finally:
        server.stop()


def test_a_tap_silences_every_channel_and_another_gives_them_back(config, clock) -> None:
    from agent.silence import SilenceFile

    _dashboard, server, base = serve(config)
    try:
        code, body = post(f"{base}/api/silence", {})
        assert code == 200, body
        assert SilenceFile(config.silence_file).active() is not None

        code, body = post(f"{base}/api/unsilence", {})
        assert code == 200, body
        assert SilenceFile(config.silence_file).active() is None
    finally:
        server.stop()


def test_without_the_token_nothing_is_readable_or_clickable(config, clock) -> None:
    guarded = replace(config, ui_token="a-real-secret")
    dashboard, server, base = serve(guarded)
    try:
        dashboard.publish({"fix": [36.83, 10.30]})

        assert get(f"{base}/api/status")[0] == 401
        assert post(f"{base}/api/anchor/down", {"radius_m": 40})[0] == 401
        assert not guarded.anchor_file.exists()

        assert get(f"{base}/api/status", token="a-real-secret")[0] == 200
        assert post(f"{base}/api/anchor/down", {"radius_m": 40}, token="a-real-secret")[0] == 200
    finally:
        server.stop()


def test_the_token_also_works_in_the_url_so_a_bookmark_keeps_working(config) -> None:
    guarded = replace(config, ui_token="a-real-secret")
    dashboard, server, base = serve(guarded)
    try:
        dashboard.publish({"state": "anchored"})
        assert get(f"{base}/api/status?t=a-real-secret")[0] == 200
        assert get(f"{base}/api/status?t=wrong")[0] == 401
    finally:
        server.stop()


def test_rubbish_is_refused_rather_than_crashing_the_thread(config) -> None:
    dashboard, server, base = serve(config)
    try:
        dashboard.publish({"fix": [36.83, 10.30]})
        assert post(f"{base}/api/anchor/down", {"radius_m": "the big one"})[0] == 400
        assert get(f"{base}/api/nonsense")[0] == 404

        # And the server is still up afterwards.
        assert get(f"{base}/api/status")[0] == 200
    finally:
        server.stop()


def test_the_ui_is_off_unless_asked_for() -> None:
    assert U.build_ui(Config()) is None
    assert U.build_ui(Config(ui=True)) is not None

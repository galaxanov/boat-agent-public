"""What the swing plot and the wind trace are drawn from.

The picture is the point of the page: a number cannot say "the boat has swung
through a quarter circle since midnight and the hook has not moved" as fast as
a shape can. These are the figures that shape has to be honest about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent import ui as U
from agent.anchor import AnchorFix
from agent.config import Config
from agent.derived import Confidence, Derived, VesselState
from agent.geo import bearing_deg, offsets_m
from agent.state import BoatState
from agent.units import format_position_ddm
from agent.weather import WeatherStore, build_forecast

NOW = datetime(2026, 9, 8, 21, 0, tzinfo=UTC)
HOOK = (36.83120, 10.30340)


def derived() -> Derived:
    return Derived(
        vessel=VesselState.ANCHORED, confidence=Confidence.LIKELY, reason="in the circle"
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        anchor_file=tmp_path / "a.json",
        hush_file=tmp_path / "h.json",
        silence_file=tmp_path / "s.json",
    )


# ----------------------------------------------------------------- geometry --


def test_north_and_east_come_out_where_a_chart_would_put_them() -> None:
    """A tenth of a minute of latitude is about 185 m; longitude is shorter by
    the cosine of the latitude, and at 37N that is a real difference."""
    north = offsets_m(HOOK, (HOOK[0] + 0.001, HOOK[1]))
    assert north[0] == pytest.approx(0, abs=0.5)
    assert north[1] == pytest.approx(111.2, abs=1)

    east = offsets_m(HOOK, (HOOK[0], HOOK[1] + 0.001))
    assert east[1] == pytest.approx(0, abs=0.5)
    assert east[0] == pytest.approx(88.9, abs=1)  # shorter, as it must be


def test_the_bearing_is_true_and_clockwise_from_north() -> None:
    assert bearing_deg(HOOK, (HOOK[0] + 0.001, HOOK[1])) == pytest.approx(0, abs=0.5)
    assert bearing_deg(HOOK, (HOOK[0], HOOK[1] + 0.001)) == pytest.approx(90, abs=0.5)
    assert bearing_deg(HOOK, (HOOK[0] - 0.001, HOOK[1])) == pytest.approx(180, abs=0.5)
    assert bearing_deg(HOOK, (HOOK[0], HOOK[1] - 0.001)) == pytest.approx(270, abs=0.5)


def test_sitting_on_the_anchor_has_no_bearing_rather_than_a_made_up_one() -> None:
    assert bearing_deg(HOOK, HOOK) is None


def test_a_position_reads_the_way_a_plotter_shows_it() -> None:
    """Degrees and decimal minutes. Decimal degrees is for machines."""
    # Written with escapes so the prime and degree marks survive any editor.
    expected = "36\u00b0 49.872\u2032 N  010\u00b0 18.204\u2032 E"
    assert format_position_ddm({"latitude": 36.8312, "longitude": 10.3034}) == expected
    assert "W" in format_position_ddm({"latitude": 38.7223, "longitude": -9.1393})
    assert format_position_ddm(None) is None


# -------------------------------------------------------------------- track --


def test_the_track_thins_itself_rather_than_drawing_gps_noise() -> None:
    track = U.Track(gap_s=20.0)
    for second in range(0, 60, 5):
        track.add(HOOK, NOW + timedelta(seconds=second))

    # Twelve fixes offered at five seconds apart, kept at twenty.
    assert len(track.offsets(HOOK, NOW + timedelta(minutes=1))) == 3


def test_the_track_forgets_the_far_end_of_the_window() -> None:
    track = U.Track(window_s=600.0, gap_s=20.0)
    for minute in range(0, 30):
        track.add(HOOK, NOW + timedelta(minutes=minute))

    kept = track.offsets(HOOK, NOW + timedelta(minutes=29))
    assert kept
    assert max(point[2] for point in kept) <= 600


def test_a_missing_fix_leaves_a_gap_rather_than_a_repeat() -> None:
    """A boat with no GPS is not a boat sitting perfectly still."""
    track = U.Track(gap_s=0.0)
    track.add(HOOK, NOW)
    track.add(None, NOW + timedelta(seconds=30))
    assert len(track.offsets(HOOK, NOW + timedelta(minutes=1))) == 1


def test_moving_the_anchor_moves_the_whole_track_with_it() -> None:
    """Stored as positions, not offsets: re-laying the hook must not leave the
    old track drawn around a point the boat was never near."""
    track = U.Track(gap_s=0.0)
    track.add((HOOK[0] + 0.0005, HOOK[1]), NOW)

    near = track.offsets(HOOK, NOW)[0]
    far = track.offsets((HOOK[0] + 0.0005, HOOK[1]), NOW)[0]
    assert near[1] == pytest.approx(55.6, abs=1)
    assert far[1] == pytest.approx(0, abs=0.5)


def test_no_anchor_means_nothing_to_draw_a_track_around() -> None:
    track = U.Track(gap_s=0.0)
    track.add(HOOK, NOW)
    assert track.offsets(None, NOW) == []


# ------------------------------------------------------------------ payload --


def test_the_plot_gets_the_anchor_the_boat_and_the_track(config, clock) -> None:
    state = BoatState(clock=clock)
    state.apply_delta(
        {
            "updates": [
                {
                    "$source": "test",
                    "values": [
                        {
                            "path": "navigation.position",
                            "value": {"latitude": HOOK[0] + 0.0003, "longitude": HOOK[1]},
                        }
                    ],
                }
            ]
        }
    )
    track = U.Track(gap_s=0.0)
    track.add((HOOK[0] + 0.0002, HOOK[1]), clock.now)

    fix = AnchorFix(latitude=HOOK[0], longitude=HOOK[1], radius_m=40, set_at=NOW)
    payload = U.build_payload(
        state, derived(), [], fix, None, None, config, now=clock.now, track=track
    )

    anchor = payload["anchor"]
    assert anchor["east_north"][1] == pytest.approx(33.4, abs=1)
    assert anchor["bearing_deg"] == pytest.approx(0, abs=1)
    assert len(anchor["track"]) == 1
    assert payload["position_ddm"].startswith("36°")


def test_with_no_anchor_the_plot_is_given_nothing_to_draw(config, clock) -> None:
    payload = U.build_payload(
        BoatState(clock=clock), derived(), [], None, None, None, config, now=clock.now
    )
    assert payload["anchor"] == {"set": False}


# -------------------------------------------------------------- wind trace --


def hourly(winds, gusts, start=NOW):
    times = [(start + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(len(winds))]
    return {
        "latitude": 36.83,
        "longitude": 10.30,
        "hourly": {
            "time": times,
            "wind_speed_10m": winds,
            "wind_gusts_10m": gusts,
            "wind_direction_10m": [0.0] * len(winds),
            "pressure_msl": [1013.0] * len(winds),
            "temperature_2m": [26.0] * len(winds),
        },
        "daily": {
            "time": ["2026-09-08"],
            "sunrise": ["2026-09-09T03:30"],
            "sunset": ["2026-09-08T17:00"],
        },
    }


def test_the_trace_is_placed_in_hours_from_now_not_timestamps() -> None:
    """The x axis answers "how long have I got", which is the only question."""
    forecast = build_forecast((36.83, 10.30), hourly([5.0] * 6, [8.0] * 6), None, now=NOW)
    series = forecast.series(6, NOW)

    assert [point["h"] for point in series] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert series[0]["wind"] == 5.0 and series[0]["gust"] == 8.0


def test_a_missing_hour_stays_missing_in_the_trace() -> None:
    forecast = build_forecast((36.83, 10.30), hourly([5.0, None, 7.0], [8.0] * 3), None, now=NOW)
    assert [point["wind"] for point in forecast.series(3, NOW)] == [5.0, None, 7.0]


def test_the_dark_is_shaded_including_the_night_already_under_way() -> None:
    """Somebody reading this at 2100 is standing in a night that has no sunset
    ahead of it, and that is exactly the night that matters."""
    forecast = build_forecast((36.83, 10.30), hourly([5.0] * 12, [8.0] * 12), None, now=NOW)

    # 21:00 now, sunrise 03:30, sunset 17:00 was already past.
    spans = forecast.dark_spans(12, NOW)
    assert spans[0][0] == 0.0
    assert spans[0][1] == pytest.approx(6.5, abs=0.1)


def test_no_sun_times_means_no_shading_rather_than_a_guess() -> None:
    payload = hourly([5.0] * 6, [8.0] * 6)
    del payload["daily"]
    forecast = build_forecast((36.83, 10.30), payload, None, now=NOW)
    assert forecast.dark_spans(12, NOW) == []


# --------------------------------------------------------------- sea state --


def test_the_trace_carries_direction_waves_and_period() -> None:
    """Height without period is half the story, and direction at anchor is
    most of the other half."""
    payload = hourly([12.0] * 4, [16.0] * 4)
    payload["hourly"]["wind_direction_10m"] = [10.0, 20.0, 30.0, 40.0]
    marine = {
        "hourly": {
            "time": payload["hourly"]["time"],
            "wave_height": [0.8, 1.2, 1.9, 2.1],
            "wave_period": [4.4, 4.6, 4.9, 5.0],
        }
    }
    forecast = build_forecast((36.83, 10.30), payload, marine, now=NOW)
    first, last = forecast.series(4, NOW)[0], forecast.series(4, NOW)[-1]

    assert first["dir"] == 10 and last["dir"] == 40
    assert last["wave"] == 2.1 and last["period"] == 5.0


def test_a_short_steep_sea_is_named_as_one(config, clock) -> None:
    """Two metres at nine seconds is a swell you sleep through. Two at five is
    what empties a cove at three in the morning."""
    payload = hourly([12.0] * 4, [16.0] * 4)
    steep = {"hourly": {"time": payload["hourly"]["time"],
                        "wave_height": [2.1] * 4, "wave_period": [5.0] * 4}}
    easy = {"hourly": {"time": payload["hourly"]["time"],
                       "wave_height": [2.1] * 4, "wave_period": [9.5] * 4}}

    store = WeatherStore()
    store.record(build_forecast((36.83, 10.30), payload, steep, now=NOW), NOW)
    sea = U.build_payload(
        BoatState(clock=clock), derived(), [], None, store, None, config, now=NOW
    )["forecast"]["sea"]
    assert sea["height_m"] == 2.1 and sea["period_s"] == 5.0 and sea["steep"] is True

    store.record(build_forecast((36.83, 10.30), payload, easy, now=NOW), NOW)
    assert U.build_payload(
        BoatState(clock=clock), derived(), [], None, store, None, config, now=NOW
    )["forecast"]["sea"]["steep"] is False


def test_no_wave_data_means_no_sea_line_rather_than_a_zero(config, clock) -> None:
    """Inland, or a point the wave models do not cover."""
    store = WeatherStore()
    store.record(
        build_forecast((36.83, 10.30), hourly([12.0] * 4, [16.0] * 4), None, now=NOW), NOW
    )
    out = U.build_payload(
        BoatState(clock=clock), derived(), [], None, store, None, config, now=NOW
    )
    assert out["forecast"]["sea"] is None


def test_how_long_the_dark_has_left_to_run() -> None:
    """Sunrise 03:30, and it is 21:00: six and a half hours of dark to sit."""
    forecast = build_forecast((36.83, 10.30), hourly([5.0] * 12, [8.0] * 12), None, now=NOW)
    assert forecast.dark_for(NOW) == pytest.approx(6.5, abs=0.1)


# -------------------------------------------------------------- excursion --


def test_the_furthest_the_boat_has_been_since_the_hook_went_down() -> None:
    """No half-hour plot can show this, and it is what says whether the circle
    is big enough."""
    track = U.Track()
    set_at = NOW
    track.watch(HOOK, set_at, (HOOK[0] + 0.0004, HOOK[1]), NOW)          # ~44 m
    track.watch(HOOK, set_at, HOOK, NOW + timedelta(minutes=5))          # back on top
    assert track.furthest_m == pytest.approx(44.5, abs=1)


def test_re_laying_the_anchor_starts_the_measurement_again() -> None:
    """A furthest measured from where the hook used to be is worse than none."""
    track = U.Track()
    track.watch(HOOK, NOW, (HOOK[0] + 0.0004, HOOK[1]), NOW)
    assert track.furthest_m > 40

    later = NOW + timedelta(minutes=20)
    track.watch(HOOK, later, HOOK, later)  # same spot, set again
    assert track.furthest_m == 0.0
    assert track.watching_since == later


def test_weighing_the_anchor_ends_the_measurement() -> None:
    track = U.Track()
    track.watch(HOOK, NOW, (HOOK[0] + 0.0004, HOOK[1]), NOW)
    track.watch(None, None, HOOK, NOW + timedelta(minutes=1))
    assert track.furthest_m == 0.0
    assert track.watching_since is None

"""The forecast: parsing it, ageing it, and the rule that reads it.

Nothing here touches the network. fetch_forecast() takes a getter, and every
test hands it a recorded payload in the shape Open-Meteo actually returns -
including the shapes that break things: a marine request that fails on its
own, an unreadable timestamp, a null in the middle of a column.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from agent import rules as R
from agent import weather as W
from agent.state import BoatState

ANCHORAGE = (36.83, 10.30)
START = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def hourly(start: datetime, count: int) -> list[str]:
    return [
        (start + timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M") for n in range(count)
    ]


def weather_payload(
    winds: list[float | None],
    gusts: list[float | None] | None = None,
    directions: list[float | None] | None = None,
    start: datetime = START,
    grid: tuple[float, float] = ANCHORAGE,
) -> dict:
    count = len(winds)
    return {
        "latitude": grid[0],
        "longitude": grid[1],
        "hourly_units": {"wind_speed_10m": "m/s"},
        "hourly": {
            "time": hourly(start, count),
            "wind_speed_10m": winds,
            "wind_gusts_10m": gusts if gusts is not None else [None] * count,
            "wind_direction_10m": directions if directions is not None else [0.0] * count,
            "pressure_msl": [1013.0] * count,
            "temperature_2m": [26.0] * count,
        },
    }


def marine_payload(waves: list[float | None], start: datetime = START) -> dict:
    return {
        "latitude": ANCHORAGE[0],
        "longitude": ANCHORAGE[1],
        "hourly": {
            "time": hourly(start, len(waves)),
            "wave_height": waves,
            "wave_period": [5.0] * len(waves),
        },
    }


def getter(weather: dict, marine: dict | None = None):
    """Answer the two URLs from canned payloads, and nothing else."""

    def get(url: str):
        if url.startswith(W.MARINE_URL):
            if marine is None:
                raise W.WeatherError("HTTP 400: no marine data for this point")
            return marine
        return weather

    return get


# ----------------------------------------------------------------- parsing --


def test_units_are_converted_to_si() -> None:
    payload = weather_payload([10.0], gusts=[15.0], directions=[90.0])
    forecast = W.build_forecast(ANCHORAGE, payload, None, now=START)

    hour = forecast.hours[0]
    assert hour.wind_ms == 10.0  # asked for m/s, so nothing to do
    assert hour.gust_ms == 15.0
    assert hour.direction_rad == pytest.approx(math.pi / 2)  # 90 degrees
    assert hour.pressure_pa == pytest.approx(101300.0)  # hPa -> Pa
    assert hour.air_temp_k == pytest.approx(299.15)  # C -> K


def test_waves_are_matched_by_timestamp_not_by_position() -> None:
    """The marine grid is a separate request and may not start in the same hour."""
    weather = weather_payload([8.0, 9.0, 10.0])
    marine = marine_payload([0.5, 0.9], start=START + timedelta(hours=1))

    forecast = W.build_forecast(ANCHORAGE, weather, marine, now=START)
    assert [h.wave_m for h in forecast.hours] == [None, 0.5, 0.9]
    assert forecast.waves is True


def test_a_missing_marine_payload_is_a_partial_forecast_not_a_failure() -> None:
    forecast = W.fetch_forecast(
        ANCHORAGE, getter=getter(weather_payload([8.0, 9.0])), now=START
    )
    assert forecast.waves is False
    assert forecast.hours[0].wind_ms == 8.0


def test_nulls_in_a_column_stay_missing() -> None:
    """A gap in the model output must not become a zero."""
    forecast = W.build_forecast(ANCHORAGE, weather_payload([8.0, None, 10.0]), None, now=START)
    assert [h.wind_ms for h in forecast.hours] == [8.0, None, 10.0]
    assert "wind_ms" not in forecast.hours[1].as_dict()


def test_an_unreadable_timestamp_drops_its_row_and_keeps_the_rest() -> None:
    payload = weather_payload([8.0, 9.0, 10.0])
    payload["hourly"]["time"][1] = "not a time"

    forecast = W.build_forecast(ANCHORAGE, payload, None, now=START)
    assert [h.wind_ms for h in forecast.hours] == [8.0, 10.0]


def test_an_empty_forecast_raises() -> None:
    with pytest.raises(W.WeatherError):
        W.build_forecast(ANCHORAGE, {"hourly": {"time": []}}, None, now=START)


def test_the_grid_point_is_kept_alongside_the_one_asked_for() -> None:
    """The models snap to their own grid, and the distance can matter."""
    payload = weather_payload([8.0], grid=(36.875, 10.3375))
    forecast = W.build_forecast(ANCHORAGE, payload, None, now=START)

    assert forecast.requested == ANCHORAGE
    assert forecast.grid == (36.875, 10.3375)
    assert forecast.as_dict(START)["grid_offset_m"] > 0


# ------------------------------------------------------------------ window --


def test_current_hour_is_the_one_the_boat_is_living_in() -> None:
    forecast = W.build_forecast(ANCHORAGE, weather_payload([8.0, 9.0, 10.0]), None, now=START)

    assert forecast.current(START).wind_ms == 8.0
    assert forecast.current(START + timedelta(minutes=59)).wind_ms == 8.0
    assert forecast.current(START + timedelta(hours=1)).wind_ms == 9.0
    # Past the end of the table there is no answer, and none is invented.
    assert forecast.current(START + timedelta(days=9)) is None


def test_the_window_stops_where_the_outlook_stops() -> None:
    forecast = W.build_forecast(ANCHORAGE, weather_payload([1.0] * 24), None, now=START)
    assert len(forecast.window(12, START)) == 12
    assert len(forecast.window(12, START + timedelta(hours=20))) == 4


def test_peak_ignores_the_empty_hours() -> None:
    forecast = W.build_forecast(ANCHORAGE, weather_payload([5.0, None, 9.0]), None, now=START)
    peak = W.Forecast.peak(forecast.hours, "wind_ms")
    assert peak.wind_ms == 9.0

    quiet = W.build_forecast(ANCHORAGE, weather_payload([None, None]), None, now=START)
    assert W.Forecast.peak(quiet.hours, "wind_ms") is None


# ------------------------------------------------------------------ deltas --


def test_the_delta_carries_now_and_the_peak_of_the_outlook() -> None:
    weather = weather_payload([8.0, 20.0, 9.0], gusts=[10.0, 26.0, 11.0])
    forecast = W.build_forecast(ANCHORAGE, weather, marine_payload([0.4, 1.8, 0.9]), now=START)

    values = forecast.as_delta(START)["updates"][0]["values"]
    by_path = {item["path"]: item["value"] for item in values}

    assert by_path["environment.forecast.wind.speed"] == 8.0  # this hour
    assert by_path["environment.forecast.wind.speedMax"] == 20.0  # the outlook
    assert by_path["environment.forecast.wind.gustMax"] == 26.0
    assert by_path["environment.forecast.waves.maxHeight"] == 1.8
    assert by_path["environment.forecast.pressure"] == pytest.approx(101300.0)


def test_a_delta_with_nothing_in_it_is_not_sent() -> None:
    forecast = W.build_forecast(ANCHORAGE, weather_payload([8.0]), None, now=START)
    assert forecast.as_delta(START + timedelta(days=5)) is None


def test_the_delta_goes_into_the_state_model_like_any_other(clock) -> None:
    state = BoatState(clock=clock)
    payload = weather_payload([8.0, 20.0], start=clock.now)
    forecast = W.build_forecast(ANCHORAGE, payload, None, now=clock.now)

    state.apply_delta(forecast.as_delta(clock.now))
    assert state.value("environment.forecast.wind.speedMax") == 20.0
    assert state.get("environment.forecast.wind.speed").source == "open-meteo"


# ------------------------------------------------------------------- store --


def test_the_first_fetch_is_always_due() -> None:
    store = W.WeatherStore()
    assert store.due(ANCHORAGE, START) is True


def test_a_good_forecast_holds_off_until_the_interval_is_up() -> None:
    store = W.WeatherStore(interval=3600.0)
    store.record(W.build_forecast(ANCHORAGE, weather_payload([8.0]), None, now=START), START)

    assert store.due(ANCHORAGE, START + timedelta(minutes=30)) is False
    assert store.due(ANCHORAGE, START + timedelta(minutes=61)) is True


def test_moving_a_long_way_refetches_early() -> None:
    store = W.WeatherStore(interval=3600.0, moved_m=15_000.0)
    store.record(W.build_forecast(ANCHORAGE, weather_payload([8.0]), None, now=START), START)

    nearby = (ANCHORAGE[0] + 0.02, ANCHORAGE[1])  # about 2 km
    far = (ANCHORAGE[0] + 0.5, ANCHORAGE[1])  # about 55 km

    assert store.due(nearby, START + timedelta(minutes=5)) is False
    assert store.due(far, START + timedelta(minutes=5)) is True


def test_a_failure_backs_off_and_keeps_the_old_forecast() -> None:
    store = W.WeatherStore(interval=3600.0, retry_after=300.0)
    good = W.build_forecast(ANCHORAGE, weather_payload([8.0]), None, now=START)
    store.record(good, START)
    store.record_failure("no route to the forecast", START + timedelta(minutes=61))

    # The forecast that was there is still there. A gap is worse than an old one.
    assert store.forecast is good
    assert store.failures == 1
    assert store.last_error


def test_with_no_forecast_at_all_the_retry_is_the_short_one() -> None:
    store = W.WeatherStore(interval=3600.0, retry_after=300.0)
    store.record_failure("down", START)

    assert store.due(ANCHORAGE, START + timedelta(minutes=2)) is False
    assert store.due(ANCHORAGE, START + timedelta(minutes=6)) is True


def test_an_old_forecast_stops_counting_as_fresh() -> None:
    store = W.WeatherStore()
    store.record(W.build_forecast(ANCHORAGE, weather_payload([8.0]), None, now=START), START)

    assert store.fresh(7200.0, START + timedelta(hours=1)) is not None
    assert store.fresh(7200.0, START + timedelta(hours=3)) is None


# -------------------------------------------------------------------- rule --


@pytest.fixture
def rule_state(clock) -> BoatState:
    return BoatState(clock=clock)


def stocked(clock, winds, gusts=None, age_s: float = 0.0) -> W.WeatherStore:
    """A store holding a forecast that starts at the clock's current hour."""
    start = clock.now - timedelta(seconds=age_s)
    payload = weather_payload(winds, gusts=gusts, start=start)
    store = W.WeatherStore()
    store.record(W.build_forecast(ANCHORAGE, payload, None, now=start), start)
    return store


def fire(rule, state, clock) -> R.Alert | None:
    """Run the rule past its debounce and return whatever it raised."""
    engine = R.RuleEngine([rule], clock=clock)
    engine.evaluate(state)
    clock.advance(rule.for_seconds + 1)
    engine.evaluate(state)
    return engine.alert_for(rule.id)


def test_a_quiet_forecast_says_nothing(rule_state, clock) -> None:
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, [5.0] * 24), for_seconds=0.0)
    assert fire(rule, rule_state, clock) is None


def test_no_store_and_no_forecast_are_both_silent(rule_state, clock) -> None:
    assert fire(R.ForecastWindRule(id="wf", for_seconds=0.0), rule_state, clock) is None

    empty = W.WeatherStore()
    rule = R.ForecastWindRule(id="wf", store=empty, for_seconds=0.0)
    assert fire(rule, rule_state, clock) is None


def test_f6_in_the_outlook_raises_an_alert(rule_state, clock) -> None:
    # Quiet now, 25 knots in six hours.
    winds = [5.0] * 6 + [13.0] * 6 + [5.0] * 12
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, winds), for_seconds=0.0)

    alert = fire(rule, rule_state, clock)
    assert alert is not None
    assert alert.severity is R.Severity.ALERT
    assert "Wind building" in alert.message
    assert alert.data["lead_h"] == pytest.approx(6.0, abs=0.1)


def test_a_gale_is_a_warn_and_never_an_alarm(rule_state, clock) -> None:
    """WARN reaches a phone and a screen. ALARM would sound the siren, and a
    siren for a wind that arrives tomorrow is how a siren gets switched off."""
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, [20.0] * 24), for_seconds=0.0)

    alert = fire(rule, rule_state, clock)
    assert alert.severity is R.Severity.WARN
    assert R.SEVERITY_ORDER[alert.severity] < R.SEVERITY_ORDER[R.Severity.ALARM]
    assert "Gale forecast" in alert.message


def test_gusts_alone_can_raise_it(rule_state, clock) -> None:
    store = stocked(clock, [8.0] * 24, gusts=[18.0] * 24)
    rule = R.ForecastWindRule(id="wf", store=store, for_seconds=0.0)

    alert = fire(rule, rule_state, clock)
    assert alert is not None
    assert "gusting" in alert.message


def test_wind_beyond_the_outlook_is_not_this_rule_s_business(rule_state, clock) -> None:
    """A gale in two days is a plan, not an alert."""
    winds = [5.0] * 20 + [22.0] * 20
    rule = R.ForecastWindRule(
        id="wf", store=stocked(clock, winds), outlook_hours=12.0, for_seconds=0.0
    )
    assert fire(rule, rule_state, clock) is None


def test_a_stale_forecast_stops_being_evidence(rule_state, clock) -> None:
    store = stocked(clock, [20.0] * 48)
    rule = R.ForecastWindRule(id="wf", store=store, max_age=7200.0, for_seconds=0.0)
    assert fire(rule, rule_state, clock) is not None

    clock.advance(4 * 3600)
    aged = R.ForecastWindRule(id="wf2", store=store, max_age=7200.0)
    assert aged.check(rule_state, active=False) is None


def test_the_onset_is_reported_not_the_peak(rule_state, clock) -> None:
    """Which hour it starts is the number you plan around."""
    winds = [5.0, 5.0, 13.0, 13.0, 19.0, 13.0] + [5.0] * 18
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, winds), for_seconds=0.0)

    alert = fire(rule, rule_state, clock)
    assert alert.data["lead_h"] == pytest.approx(2.0, abs=0.1)  # onset, not the 19 m/s hour
    assert alert.data["max_wind_ms"] == 19.0


def test_hysteresis_keeps_a_borderline_forecast_from_flapping(rule_state, clock) -> None:
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, [12.2] * 24), for_seconds=0.0)
    assert rule.check(rule_state, active=False) is not None

    # Eases to just under the line. Already active, so it holds.
    rule.store = stocked(clock, [11.5] * 24)
    assert rule.check(rule_state, active=True) is not None
    # And a forecast that has genuinely dropped away lets go.
    rule.store = stocked(clock, [8.0] * 24)
    assert rule.check(rule_state, active=True) is None


def anchor_down(state: BoatState) -> None:
    """Arm the watch the way the anchor file and the Signal K plugin both do."""
    state.apply_delta(
        {
            "updates": [
                {
                    "$source": "anchor.file",
                    "values": [
                        {"path": "navigation.anchor.position", "value": {
                            "latitude": ANCHORAGE[0], "longitude": ANCHORAGE[1]}},
                        {"path": "navigation.anchor.maxRadius", "value": 35.0},
                    ],
                }
            ]
        }
    )


def test_at_anchor_the_rule_looks_all_the_way_through_the_night(rule_state, clock) -> None:
    """Anchoring at 1500, the wind that matters is the one at 0300."""
    winds = [5.0] * 14 + [16.0] * 10  # nothing for fourteen hours, then F7
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, winds), for_seconds=0.0)

    # Underway the twelve-hour question has a quiet answer.
    assert fire(rule, rule_state, clock) is None

    anchor_down(rule_state)
    alert = fire(rule, rule_state, clock)
    assert alert is not None
    assert alert.data["outlook_h"] == 18.0
    assert alert.data["anchored"] is True


def test_at_anchor_the_message_says_so(rule_state, clock) -> None:
    """Read on a phone at 2200, it has to be obvious which boat this is about."""
    anchor_down(rule_state)
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, [20.0] * 24), for_seconds=0.0)

    alert = fire(rule, rule_state, clock)
    assert alert.message.startswith("At anchor.")
    assert alert.severity is R.Severity.WARN  # still never an alarm


def test_a_weighed_anchor_puts_the_horizon_back(rule_state, clock) -> None:
    winds = [5.0] * 14 + [16.0] * 10
    rule = R.ForecastWindRule(id="wf", store=stocked(clock, winds), for_seconds=0.0)

    anchor_down(rule_state)
    assert rule.check(rule_state, active=False) is not None

    # A zero radius is how the anchor is stood down, from the file or the plugin.
    rule_state.apply_delta(
        {"updates": [{"$source": "anchor.file",
                      "values": [{"path": "navigation.anchor.maxRadius", "value": 0}]}]}
    )
    assert rule.check(rule_state, active=False) is None


def test_the_three_reasons_there_is_no_forecast_read_differently() -> None:
    """Found live: an agent restarted with the hook down said "no forecast to
    check tonight against" when the weather loop simply had not ticked yet, and
    said the same thing again when the GPS had no fix to forecast for. Those
    are three different facts and only one of them means nobody will check."""
    store = W.WeatherStore()
    assert store.explain(7200.0, START) == (
        "the forecast has not come in yet; the wind rule takes it from here"
    )

    store.blocked = W.NO_POSITION
    assert store.explain(7200.0, START) == W.NO_POSITION

    store.blocked = None
    store.record_failure("no route to the forecast", START)
    assert store.explain(7200.0, START) == "no forecast to check tonight against"

    store.record(W.build_forecast(ANCHORAGE, weather_payload([8.0]), None, now=START), START)
    assert store.explain(7200.0, START) is None

    # And an old one is no better than none, whatever got it there.
    assert store.explain(7200.0, START + timedelta(hours=3)) == (
        "no forecast to check tonight against"
    )

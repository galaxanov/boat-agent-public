"""The weather that is coming, for the position the boat is actually in.

Every other module here reports what the instruments say right now. This one
is the only part of the agent that knows anything about the future, and at
anchor that is the thing that decides whether you stay in the cove or leave at
first light.

The forecast is fetched from Open-Meteo, converted to SI, and fed into the
state model as though a plugin had published it - the same trick anchor.py
uses. Everything downstream then gets it for free: the snapshots record it, the
ship's log can quote it, and a rule can read it with the ordinary staleness
checks rather than a special case. A forecast that stops arriving ages out and
goes quiet, which is exactly the behaviour wanted.

Why Open-Meteo
--------------
Regional services often run finer wave grids than a global model, but the ones
worth wanting tend to serve observations rather than forecasts through their
APIs, want credentials arranged in advance, or ship forecast fields as NetCDF
over OPeNDAP - an account, a netCDF stack on the boat, and a grid file pulled
over a satellite link to read one point out of it.

None of that fits a machine that has to install over a satellite link and keep
running with no internet at all. Open-Meteo needs no key, no account and no
dependencies beyond urllib, and answers in about four kilobytes. It serves the
same global models - ECMWF, DWD ICON - that most regional wave models are
forced with, so the loss is mostly in the waves close inshore, where a fine grid
resolves island shadows and fetch that a 5 km model smooths over. If that ever
matters, this module is the place to add a second source: fetch() returns a
Forecast and nothing else cares where it came from.

Four constraints:

1. It is optional. No network, no forecast, and every rule that matters carries
   on untouched. Nothing here is on the alarm path.
2. It never raises into the agent loop and never blocks it: the fetch is
   urllib in a worker thread with a timeout on it.
3. It says how old it is. A forecast is a claim about the future made at a
   moment in the past, and the moment is part of the claim.
4. It is cheap. One fetch an hour, two small GETs, and a backoff when the link
   is down so a week at anchor with no sky does not hammer anything.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .geo import distance_m
from .units import cardinal, hhmm, knots

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

# Three days out. Beyond that a wind forecast is a rumour, and the point
# of this is the next night and the day after it.
FORECAST_DAYS = 3

# How far ahead a rule looks. Long enough to get the anchor up and be somewhere
# else before it arrives, short enough that the model is still worth believing.
OUTLOOK_HOURS = 12.0

# The horizon once the hook is down. The question at anchor is not the next
# twelve hours, it is tonight: a boat anchoring at 1500 wants to know about
# 0300, and a twelve-hour window does not reach it. Deliberately not worked out
# from sunset - that would need a timezone the agent has no business guessing,
# and the message names the hour anyway.
ANCHORED_OUTLOOK_HOURS = 18.0

# One fetch an hour. The models themselves update no faster than that.
DEFAULT_INTERVAL = 3600.0

# After a failed fetch, wait this long rather than the full hour: the usual
# cause is the link being down for a minute, not the forecast being gone.
RETRY_AFTER = 300.0

# Refetch early if the boat has moved this far, whatever the interval says.
# About 8 miles, which is more than a grid cell of any model behind this and
# less than a morning's sail.
MOVED_M = 15_000.0

# Starlink at anchor can be slow. Longer than this and the forecast has missed
# its slot; there will be another one along.
REQUEST_TIMEOUT = 20.0

HPA_TO_PA = 100.0
KELVIN = 273.15

HOURLY_WEATHER = (
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "pressure_msl",
    "temperature_2m",
)
HOURLY_MARINE = ("wave_height", "wave_period")

# Sunrise and sunset, so the wind graph can shade the dark. When a blow lands
# matters as much as how hard: 30 knots at noon is a decision, the same 30
# knots at 0300 is a decision taken half asleep with a torch in your teeth.
DAILY_WEATHER = ("sunrise", "sunset")


class WeatherError(Exception):
    """A forecast could not be fetched. Always non-fatal."""


# ------------------------------------------------------------------ values --


@dataclass(frozen=True)
class Hour:
    """One hour of forecast, in SI, for one point.

    Any field may be None: models disagree about what they publish, the marine
    request can fail on its own, and a missing figure has to stay missing
    rather than become a zero.
    """

    time: datetime
    wind_ms: float | None = None
    gust_ms: float | None = None
    # Radians true, the direction the wind is coming FROM, matching Signal K's
    # environment.wind.directionTrue and Open-Meteo's own convention.
    direction_rad: float | None = None
    pressure_pa: float | None = None
    air_temp_k: float | None = None
    wave_m: float | None = None
    wave_period_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"time": self.time.isoformat(timespec="minutes")}
        for key, value, digits in (
            ("wind_ms", self.wind_ms, 1),
            ("gust_ms", self.gust_ms, 1),
            ("direction_rad", self.direction_rad, 3),
            ("pressure_pa", self.pressure_pa, 0),
            ("air_temp_k", self.air_temp_k, 1),
            ("wave_m", self.wave_m, 2),
            ("wave_period_s", self.wave_period_s, 1),
        ):
            if value is not None:
                out[key] = round(value, digits)
        return out


@dataclass(frozen=True)
class Outlook:
    """The worst of a stretch of forecast, and when it starts.

    Every part of the agent that has an opinion about the weather works from
    one of these: the rule that alerts, the line in the journal, the check made
    when the anchor goes down. They differ only in how many hours they ask for,
    which is the one thing that should differ - twelve hours is the question
    underway, and the night is the question at anchor.
    """

    hours: float
    windiest: Hour | None = None
    gustiest: Hour | None = None
    roughest: Hour | None = None

    @property
    def wind_ms(self) -> float | None:
        return self.windiest.wind_ms if self.windiest is not None else None

    @property
    def gust_ms(self) -> float | None:
        return self.gustiest.gust_ms if self.gustiest is not None else None

    @property
    def wave_m(self) -> float | None:
        return self.roughest.wave_m if self.roughest is not None else None

    @property
    def empty(self) -> bool:
        return self.wind_ms is None and self.gust_ms is None

    def summary(self) -> str:
        """One clause a person can read, in knots and compass points.

        The gusts get their own clause because they need not peak in the same
        hour as the sustained wind, and joining them would invent a fact.
        """
        parts: list[str] = []
        if self.wind_ms is not None:
            point = cardinal(self.windiest.direction_rad) if self.windiest else None
            lead = f"up to {knots(self.wind_ms):.0f} kn"
            parts.append(f"{lead} from the {point}" if point else lead)
        if self.gust_ms is not None:
            parts.append(f"gusting {knots(self.gust_ms):.0f} kn")
        if self.wave_m is not None:
            parts.append(f"sea to {self.wave_m:.1f} m")
        if not parts:
            return f"nothing usable in the next {self.hours:.0f} h"
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"outlook_h": self.hours}
        if self.windiest is not None:
            out["max_wind"] = self.windiest.as_dict()
        # Only its own entry when the gusts peak in a different hour from the
        # sustained wind; otherwise it is already inside max_wind.
        if self.gustiest is not None and (
            self.windiest is None or self.gustiest.time != self.windiest.time
        ):
            out["max_gust"] = self.gustiest.as_dict()
        if self.roughest is not None:
            out["max_wave"] = self.roughest.as_dict()
        return out


@dataclass(frozen=True)
class Forecast:
    """What was fetched, where for, and when.

    `grid` is the point the model actually answered for, which is not the point
    that was asked for - the models snap to their own grid, and at 25 km that
    can put the answer on the wrong side of an island. Both are kept so the
    difference is visible in the log rather than lost in the conversion.
    """

    requested: tuple[float, float]
    grid: tuple[float, float]
    fetched_at: datetime
    hours: tuple[Hour, ...]
    waves: bool = False
    sunrise: tuple[datetime, ...] = ()
    sunset: tuple[datetime, ...] = ()

    def age(self, now: datetime) -> float:
        return (now - self.fetched_at).total_seconds()

    def current(self, now: datetime) -> Hour | None:
        """The hour the boat is living in, or None if the forecast has run out.

        Nothing is interpolated. An hourly forecast says one thing about each
        hour and pretending otherwise would be inventing precision.
        """
        for hour in self.hours:
            if hour.time <= now < hour.time + timedelta(hours=1):
                return hour
        return None

    def window(self, hours: float, now: datetime) -> tuple[Hour, ...]:
        """Every hour from the current one out to `hours` ahead."""
        end = now + timedelta(hours=hours)
        return tuple(h for h in self.hours if h.time + timedelta(hours=1) > now and h.time < end)

    @staticmethod
    def peak(hours: tuple[Hour, ...], field: str) -> Hour | None:
        """The hour with the highest value of `field`, ignoring the empty ones."""
        candidates = [h for h in hours if getattr(h, field) is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda h: getattr(h, field))

    def outlook(self, hours: float, now: datetime) -> Outlook:
        """The worst of the next `hours`. Empty when the table does not reach."""
        window = self.window(hours, now)
        return Outlook(
            hours=hours,
            windiest=self.peak(window, "wind_ms"),
            gustiest=self.peak(window, "gust_ms"),
            roughest=self.peak(window, "wave_m"),
        )

    def series(self, hours: float, now: datetime) -> list[dict[str, Any]]:
        """The window as points to draw, each placed in hours from now.

        Hours from now rather than a timestamp, because the graph's x axis is
        "how long have I got" - the only question being asked of it.
        """
        return [
            {
                "h": round((hour.time - now).total_seconds() / 3600, 2),
                "wind": None if hour.wind_ms is None else round(hour.wind_ms, 1),
                "gust": None if hour.gust_ms is None else round(hour.gust_ms, 1),
                # Where it is coming FROM, in degrees true. At anchor this is
                # the number that decides whether a cove is shelter or a lee
                # shore, and a veer matters more than another five knots.
                "dir": (
                    None
                    if hour.direction_rad is None
                    else round(math.degrees(hour.direction_rad) % 360)
                ),
                "wave": None if hour.wave_m is None else round(hour.wave_m, 2),
                # Height without period is half the story. Two metres at four
                # seconds is vicious short chop that empties a cove; two metres
                # at nine is a swell you sleep through.
                "period": None if hour.wave_period_s is None else round(hour.wave_period_s, 1),
            }
            for hour in self.window(hours, now)
        ]

    def dark_for(self, now: datetime) -> float | None:
        """Hours until it gets light, or None if it is already light.

        "Another eight hours of dark" is what decides whether you sit a blow
        out or move now while you can still see the other boats.
        """
        ahead = sorted(t for t in self.sunrise if t > now)
        if not ahead:
            return None
        down = sorted(t for t in self.sunset if t > now)
        if down and down[0] < ahead[0]:
            return None  # the sun sets before it rises: it is daytime
        return round((ahead[0] - now).total_seconds() / 3600, 1)

    def dark_spans(self, hours: float, now: datetime) -> list[list[float]]:
        """When it will be dark inside the window, in hours from now.

        Sunset to the next sunrise, clipped to the window, and including the
        night already under way - which is the one that matters, because
        somebody reading this at 2200 is standing in it. Returns [] when the
        model gave no times, rather than guessing from latitude.
        """
        spans: list[list[float]] = []

        def add(start: float, stop: float) -> None:
            start, stop = max(start, 0.0), min(stop, hours)
            if stop > start:
                spans.append([round(start, 2), round(stop, 2)])

        def to_h(when: datetime) -> float:
            return (when - now).total_seconds() / 3600

        # Already dark: the next sun event is a sunrise, so the window opens
        # in the middle of a night nobody has recorded a sunset for.
        events = sorted([(t, "up") for t in self.sunrise] + [(t, "down") for t in self.sunset])
        ahead = [event for event in events if event[0] > now]
        if ahead and ahead[0][1] == "up":
            add(0.0, to_h(ahead[0][0]))

        for down in self.sunset:
            ups = [up for up in self.sunrise if up > down]
            if ups:
                add(to_h(down), to_h(min(ups)))
        return spans

    def as_delta(self, now: datetime, source: str = "open-meteo") -> dict[str, Any] | None:
        """The forecast in the shape the bus would have sent it, or None.

        Only the current hour and the peak of the outlook go in. The whole
        72-hour table is in the logbook line next to it; the state model holds
        the two facts a rule or a status line has any use for.

        environment.forecast.* is not in the Signal K spec, which defines no
        forecast paths at all. It is an agent-derived family in the same sense
        as navigation.state, named to sit where a spec path would if there
        were one.
        """
        values: list[dict[str, Any]] = []

        def add(path: str, value: float | None) -> None:
            if value is not None:
                values.append({"path": f"environment.forecast.{path}", "value": value})

        current = self.current(now)
        if current is not None:
            add("wind.speed", current.wind_ms)
            add("wind.gust", current.gust_ms)
            add("wind.directionTrue", current.direction_rad)
            add("pressure", current.pressure_pa)
            add("airTemperature", current.air_temp_k)
            add("waves.significantHeight", current.wave_m)

        outlook = self.outlook(OUTLOOK_HOURS, now)
        add("wind.speedMax", outlook.wind_ms)
        add("wind.gustMax", outlook.gust_ms)
        add("waves.maxHeight", outlook.wave_m)

        if not values:
            return None
        return {"updates": [{"$source": source, "values": values}]}

    def as_dict(self, now: datetime, hours: float = OUTLOOK_HOURS) -> dict[str, Any]:
        """A logbook line: what it says now, and the worst of the outlook."""
        current = self.current(now)
        summary: dict[str, Any] = {
            "fetched_at": self.fetched_at.isoformat(timespec="seconds"),
            "requested": [round(self.requested[0], 4), round(self.requested[1], 4)],
            "grid": [round(self.grid[0], 4), round(self.grid[1], 4)],
            "grid_offset_m": round(distance_m(self.requested, self.grid)),
            "hours": len(self.hours),
            "waves": self.waves,
            **self.outlook(hours, now).as_dict(),
        }
        if current is not None:
            summary["now"] = current.as_dict()
        return summary

    def describe(self, now: datetime) -> str:
        """One line for the journal, in the units the crew thinks in."""
        parts: list[str] = []
        current = self.current(now)
        if current is not None and current.wind_ms is not None:
            point = cardinal(current.direction_rad)
            parts.append(f"now {knots(current.wind_ms):.0f} kn" + (f" {point}" if point else ""))
        if current is not None and current.wave_m is not None:
            parts.append(f"sea {current.wave_m:.1f} m")

        outlook = self.outlook(OUTLOOK_HOURS, now)
        if outlook.wind_ms is not None and outlook.windiest is not None:
            parts.append(
                f"peak {knots(outlook.wind_ms):.0f} kn at "
                f"{hhmm(outlook.windiest.time)}"
            )
        # Its own clause, because the gusts do not have to peak in the same
        # hour as the sustained wind and saying so would be inventing a fact.
        if outlook.gust_ms is not None:
            parts.append(f"gusts to {knots(outlook.gust_ms):.0f} kn")

        offset = distance_m(self.requested, self.grid)
        parts.append(f"grid {offset / 1000:.0f} km away")
        return ", ".join(parts) if parts else "nothing usable in the forecast"


# ----------------------------------------------------------------- fetching --

# url -> decoded JSON. A seam, so the tests never touch the network.
Getter = Callable[[str], Any]


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "boat-agent"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Open-Meteo puts the actual complaint in the body of a 400.
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            detail = f": {body.get('reason')}" if isinstance(body, dict) else ""
        except (ValueError, OSError):
            pass
        raise WeatherError(f"HTTP {exc.code}{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # The normal state at sea. Not worth a stack trace.
        raise WeatherError(f"no route to the forecast: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WeatherError(f"the forecast was not JSON: {exc}") from exc


def _url(
    base: str,
    position: tuple[float, float],
    hourly: tuple[str, ...],
    days: int,
    daily: tuple[str, ...] = (),
) -> str:
    query = urllib.parse.urlencode(
        {
            "latitude": f"{position[0]:.4f}",
            "longitude": f"{position[1]:.4f}",
            "hourly": ",".join(hourly),
            **({"daily": ",".join(daily)} if daily else {}),
            "forecast_days": days,
            # Ask for m/s so nothing downstream has to know what a knot is.
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }
    )
    return f"{base}?{query}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _times(payload: Any) -> list[datetime]:
    """Parse the hourly time column. A row with a bad time is dropped later."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    raw = hourly.get("time") if isinstance(hourly, dict) else None
    if not isinstance(raw, list):
        return []

    out: list[datetime] = []
    for item in raw:
        if not isinstance(item, str):
            out.append(datetime.min.replace(tzinfo=UTC))
            continue
        try:
            parsed = datetime.fromisoformat(item)
        except ValueError:
            out.append(datetime.min.replace(tzinfo=UTC))
            continue
        out.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    return out


def _column(payload: Any, name: str) -> list[Any]:
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    column = hourly.get(name) if isinstance(hourly, dict) else None
    return column if isinstance(column, list) else []


def _at(column: list[Any], index: int) -> float | None:
    return _number(column[index]) if index < len(column) else None


def _grid(payload: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(payload, dict):
        return fallback
    lat, lon = _number(payload.get("latitude")), _number(payload.get("longitude"))
    if lat is None or lon is None:
        return fallback
    return lat, lon


def _daily(payload: Any, name: str) -> tuple[datetime, ...]:
    """One column of the daily block, as datetimes. Anything odd is dropped."""
    daily = payload.get("daily") if isinstance(payload, dict) else None
    column = daily.get(name) if isinstance(daily, dict) else None
    if not isinstance(column, list):
        return ()

    out: list[datetime] = []
    for item in column:
        if not isinstance(item, str):
            continue
        try:
            parsed = datetime.fromisoformat(item)
        except ValueError:
            continue
        out.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    return tuple(out)


def build_forecast(
    position: tuple[float, float],
    weather: Any,
    marine: Any = None,
    now: datetime | None = None,
) -> Forecast:
    """Fold the two payloads into one hourly table. Raises if there is nothing."""
    times = _times(weather)
    if not times:
        raise WeatherError("the forecast came back with no hours in it")

    columns = {name: _column(weather, name) for name in HOURLY_WEATHER}

    # The marine payload is a separate request against a separate grid, so its
    # rows are matched by timestamp rather than by position in the list. They
    # normally line up exactly; when they do not, the waves are simply absent
    # for that hour rather than shifted onto the wrong one.
    marine_rows: dict[datetime, tuple[float | None, float | None]] = {}
    if marine is not None:
        marine_times = _times(marine)
        heights = _column(marine, "wave_height")
        periods = _column(marine, "wave_period")
        for index, stamp in enumerate(marine_times):
            marine_rows[stamp] = (_at(heights, index), _at(periods, index))

    hours: list[Hour] = []
    for index, stamp in enumerate(times):
        if stamp == datetime.min.replace(tzinfo=UTC):
            continue
        direction = _at(columns["wind_direction_10m"], index)
        pressure = _at(columns["pressure_msl"], index)
        air = _at(columns["temperature_2m"], index)
        wave, period = marine_rows.get(stamp, (None, None))
        hours.append(
            Hour(
                time=stamp,
                wind_ms=_at(columns["wind_speed_10m"], index),
                gust_ms=_at(columns["wind_gusts_10m"], index),
                direction_rad=None if direction is None else math.radians(direction % 360),
                pressure_pa=None if pressure is None else pressure * HPA_TO_PA,
                air_temp_k=None if air is None else air + KELVIN,
                wave_m=wave,
                wave_period_s=period,
            )
        )

    if not hours:
        raise WeatherError("every hour in the forecast had an unreadable timestamp")

    return Forecast(
        requested=position,
        grid=_grid(weather, position),
        fetched_at=now or datetime.now(UTC),
        hours=tuple(hours),
        waves=any(h.wave_m is not None for h in hours),
        sunrise=_daily(weather, "sunrise"),
        sunset=_daily(weather, "sunset"),
    )


def fetch_forecast(
    position: tuple[float, float],
    *,
    days: int = FORECAST_DAYS,
    timeout: float = REQUEST_TIMEOUT,
    getter: Getter | None = None,
    now: datetime | None = None,
) -> Forecast:
    """Two GETs and a fold. Blocking: call it with asyncio.to_thread."""
    get = getter or (lambda url: _get_json(url, timeout))

    weather = get(_url(FORECAST_URL, position, HOURLY_WEATHER, days, DAILY_WEATHER))

    # The wave models cover the sea and nothing else, so a boat far up a creek
    # or a position that snaps to a land cell gets wind and no waves. That is a
    # partial forecast, not a failed one.
    marine: Any = None
    try:
        marine = get(_url(MARINE_URL, position, HOURLY_MARINE, days))
    except WeatherError as exc:
        log.debug("no wave forecast for this position (%s); wind only", exc)

    return build_forecast(position, weather, marine, now=now)


# -------------------------------------------------------------------- store --


class WeatherStore:
    """The latest forecast, and an honest account of how it was got.

    One object, held by the loop that fetches and by the rule that reads. The
    rule never fetches and the loop never decides anything, which keeps the
    thing that talks to the internet away from the thing that raises alerts.
    """

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL,
        retry_after: float = RETRY_AFTER,
        moved_m: float = MOVED_M,
    ) -> None:
        self.interval = interval
        self.retry_after = retry_after
        self.moved_m = moved_m
        self.forecast: Forecast | None = None
        self.last_attempt: datetime | None = None
        self.last_error: str | None = None
        self.failures = 0
        # Why the loop could not even ask, when that is the reason rather than
        # a failed request. Set by whoever drives the fetching.
        self.blocked: str | None = None

    def due(self, position: tuple[float, float], now: datetime) -> bool:
        """Is it time to fetch again?"""
        if self.last_attempt is None:
            return True

        since = (now - self.last_attempt).total_seconds()
        if self.forecast is None:
            return since >= self.retry_after
        if since >= self.interval:
            return True
        # Moved out from under the old one. A forecast for where the boat was
        # this morning is not a forecast for where it is now.
        return distance_m(self.forecast.requested, position) >= self.moved_m

    def record(self, forecast: Forecast, now: datetime) -> None:
        self.forecast = forecast
        self.last_attempt = now
        self.last_error = None
        self.failures = 0

    def record_failure(self, reason: str, now: datetime) -> None:
        """Keep the old forecast. It ages out on its own; a gap is worse."""
        self.last_attempt = now
        self.last_error = reason
        self.failures += 1

    def fresh(self, max_age: float, now: datetime) -> Forecast | None:
        """The forecast, if it is young enough to be worth acting on."""
        if self.forecast is None:
            return None
        return self.forecast if self.forecast.age(now) <= max_age else None

    def explain(self, max_age: float, now: datetime) -> str | None:
        """Why there is no forecast, in the words the crew needs, or None.

        Three absences that look identical from outside and are not: nothing
        has been tried yet, there was nothing to try (no position), and it was
        tried and failed. Only the last one means nobody is going to check
        tonight, and only the last one should read that way.
        """
        if self.fresh(max_age, now) is not None:
            return None
        if self.blocked:
            return self.blocked
        if self.last_attempt is None:
            return "the forecast has not come in yet; the wind rule takes it from here"
        return "no forecast to check tonight against"


# What the loop sets on the store when there is nothing to ask about. A
# forecast for a guessed position is worse than none, so it does not guess.
NO_POSITION = "the boat has no position, so there is nowhere to forecast for"

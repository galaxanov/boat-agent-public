"""Small geo helpers. Signal K positions are decimal degrees, WGS84."""

from __future__ import annotations

import math
from typing import Any

EARTH_RADIUS_M = 6371008.8  # mean radius, good to ~0.3% for anchor-scale distances

# How close to 0,0 counts as "not a position at all". About 0.1 m, which is far
# inside the noise of any receiver and far outside any rounding.
NULL_ISLAND_DEG = 1e-6


def as_position(value: Any) -> tuple[float, float] | None:
    """Pull (lat, lon) out of a Signal K position value, or None if it is junk."""
    if not isinstance(value, dict):
        return None
    lat, lon = value.get("latitude"), value.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    # 0,0 is not a position, it is the shape a receiver makes when it has
    # nothing to say. Observed on this boat: gpsd regenerates NMEA from its own
    # state, and with no fix it emits
    #   $GPRMC,,V,000.0000000,S,0000.0000000,W,...
    # where the V means void and every other field is filled with zeros anyway.
    # Signal K converts that into a position of 0,0 like any other.
    #
    # Left alone it is the worst kind of bad reading, because it is not
    # obviously wrong and it is very far away: the drag rule would measure the
    # distance from the anchor to the Gulf of Guinea and raise a dragging alarm
    # every time the GPS lost its fix. A boat is not at Null Island. Nothing
    # that matters here is within 100 mm of it either.
    if abs(lat) < NULL_ISLAND_DEG and abs(lon) < NULL_ISLAND_DEG:
        return None

    return float(lat), float(lon)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres.

    Haversine rather than a flat-earth approximation - not because the error
    matters over an anchor swing, but because it costs nothing and does not
    quietly break near the poles or the antimeridian.
    """
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1

    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def offsets_m(origin: tuple[float, float], point: tuple[float, float]) -> tuple[float, float]:
    """Metres east and north of `origin`, for drawing.

    Equirectangular rather than anything cleverer, on purpose. Over an anchor
    swing - a hundred metres at the very most - the error is millimetres, and
    what the plot needs is a flat plane with north up, which is exactly what
    this gives. distance_m stays haversine because it is used for the alarm.
    """
    lat = math.radians((origin[0] + point[0]) / 2)
    east = math.radians(point[1] - origin[1]) * math.cos(lat) * EARTH_RADIUS_M
    north = math.radians(point[0] - origin[0]) * EARTH_RADIUS_M
    return east, north


def bearing_deg(origin: tuple[float, float], point: tuple[float, float]) -> float | None:
    """True bearing from origin to point, 0-360. None if they are the same spot.

    Which way the boat is lying tells you what the wind has been doing, and it
    is the first thing anyone asks after "how far".
    """
    east, north = offsets_m(origin, point)
    if abs(east) < 0.5 and abs(north) < 0.5:
        return None
    return math.degrees(math.atan2(east, north)) % 360

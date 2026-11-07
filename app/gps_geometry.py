"""Shared spherical distance; accepts point-like objects without a DB dependency."""
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_M = 6371000.0


def distance_m(first, second):
    lat1, lat2 = radians(float(first.latitude)), radians(float(second.latitude))
    dlat = lat2 - lat1
    dlon = radians(float(second.longitude) - float(first.longitude))
    hav = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(min(1.0, max(0.0, hav))))

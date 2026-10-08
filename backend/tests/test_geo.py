import math

import pytest
from geo import EARTH_RADIUS_KM, haversine_distance


# The distance between identical points must be exactly zero.
def test_zero_distance_for_same_point():
    assert haversine_distance(48.1351, 11.5820, 48.1351, 11.5820) == 0.0


# Munich to Berlin is roughly 504 km as the crow flies.
def test_known_distance_munich_berlin():
    distance = haversine_distance(48.1351, 11.5820, 52.5200, 13.4050)
    assert distance == pytest.approx(504, abs=3)


# Distance must be identical in both directions.
def test_distance_is_symmetric():
    forward = haversine_distance(48.1351, 11.5820, 50.1109, 8.6821)
    backward = haversine_distance(50.1109, 8.6821, 48.1351, 11.5820)
    assert forward == pytest.approx(backward)


# A quarter of the equator equals pi/2 times the Earth radius.
def test_quarter_equator():
    distance = haversine_distance(0.0, 0.0, 0.0, 90.0)
    assert distance == pytest.approx(math.pi / 2 * EARTH_RADIUS_KM)


# Antipodal points are half the Earth circumference apart.
def test_antipodal_points():
    distance = haversine_distance(0.0, 0.0, 0.0, 180.0)
    assert distance == pytest.approx(math.pi * EARTH_RADIUS_KM)


# One degree of latitude is about 111.2 km everywhere.
def test_one_degree_latitude():
    distance = haversine_distance(48.0, 11.0, 49.0, 11.0)
    assert distance == pytest.approx(111.19, abs=0.1)


# Crossing the antimeridian must not produce a huge detour distance.
def test_antimeridian_crossing_is_short():
    distance = haversine_distance(0.0, 179.5, 0.0, -179.5)
    assert distance == pytest.approx(111.19, abs=0.5)


# Crossing the equator and the prime meridian works with negative coordinates.
def test_negative_coordinates():
    distance = haversine_distance(-1.0, -1.0, 1.0, 1.0)
    assert distance == pytest.approx(314.4, abs=1)


# The distance between the poles is half the Earth circumference.
def test_pole_to_pole():
    distance = haversine_distance(90.0, 0.0, -90.0, 0.0)
    assert distance == pytest.approx(math.pi * EARTH_RADIUS_KM)


# Distance is always non-negative for arbitrary inputs.
@pytest.mark.parametrize(
    "lat1,lon1,lat2,lon2",
    [(10, 20, -30, 40), (-45, 170, 45, -170), (0, 0, 0.0001, 0.0001), (89, 10, 89, -170)],
)
def test_distance_never_negative(lat1, lon1, lat2, lon2):
    assert haversine_distance(lat1, lon1, lat2, lon2) >= 0.0
"""Adversarial external payload checks; entirely offline."""
from datetime import UTC, datetime

import pytest

from odin.tools.live_common import to_money
from odin.tools.rest_adapters import normalize_geocoding, normalize_transfers

NOW = datetime(2026, 10, 25, tzinfo=UTC)
POINTS = [{"point_id": "rail", "label": "station", "lat": 55.78, "lon": 49.09, "directions": ["transfer_in"]}]
HOTELS = [{"hotel_id": "hotel", "name": "hotel", "lat": 55.79, "lon": 49.15}]

@pytest.mark.parametrize("value", ["NaN", "nan", "sNaN", "-NaN", float('nan')])
def test_invalid_money_is_unknown_not_exception(value):
    assert to_money(value) is None

@pytest.mark.parametrize("seconds", [float('nan'), float('inf'), -float('inf'), "11", True])
def test_bad_osrm_duration_is_skipped(seconds):
    payload = {"code": "Ok", "durations": [[0, seconds], [seconds, 0]]}
    assert normalize_transfers(payload, points=POINTS, hotels=HOTELS, city="Казань", data_mode="live", retrieved_at=NOW, ttl_minutes=30) == []

@pytest.mark.parametrize("meters", [float('nan'), float('inf'), -1, True, "100"])
def test_bad_osrm_distance_is_unknown(meters):
    payload = {"code": "Ok", "durations": [[0, 600], [600, 0]], "distances": [[0, meters], [meters, 0]]}
    offers = normalize_transfers(payload, points=POINTS, hotels=HOTELS, city="Казань", data_mode="live", retrieved_at=NOW, ttl_minutes=30)
    assert len(offers) == 1
    assert offers[0].terms["distance_km"] is None

@pytest.mark.parametrize("latitude,longitude", [(float('nan'),49.1), (55.7,float('inf')), (True,49.1), (55.7,False)])
def test_bad_geocoding_coordinates_are_discarded(latitude, longitude):
    payload = {"results": [{"name": "Казань", "latitude": latitude, "longitude": longitude, "timezone": "Europe/Moscow"}]}
    assert normalize_geocoding(payload, data_mode="live", retrieved_at=NOW, query="Казань") == []

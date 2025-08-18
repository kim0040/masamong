import pytest
from utils import coords

# Sample data from the original Gist for verification
# (lat, lon, expected_x, expected_y)
SAMPLE_POINTS = [
    (37.579871128849334, 126.98935225645432, 60, 127),
    (35.101148844565955, 129.02478725562108, 97, 74),
    (33.500946412305076, 126.54663058817043, 53, 38),
]

@pytest.mark.parametrize("lat, lon, expected_x, expected_y", SAMPLE_POINTS)
def test_latlon_to_kma_grid(lat, lon, expected_x, expected_y):
    """
    Tests the conversion from latitude/longitude to KMA grid coordinates.
    """
    x, y = coords.latlon_to_kma_grid(lat, lon)
    assert x == expected_x
    assert y == expected_y

@pytest.mark.parametrize("expected_lat, expected_lon, x, y", SAMPLE_POINTS)
def test_kma_grid_to_latlon(expected_lat, expected_lon, x, y):
    """
    Tests the conversion from KMA grid coordinates back to latitude/longitude.
    """
    lat, lon = coords.kma_grid_to_latlon(x, y)
    # Use pytest.approx for floating point comparisons
    assert lat == pytest.approx(expected_lat, abs=1e-6)
    assert lon == pytest.approx(expected_lon, abs=1e-6)

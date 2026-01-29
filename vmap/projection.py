"""Equirectangular projection: WGS84 lat/lon -> drawing units (feet or meters)."""

import math

METERS_PER_DEGREE_LAT = 111_319.49  # at the equator
FEET_PER_METER = 3.28083989501312


class Projector:
    """Projects WGS84 coordinates to a local Cartesian frame.

    Origin is the center of the bounding box.  X = east, Y = north.
    """

    def __init__(self, south: float, west: float, north: float, east: float,
                 units: str = "feet"):
        self.center_lat = (south + north) / 2.0
        self.center_lon = (west + east) / 2.0
        self.cos_lat = math.cos(math.radians(self.center_lat))
        self.scale = FEET_PER_METER if units == "feet" else 1.0

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """Return (x, y) in drawing units."""
        x = (lon - self.center_lon) * METERS_PER_DEGREE_LAT * self.cos_lat * self.scale
        y = (lat - self.center_lat) * METERS_PER_DEGREE_LAT * self.scale
        return (x, y)

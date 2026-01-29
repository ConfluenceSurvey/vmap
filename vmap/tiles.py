"""Download and stitch map tiles into a single image for DXF embedding."""

import io
import math

import requests
from PIL import Image

# Tile sources the user can choose from
TILE_SOURCES = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "label": "OpenStreetMap",
        "max_zoom": 19,
    },
    "esri_satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "label": "Esri Satellite",
        "max_zoom": 18,
    },
}

HEADERS = {
    "User-Agent": "VMAP/1.0 (vicinity map generator)",
}


def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to tile x, y at the given zoom level."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def _tile_to_lat_lon(tx: int, ty: int, zoom: int) -> tuple[float, float]:
    """Return the NW corner (lat, lon) of tile (tx, ty)."""
    n = 2 ** zoom
    lon = tx / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ty / n)))
    lat = math.degrees(lat_rad)
    return lat, lon


def _pick_zoom(south: float, west: float, north: float, east: float,
               max_zoom: int, max_tiles: int = 100) -> int:
    """Pick the highest zoom level that stays under max_tiles total."""
    for z in range(max_zoom, 0, -1):
        x0, y0 = _lat_lon_to_tile(north, west, z)
        x1, y1 = _lat_lon_to_tile(south, east, z)
        nx = x1 - x0 + 1
        ny = y1 - y0 + 1
        if nx * ny <= max_tiles:
            return z
    return 1


def fetch_tile_image(south: float, west: float, north: float, east: float,
                     source: str = "esri_satellite",
                     timeout: int = 60) -> tuple[bytes, tuple[float, float, float, float]]:
    """Download tiles covering the bbox and stitch into a single PNG.

    Returns (png_bytes, (img_south, img_west, img_north, img_east)) where the
    image bounds are the exact tile boundaries (may be slightly larger than the
    requested bbox).
    """
    cfg = TILE_SOURCES[source]
    url_template = cfg["url"]
    max_zoom = cfg["max_zoom"]

    zoom = _pick_zoom(south, west, north, east, max_zoom)

    x0, y0 = _lat_lon_to_tile(north, west, zoom)   # NW corner
    x1, y1 = _lat_lon_to_tile(south, east, zoom)    # SE corner

    nx = x1 - x0 + 1
    ny = y1 - y0 + 1

    canvas = Image.new("RGB", (nx * 256, ny * 256))

    sess = requests.Session()
    sess.headers.update(HEADERS)

    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            url = url_template.format(z=zoom, x=tx, y=ty)
            try:
                resp = sess.get(url, timeout=timeout)
                resp.raise_for_status()
                tile = Image.open(io.BytesIO(resp.content))
                px = (tx - x0) * 256
                py = (ty - y0) * 256
                canvas.paste(tile, (px, py))
            except Exception:
                pass  # leave black for missing tiles

    # Compute exact geographic bounds of the stitched image
    img_north, img_west = _tile_to_lat_lon(x0, y0, zoom)
    img_south, img_east_lon = _tile_to_lat_lon(x1 + 1, y1 + 1, zoom)

    # Crop the image to the requested bbox
    # Map requested bbox to pixel coordinates within the full tile canvas
    full_width = nx * 256
    full_height = ny * 256

    # Pixel per degree
    ppd_x = full_width / (img_east_lon - img_west)
    # Latitude is non-linear in Mercator but within a small tile grid, linear approx is fine
    ppd_y = full_height / (img_north - img_south)

    crop_left = int((west - img_west) * ppd_x)
    crop_right = int((east - img_west) * ppd_x)
    crop_top = int((img_north - north) * ppd_y)
    crop_bottom = int((img_north - south) * ppd_y)

    crop_left = max(0, crop_left)
    crop_top = max(0, crop_top)
    crop_right = min(full_width, crop_right)
    crop_bottom = min(full_height, crop_bottom)

    if crop_right > crop_left and crop_bottom > crop_top:
        canvas = canvas.crop((crop_left, crop_top, crop_right, crop_bottom))
        # After cropping, the image bounds match the requested bbox exactly
        final_bounds = (south, west, north, east)
    else:
        final_bounds = (img_south, img_west, img_north, img_east_lon)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), final_bounds

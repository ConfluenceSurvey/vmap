"""Overpass API client for fetching geometry from OpenStreetMap."""

from dataclasses import dataclass, field

import math
import time
import requests

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# The OSM/Overpass usage policy requires a descriptive User-Agent that
# identifies the application. Public mirrors reject the default
# "python-requests/x.y.z" UA outright (HTTP 406) and/or rate-limit it
# aggressively (HTTP 429), which is the #1 cause of "Overpass query failed".
OVERPASS_HEADERS = {
    "User-Agent": "VMAP/1.0 Vicinity Map Generator (+https://vmap.surveybible.com)",
    "Referer": "https://vmap.surveybible.com/",
}

HIGHWAY_TYPES = [
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "residential",
    "unclassified",
]

# Road detail levels — higher = more road types included
ROAD_DETAIL_LEVELS = {
    "major": ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"],
    "moderate": ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
                 "secondary", "secondary_link", "tertiary", "tertiary_link"],
    "full": HIGHWAY_TYPES,
}

PATH_TYPES = [
    "footway", "cycleway", "path", "track",
    "pedestrian", "bridleway", "steps",
]

WATERWAY_TYPES = [
    "river", "stream", "canal", "ditch", "drain",
]

RAILWAY_TYPES = [
    "rail", "light_rail", "subway", "tram", "narrow_gauge",
]

AVAILABLE_LAYERS = [
    "roads", "buildings", "water", "railways",
    "paths", "power_lines", "landuse", "parking",
    "boundaries",
]


@dataclass
class Road:
    name: str
    highway: str
    coords: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)


@dataclass
class Feature:
    name: str
    layer: str
    feature_type: str
    coords: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    is_area: bool = False


def _build_query(bbox: str, layers: list[str], timeout: int,
                  road_detail: str = "full") -> str:
    """Build an Overpass QL query for the requested layers."""
    parts = []

    if "roads" in layers:
        road_types = ROAD_DETAIL_LEVELS.get(road_detail, HIGHWAY_TYPES)
        highway_filter = "|".join(road_types)
        parts.append(f'way["highway"~"^({highway_filter})$"]({bbox});')

    if "buildings" in layers:
        parts.append(f'way["building"]({bbox});')

    if "water" in layers:
        waterway_filter = "|".join(WATERWAY_TYPES)
        parts.append(f'way["waterway"~"^({waterway_filter})$"]({bbox});')
        parts.append(f'way["natural"="water"]({bbox});')

    if "railways" in layers:
        railway_filter = "|".join(RAILWAY_TYPES)
        parts.append(f'way["railway"~"^({railway_filter})$"]({bbox});')

    if "paths" in layers:
        path_filter = "|".join(PATH_TYPES)
        parts.append(f'way["highway"~"^({path_filter})$"]({bbox});')

    if "power_lines" in layers:
        parts.append(f'way["power"="line"]({bbox});')

    if "landuse" in layers:
        parts.append(f'way["landuse"]({bbox});')

    if "parking" in layers:
        parts.append(f'way["amenity"="parking"]({bbox});')

    if "boundaries" in layers:
        parts.append(f'way["boundary"="cadastral"]({bbox});')
        parts.append(f'way["landuse"="cadastral"]({bbox});')
        parts.append(f'relation["boundary"="cadastral"]({bbox});')

    return f"""
    [out:json][timeout:{timeout}];
    (
    {"".join(parts)}
    );
    out body;
    >;
    out skel qt;
    """


def _classify(tags: dict, layers: list[str],
              road_detail: str = "full") -> tuple[str, str, bool] | None:
    """Classify a way into (layer, feature_type, is_area) or None if no match."""
    highway = tags.get("highway", "")
    road_types = ROAD_DETAIL_LEVELS.get(road_detail, HIGHWAY_TYPES)

    if "roads" in layers and highway in road_types:
        return ("roads", highway, False)

    if "paths" in layers and highway in PATH_TYPES:
        return ("paths", highway, False)

    if "buildings" in layers and "building" in tags:
        return ("buildings", tags["building"], True)

    if "water" in layers:
        waterway = tags.get("waterway", "")
        if waterway in WATERWAY_TYPES:
            return ("water", waterway, False)
        if tags.get("natural") == "water":
            return ("water", "water", True)

    if "railways" in layers and tags.get("railway", "") in RAILWAY_TYPES:
        return ("railways", tags["railway"], False)

    if "power_lines" in layers and tags.get("power") == "line":
        return ("power_lines", "line", False)

    if "landuse" in layers and "landuse" in tags:
        return ("landuse", tags["landuse"], True)

    if "parking" in layers and tags.get("amenity") == "parking":
        return ("parking", "parking", True)

    if "boundaries" in layers:
        if tags.get("boundary") == "cadastral":
            return ("boundaries", "cadastral", True)
        if tags.get("landuse") == "cadastral":
            return ("boundaries", "cadastral", True)

    return None


def _fetch_overpass(query: str, timeout: int) -> dict:
    """Execute an Overpass query, trying multiple mirrors.

    A descriptive User-Agent is sent on every request (public mirrors reject
    the default python-requests UA). Transient failures (429 rate-limit, 5xx
    gateway errors) are retried with a short backoff before moving on to the
    next mirror, so a single busy mirror doesn't fail the whole request.
    """
    last_err = None
    for url in OVERPASS_URLS:
        for attempt in range(2):  # one retry per mirror on transient errors
            try:
                resp = requests.post(
                    url,
                    data={"data": query},
                    headers=OVERPASS_HEADERS,
                    timeout=timeout + 30,
                )
                # Retry the same mirror once on rate-limit / gateway errors.
                if resp.status_code in (429, 502, 503, 504) and attempt == 0:
                    last_err = requests.HTTPError(
                        f"{resp.status_code} from {url}", response=resp
                    )
                    time.sleep(2)
                    continue
                resp.raise_for_status()
                # Overpass sometimes returns 200 with HTML error body
                content_type = resp.headers.get("content-type", "")
                if "json" not in content_type:
                    raise ValueError(f"Overpass returned non-JSON response ({content_type})")
                return resp.json()
            except Exception as exc:
                last_err = exc
                break  # non-transient (or retry exhausted): try next mirror
    raise last_err  # type: ignore[misc]


def fetch_features(south: float, west: float, north: float, east: float,
                   layers: list[str] | None = None,
                   timeout: int = 90,
                   road_detail: str = "full") -> dict[str, list[Feature]]:
    """Fetch OSM features for the requested layers.

    Returns a dict mapping layer name to list of Feature objects.
    """
    if layers is None:
        layers = ["roads"]

    layers = [l for l in layers if l in AVAILABLE_LAYERS]
    if not layers:
        return {}

    bbox = f"{south},{west},{north},{east}"
    query = _build_query(bbox, layers, timeout, road_detail)
    data = _fetch_overpass(query, timeout)

    # Build node lookup
    nodes: dict[int, tuple[float, float]] = {}
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])

    # Classify ways into layers
    result: dict[str, list[Feature]] = {l: [] for l in layers}

    for el in data.get("elements", []):
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        classification = _classify(tags, layers, road_detail)
        if classification is None:
            continue

        layer, feature_type, is_area = classification
        name = tags.get("name", "")
        coords = []
        for nid in el.get("nodes", []):
            if nid in nodes:
                coords.append(nodes[nid])
        if len(coords) < 2:
            continue

        result[layer].append(Feature(
            name=name,
            layer=layer,
            feature_type=feature_type,
            coords=coords,
            is_area=is_area,
        ))

    return result


def _feature_signature(feat: Feature) -> tuple:
    """Stable signature for de-duplicating features merged from overlapping tiles."""
    rounded_coords = tuple((round(lat, 7), round(lon, 7)) for lat, lon in feat.coords)
    return (feat.layer, feat.feature_type, feat.name, feat.is_area, rounded_coords)


def fetch_features_tiled(
    south: float,
    west: float,
    north: float,
    east: float,
    layers: list[str] | None = None,
    timeout: int = 90,
    road_detail: str = "full",
    max_tile_area_km2: float = 20.0,
) -> dict[str, list[Feature]]:
    """Fetch OSM features by splitting the bbox into manageable tiles and merging results.

    This avoids hard-failing large selections that exceed safe single-query Overpass sizes.
    """
    if layers is None:
        layers = ["roads"]
    layers = [l for l in layers if l in AVAILABLE_LAYERS]
    if not layers:
        return {}

    lat_span = max(0.0, north - south)
    lon_span = max(0.0, east - west)
    if lat_span == 0 or lon_span == 0:
        return {l: [] for l in layers}

    # Size tiles by approximate ground dimensions to keep each Overpass request bounded.
    mid_lat_rad = math.radians((south + north) / 2.0)
    height_km = lat_span * 111.32
    width_km = lon_span * 111.32 * abs(math.cos(mid_lat_rad))
    target_side_km = max(1.0, max_tile_area_km2 ** 0.5)
    nx = max(1, int(math.ceil(width_km / target_side_km)))
    ny = max(1, int(math.ceil(height_km / target_side_km)))

    lat_step = lat_span / ny
    lon_step = lon_span / nx
    overlap = min(lat_step, lon_step) * 0.01  # 1% overlap to avoid seam misses.

    merged: dict[str, list[Feature]] = {l: [] for l in layers}
    seen: set[tuple] = set()

    for iy in range(ny):
        for ix in range(nx):
            ts = south + iy * lat_step
            tn = south + (iy + 1) * lat_step
            tw = west + ix * lon_step
            te = west + (ix + 1) * lon_step

            # Slight overlap except on outer edges.
            if iy > 0:
                ts -= overlap
            if iy < ny - 1:
                tn += overlap
            if ix > 0:
                tw -= overlap
            if ix < nx - 1:
                te += overlap

            tile_features = fetch_features(
                ts,
                tw,
                tn,
                te,
                layers=layers,
                timeout=timeout,
                road_detail=road_detail,
            )

            for layer, feats in tile_features.items():
                for feat in feats:
                    sig = _feature_signature(feat)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    merged[layer].append(feat)

    return merged


def fetch_roads(south: float, west: float, north: float, east: float,
                timeout: int = 90) -> list[Road]:
    """Query Overpass for roads within the bounding box.

    Tries multiple Overpass mirrors if the first one fails.
    """
    features = fetch_features(south, west, north, east, ["roads"], timeout)
    return [
        Road(name=f.name, highway=f.feature_type, coords=f.coords)
        for f in features.get("roads", [])
    ]

"""Overpass API client for fetching geometry from OpenStreetMap."""

from dataclasses import dataclass, field

import requests

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

HIGHWAY_TYPES = [
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "residential",
    "unclassified",
]

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


def _build_query(bbox: str, layers: list[str], timeout: int) -> str:
    """Build an Overpass QL query for the requested layers."""
    parts = []

    if "roads" in layers:
        highway_filter = "|".join(HIGHWAY_TYPES)
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


def _classify(tags: dict, layers: list[str]) -> tuple[str, str, bool] | None:
    """Classify a way into (layer, feature_type, is_area) or None if no match."""
    highway = tags.get("highway", "")

    if "roads" in layers and highway in HIGHWAY_TYPES:
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
    """Execute an Overpass query, trying multiple mirrors."""
    last_err = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, timeout=timeout + 30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_err = exc
            continue
    raise last_err  # type: ignore[misc]


def fetch_features(south: float, west: float, north: float, east: float,
                   layers: list[str] | None = None,
                   timeout: int = 90) -> dict[str, list[Feature]]:
    """Fetch OSM features for the requested layers.

    Returns a dict mapping layer name to list of Feature objects.
    """
    if layers is None:
        layers = ["roads"]

    layers = [l for l in layers if l in AVAILABLE_LAYERS]
    if not layers:
        return {}

    bbox = f"{south},{west},{north},{east}"
    query = _build_query(bbox, layers, timeout)
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
        classification = _classify(tags, layers)
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

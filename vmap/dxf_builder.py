"""DXF generation: polylines, labels, border, optional imagery, with bbox clipping."""

import math
import os

import ezdxf
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import box, LineString, Polygon, MultiLineString, MultiPolygon

from .overpass import Feature
from .projection import Projector
from .road_styles import get_style


# DXF layer names per feature layer
DXF_LAYER_NAMES = {
    "roads":       "VICINITY-ROADS",
    "buildings":   "VICINITY-BUILDINGS",
    "water":       "VICINITY-WATER",
    "railways":    "VICINITY-RAILWAYS",
    "paths":       "VICINITY-PATHS",
    "power_lines": "VICINITY-POWER",
    "landuse":     "VICINITY-LANDUSE",
    "parking":     "VICINITY-PARKING",
    "boundaries":  "VICINITY-BOUNDARIES",
}

# Styles for non-road layers: (ACI color, lineweight in hundredths of mm)
FEATURE_STYLES = {
    "buildings":   {"color": 8,   "lineweight": 13},
    "water":       {"color": 5,   "lineweight": 25},
    "railways":    {"color": 1,   "lineweight": 30},
    "paths":       {"color": 3,   "lineweight": 10},
    "power_lines": {"color": 6,   "lineweight": 15},
    "landuse":     {"color": 4,   "lineweight": 10},
    "parking":     {"color": 252, "lineweight": 10},
    "boundaries":  {"color": 2,   "lineweight": 18},
}

# Which layers get text labels for named features
LABELED_LAYERS = {"roads", "water", "railways", "landuse"}


def _midpoint_and_angle(pts: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Return (x, y, angle_deg) at the midpoint of a polyline."""
    total = 0.0
    lengths = []
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        seg = math.hypot(dx, dy)
        lengths.append(seg)
        total += seg

    half = total / 2.0
    accum = 0.0
    for i, seg in enumerate(lengths):
        if accum + seg >= half and seg > 0:
            t = (half - accum) / seg
            x = pts[i][0] + t * (pts[i + 1][0] - pts[i][0])
            y = pts[i][1] + t * (pts[i + 1][1] - pts[i][1])
            angle = math.degrees(math.atan2(
                pts[i + 1][1] - pts[i][1],
                pts[i + 1][0] - pts[i][0],
            ))
            if angle > 90:
                angle -= 180
            elif angle < -90:
                angle += 180
            return x, y, angle
        accum += seg

    # Fallback
    x = (pts[0][0] + pts[-1][0]) / 2
    y = (pts[0][1] + pts[-1][1]) / 2
    angle = math.degrees(math.atan2(
        pts[-1][1] - pts[0][1],
        pts[-1][0] - pts[0][0],
    ))
    if angle > 90:
        angle -= 180
    elif angle < -90:
        angle += 180
    return x, y, angle


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """Return the centroid (average x, average y) of a point list."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return cx, cy


def _clip_line(pts: list[tuple[float, float]],
               clip_box: box) -> list[list[tuple[float, float]]]:
    """Clip an open polyline to the bounding box. Returns list of line segments."""
    line = LineString(pts)
    clipped = line.intersection(clip_box)
    if clipped.is_empty:
        return []
    result = []
    if isinstance(clipped, LineString):
        coords = list(clipped.coords)
        if len(coords) >= 2:
            result.append(coords)
    elif isinstance(clipped, MultiLineString):
        for part in clipped.geoms:
            coords = list(part.coords)
            if len(coords) >= 2:
                result.append(coords)
    return result


def _clip_polygon(pts: list[tuple[float, float]],
                  clip_box: box) -> list[list[tuple[float, float]]]:
    """Clip a closed polygon to the bounding box. Returns list of polygon rings."""
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    clipped = poly.intersection(clip_box)
    if clipped.is_empty:
        return []
    result = []
    if isinstance(clipped, Polygon):
        coords = list(clipped.exterior.coords)
        if len(coords) >= 3:
            result.append(coords)
    elif isinstance(clipped, MultiPolygon):
        for part in clipped.geoms:
            coords = list(part.exterior.coords)
            if len(coords) >= 3:
                result.append(coords)
    # Also handle GeometryCollection (may contain lines from edge intersections)
    elif hasattr(clipped, 'geoms'):
        for geom in clipped.geoms:
            if isinstance(geom, Polygon):
                coords = list(geom.exterior.coords)
                if len(coords) >= 3:
                    result.append(coords)
    return result


def _add_label(msp, text: str, x: float, y: float, angle: float,
               height: float, color: int, dxf_layer: str,
               text_type: str, offset_dist: float,
               is_area: bool):
    """Add a TEXT or MTEXT label to the modelspace."""
    if is_area:
        ox, oy = x, y
        angle = 0
    else:
        perp = math.radians(angle + 90)
        ox = x + offset_dist * math.cos(perp)
        oy = y + offset_dist * math.sin(perp)

    if text_type == "mtext":
        mt = msp.add_mtext(
            text,
            dxfattribs={"layer": dxf_layer, "color": color},
        )
        mt.dxf.char_height = height
        mt.dxf.rotation = angle
        mt.dxf.insert = (ox, oy)
        mt.dxf.attachment_point = 5  # MIDDLE_CENTER
    else:
        msp.add_text(
            text,
            height=height,
            rotation=angle,
            dxfattribs={"layer": dxf_layer, "color": color},
        ).set_placement((ox, oy), align=TextEntityAlignment.MIDDLE_CENTER)


def build_dxf(features: dict[str, list[Feature]], projector: Projector,
              south: float, west: float, north: float, east: float,
              units: str = "feet",
              uppercase: bool = True,
              text_type: str = "text",
              image_path: str | None = None,
              image_bounds: tuple[float, float, float, float] | None = None,
              ) -> ezdxf.document.Drawing:
    """Create a DXF document with styled polylines, labels, border, and optional image."""
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 2 if units == "feet" else 6
    msp = doc.modelspace()

    # Create DXF layers
    border_layer = "VICINITY-BORDER"
    doc.layers.add(border_layer)
    for layer_key in features:
        dxf_name = DXF_LAYER_NAMES.get(layer_key)
        if dxf_name:
            doc.layers.add(dxf_name)

    # Corner points
    bl = projector.project(south, west)
    br = projector.project(south, east)
    tr = projector.project(north, east)
    tl = projector.project(north, west)

    # Clipping rectangle in projected coordinates
    min_x = min(bl[0], tl[0])
    max_x = max(br[0], tr[0])
    min_y = min(bl[1], br[1])
    max_y = max(tl[1], tr[1])
    clip_rect = box(min_x, min_y, max_x, max_y)

    # --- Background image (below everything) ---
    if image_path and image_bounds:
        img_layer = "VICINITY-IMAGE"
        doc.layers.add(img_layer)
        img_s, img_w, img_n, img_e = image_bounds
        img_bl = projector.project(img_s, img_w)
        img_tr = projector.project(img_n, img_e)
        img_width = abs(img_tr[0] - img_bl[0])
        img_height = abs(img_tr[1] - img_bl[1])
        if img_width > 0 and img_height > 0:
            img_def = doc.add_image_def(filename=image_path, size_in_pixel=(1, 1))
            msp.add_image(
                insert=img_bl,
                size_in_units=(img_width, img_height),
                image_def=img_def,
                rotation=0,
                dxfattribs={"layer": img_layer},
            )

    # Border rectangle
    border_pts = [bl, br, tr, tl, bl]
    msp.add_lwpolyline(
        border_pts,
        dxfattribs={"layer": border_layer, "color": 7, "lineweight": 50},
    )

    # Label sizing
    extent_x = abs(tr[0] - bl[0])
    extent_y = abs(tr[1] - bl[1])
    label_height = min(extent_x, extent_y) / 80.0
    offset_dist = label_height * 1.0

    # Spatial grid for label deduplication
    grid_size = label_height * 8
    placed_labels: set[tuple[str, int, int]] = set()

    # --- Roads (special: per-highway-type styles) ---
    for feat in features.get("roads", []):
        style = get_style(feat.feature_type)
        pts = [projector.project(lat, lon) for lat, lon in feat.coords]
        if len(pts) < 2:
            continue

        dxf_layer = DXF_LAYER_NAMES["roads"]
        clipped_lines = _clip_line(pts, clip_rect)
        for seg in clipped_lines:
            msp.add_lwpolyline(
                seg,
                dxfattribs={
                    "layer": dxf_layer,
                    "color": style.color,
                    "lineweight": style.lineweight,
                },
            )

        if not feat.name or not clipped_lines:
            continue

        # Label on the longest clipped segment
        longest = max(clipped_lines, key=lambda s: sum(
            math.hypot(s[i+1][0]-s[i][0], s[i+1][1]-s[i][1])
            for i in range(len(s)-1)
        ))
        mx, my, angle = _midpoint_and_angle(longest)
        gx = int(mx / grid_size) if grid_size else 0
        gy = int(my / grid_size) if grid_size else 0
        key = (feat.name, gx, gy)
        if key in placed_labels:
            continue
        placed_labels.add(key)

        label_text = feat.name.upper() if uppercase else feat.name
        _add_label(msp, label_text, mx, my, angle, label_height,
                   style.color, dxf_layer, text_type, offset_dist,
                   is_area=False)

    # --- All other layers ---
    for layer_key in ["buildings", "water", "railways", "paths",
                      "power_lines", "landuse", "parking", "boundaries"]:
        layer_features = features.get(layer_key, [])
        if not layer_features:
            continue

        dxf_layer = DXF_LAYER_NAMES[layer_key]
        style_info = FEATURE_STYLES[layer_key]
        color = style_info["color"]
        lineweight = style_info["lineweight"]
        do_label = layer_key in LABELED_LAYERS

        for feat in layer_features:
            pts = [projector.project(lat, lon) for lat, lon in feat.coords]
            if len(pts) < 2:
                continue

            if feat.is_area:
                # Close polygon if needed before clipping
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                clipped_parts = _clip_polygon(pts, clip_rect)
                for ring in clipped_parts:
                    msp.add_lwpolyline(
                        ring,
                        dxfattribs={
                            "layer": dxf_layer,
                            "color": color,
                            "lineweight": lineweight,
                        },
                    )
            else:
                clipped_parts = _clip_line(pts, clip_rect)
                for seg in clipped_parts:
                    msp.add_lwpolyline(
                        seg,
                        dxfattribs={
                            "layer": dxf_layer,
                            "color": color,
                            "lineweight": lineweight,
                        },
                    )

            if not do_label or not feat.name or not clipped_parts:
                continue

            if feat.is_area:
                cx, cy = _centroid(clipped_parts[0])
                mx, my, angle = cx, cy, 0.0
            else:
                longest = max(clipped_parts, key=lambda s: sum(
                    math.hypot(s[i+1][0]-s[i][0], s[i+1][1]-s[i][1])
                    for i in range(len(s)-1)
                ))
                mx, my, angle = _midpoint_and_angle(longest)

            gx = int(mx / grid_size) if grid_size else 0
            gy = int(my / grid_size) if grid_size else 0
            key = (feat.name, gx, gy)
            if key in placed_labels:
                continue
            placed_labels.add(key)

            label_text = feat.name.upper() if uppercase else feat.name
            _add_label(msp, label_text, mx, my, angle, label_height,
                       color, dxf_layer, text_type, offset_dist,
                       is_area=feat.is_area)

    return doc

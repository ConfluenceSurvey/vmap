"""Flask application with /api/generate endpoint."""

import os
import io
import math
import zipfile
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_file

from .overpass import fetch_features, AVAILABLE_LAYERS
from .projection import Projector
from .dxf_builder import build_dxf
from .tiles import fetch_tile_image, TILE_SOURCES

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")

os.makedirs(OUTPUT_DIR, exist_ok=True)

MAX_AREA_KM2 = 25.0


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/test-tiles")
def test_tiles():
    """Debug endpoint to test tile fetching."""
    try:
        from .tiles import fetch_tile_image
        # Small test area (Golden Gate Bridge)
        png_bytes, bounds = fetch_tile_image(37.81, -122.48, 37.82, -122.47, source="esri_satellite")
        return jsonify({
            "success": True,
            "image_size_bytes": len(png_bytes),
            "bounds": bounds
        })
    except Exception as e:
        import traceback
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True)
    try:
        south = float(data["south"])
        west = float(data["west"])
        north = float(data["north"])
        east = float(data["east"])
        units = data.get("units", "feet")
        uppercase = bool(data.get("uppercase", True))
        text_type = data.get("text_type", "text")
        layers = data.get("layers", ["roads"])
        imagery = data.get("imagery", "none")
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": f"Invalid parameters: {exc}"}), 400

    if units not in ("feet", "meters"):
        return jsonify({"error": "units must be 'feet' or 'meters'"}), 400

    if text_type not in ("text", "mtext"):
        return jsonify({"error": "text_type must be 'text' or 'mtext'"}), 400

    if imagery not in ("none", *TILE_SOURCES.keys()):
        return jsonify({"error": f"imagery must be 'none' or one of {list(TILE_SOURCES.keys())}"}), 400

    if not isinstance(layers, list) or not layers:
        return jsonify({"error": "layers must be a non-empty list"}), 400

    invalid = [l for l in layers if l not in AVAILABLE_LAYERS]
    if invalid:
        return jsonify({"error": f"Unknown layers: {invalid}"}), 400

    # Rough area check
    lat_mid = math.radians((south + north) / 2)
    height_km = (north - south) * 111.32
    width_km = (east - west) * 111.32 * math.cos(lat_mid)
    area = abs(height_km * width_km)
    if area > MAX_AREA_KM2:
        return jsonify({"error": f"Selected area ~{area:.1f} km² exceeds {MAX_AREA_KM2} km² limit."}), 400

    # Fetch features
    try:
        features = fetch_features(south, west, north, east, layers)
    except Exception as exc:
        return jsonify({"error": f"Overpass query failed: {exc}"}), 502

    total = sum(len(v) for v in features.values())
    if total == 0 and imagery == "none":
        return jsonify({"error": "No features found in the selected area."}), 404

    # Fetch background imagery if requested
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    image_filename = None
    image_bytes = None
    image_bounds = None

    if imagery != "none":
        try:
            image_bytes, image_bounds = fetch_tile_image(
                south, west, north, east, source=imagery)
            # Use relative filename for DXF reference
            image_filename = f"vicinity_bg_{ts}.png"
        except Exception as exc:
            return jsonify({"error": f"Tile download failed: {exc}"}), 502

    # Build DXF with relative image path (just filename, not full path)
    projector = Projector(south, west, north, east, units)
    doc = build_dxf(features, projector, south, west, north, east,
                    units, uppercase, text_type,
                    image_path=image_filename, image_bounds=image_bounds)

    # Save DXF to memory (ezdxf.write requires a text stream)
    dxf_stream = io.StringIO()
    doc.write(dxf_stream)
    dxf_bytes = dxf_stream.getvalue().encode('utf-8')
    dxf_filename = f"vicinity_map_{ts}.dxf"

    # If no imagery, return just the DXF
    if image_bytes is None:
        return send_file(
            io.BytesIO(dxf_bytes),
            download_name=dxf_filename,
            as_attachment=True,
            mimetype="application/dxf"
        )

    # With imagery, return a ZIP containing both DXF and PNG
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(dxf_filename, dxf_bytes)
        zf.writestr(image_filename, image_bytes)

    zip_buffer.seek(0)
    zip_filename = f"vicinity_map_{ts}.zip"

    return send_file(
        zip_buffer,
        download_name=zip_filename,
        as_attachment=True,
        mimetype="application/zip"
    )

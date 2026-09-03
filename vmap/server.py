"""Flask application with an async /api/generate job queue.

Exports run in background threads. The browser POSTs to /api/generate, gets a
job id back immediately (202), polls /api/jobs/<id> every couple of seconds,
then downloads from /api/jobs/<id>/download once the job is done. That keeps
every HTTP request short, so neither the gunicorn worker timeout nor the
Cloudflare 100 s proxy limit can kill a long Overpass fetch mid-flight.
"""

import hashlib
import io
import logging
import math
import os
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_file

from .overpass import fetch_features, fetch_features_tiled, AVAILABLE_LAYERS
from .projection import Projector
from .dxf_builder import build_dxf
from .tiles import fetch_tile_image, TILE_SOURCES

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging. One key=value line per page visit and per export so Render's log
# stream doubles as a usage record (distinct ip= values per day = daily users).
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("vmap")
# ezdxf logs every dictionary it creates at INFO; keep the stream readable.
logging.getLogger("ezdxf").setLevel(logging.WARNING)

_IP_SALT = os.environ.get("VMAP_LOG_SALT", "vmap")


def _client_ip() -> str:
    # Cloudflare sits in front of Render; CF-Connecting-IP is the real client.
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "-"
    )


def _ip_hash(ip: str) -> str:
    """Short salted hash so logs can count distinct users without storing IPs."""
    return hashlib.sha256(f"{_IP_SALT}|{ip}".encode()).hexdigest()[:12]


def _client_meta() -> dict:
    ip = _client_ip()
    return {
        "ip": _ip_hash(ip),
        "country": request.headers.get("CF-IPCountry", "-"),
        "ua": request.headers.get("User-Agent", "-")[:160],
        "ref": request.headers.get("Referer", "-"),
    }


def _kv(**fields) -> str:
    parts = []
    for k, v in fields.items():
        if isinstance(v, float):
            v = f"{v:.2f}"
        s = str(v)
        if " " in s or '"' in s or s == "":
            s = '"' + s.replace('"', "'") + '"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Export limits
# ---------------------------------------------------------------------------
# Single Overpass queries are reliable up to roughly this area.
DIRECT_QUERY_MAX_AREA_KM2 = 25.0
# Safety brake to avoid pathological requests that can exhaust memory/time.
MAX_EXPORT_AREA_KM2 = 2000.0
LARGE_AREA_PROFILES = {
    # Larger tiles and shorter timeout = faster overall, but more likely to miss sparse edge cases.
    "fast": {"max_tile_area_km2": DIRECT_QUERY_MAX_AREA_KM2 * 1.4, "timeout": 75},
    "balanced": {"max_tile_area_km2": DIRECT_QUERY_MAX_AREA_KM2 * 0.8, "timeout": 90},
    # Smaller tiles and longer timeout = most resilient for difficult extracts.
    "detailed": {"max_tile_area_km2": DIRECT_QUERY_MAX_AREA_KM2 * 0.45, "timeout": 120},
}


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------
JOB_TTL_SECONDS = 15 * 60          # finished results are kept this long
MAX_JOB_WORKERS = 2                 # concurrent Overpass fetches
POLL_INTERVAL_MS = 2000


class GenerationError(Exception):
    """A user-facing failure with an HTTP status to report via the job API."""

    def __init__(self, message: str, http_status: int):
        super().__init__(message)
        self.http_status = http_status


@dataclass
class Job:
    id: str
    params: dict
    client: dict
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    status: str = "queued"          # queued | running | done | error
    error: str | None = None
    http_status: int = 200
    result: bytes | None = None
    filename: str | None = None
    mimetype: str | None = None
    feature_count: int = 0


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=MAX_JOB_WORKERS, thread_name_prefix="vmap-job")


def _purge_expired_jobs(now: float | None = None) -> None:
    now = now or time.time()
    with _jobs_lock:
        expired = [
            jid for jid, j in _jobs.items()
            if j.finished is not None and now - j.finished > JOB_TTL_SECONDS
        ]
        for jid in expired:
            del _jobs[jid]


def _get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _run_export(params: dict) -> tuple[bytes, str, str, int]:
    """Run the full export pipeline. Returns (bytes, filename, mimetype, feature_count)."""
    south, west, north, east = params["south"], params["west"], params["north"], params["east"]
    layers = params["layers"]
    imagery = params["imagery"]
    area = params["area_km2"]

    # Fetch features. For large areas, auto-tile and merge instead of hard-failing.
    try:
        if area > DIRECT_QUERY_MAX_AREA_KM2:
            profile = LARGE_AREA_PROFILES[params["large_area_mode"]]
            features = fetch_features_tiled(
                south, west, north, east,
                layers=layers,
                timeout=profile["timeout"],
                road_detail=params["road_detail"],
                max_tile_area_km2=profile["max_tile_area_km2"],
            )
        else:
            features = fetch_features(south, west, north, east, layers,
                                      road_detail=params["road_detail"])
    except Exception as exc:
        raise GenerationError(f"Overpass query failed: {exc}", 502) from exc

    total = sum(len(v) for v in features.values())
    if total == 0 and imagery == "none":
        raise GenerationError("No features found in the selected area.", 404)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    image_filename = None
    image_bytes = None
    image_bounds = None

    if imagery != "none":
        try:
            image_bytes, image_bounds = fetch_tile_image(
                south, west, north, east, source=imagery)
            # Relative filename so the DXF IMAGE reference resolves next to the PNG.
            image_filename = f"vicinity_bg_{ts}.png"
        except Exception as exc:
            raise GenerationError(f"Tile download failed: {exc}", 502) from exc

    try:
        projector = Projector(south, west, north, east, params["units"])
        doc = build_dxf(features, projector, south, west, north, east,
                        params["units"], params["uppercase"], params["text_type"],
                        show_labels=params["show_labels"],
                        image_path=image_filename, image_bounds=image_bounds)
        # ezdxf.write requires a text stream
        dxf_stream = io.StringIO()
        doc.write(dxf_stream)
        dxf_bytes = dxf_stream.getvalue().encode("utf-8")
    except Exception as exc:
        log.exception("dxf_build_failed")
        raise GenerationError(f"DXF generation failed: {exc}", 500) from exc

    dxf_filename = f"vicinity_map_{ts}.dxf"

    if image_bytes is None:
        return dxf_bytes, dxf_filename, "application/dxf", total

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(dxf_filename, dxf_bytes)
        zf.writestr(image_filename, image_bytes)
    return zip_buffer.getvalue(), f"vicinity_map_{ts}.zip", "application/zip", total


def _run_job(job: Job) -> None:
    job.started = time.time()
    job.status = "running"
    try:
        data, filename, mimetype, count = _run_export(job.params)
        job.result, job.filename, job.mimetype, job.feature_count = data, filename, mimetype, count
        job.status = "done"
    except GenerationError as exc:
        job.status, job.error, job.http_status = "error", str(exc), exc.http_status
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.exception("job_crashed job=%s", job.id)
        job.status, job.error, job.http_status = "error", f"Unexpected error: {exc}", 500
    finally:
        job.finished = time.time()
        log.info(_kv(
            event="export_done",
            job=job.id,
            status=job.status,
            http=job.http_status,
            dur_s=job.finished - job.started,
            queued_s=job.started - job.created,
            features=job.feature_count,
            bytes=len(job.result) if job.result else 0,
            err=job.error or "-",
        ))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    log.info(_kv(event="visit", **_client_meta()))
    return app.send_static_file("index.html")


@app.route("/api/test-tiles")
def test_tiles():
    """Debug endpoint to test tile fetching."""
    try:
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


def _validate_params(data) -> dict:
    """Validate the request body. Raises GenerationError(…, 400) on bad input."""
    if not isinstance(data, dict):
        raise GenerationError("Invalid parameters: JSON object expected", 400)
    try:
        params = {
            "south": float(data["south"]),
            "west": float(data["west"]),
            "north": float(data["north"]),
            "east": float(data["east"]),
            "units": data.get("units", "feet"),
            "uppercase": bool(data.get("uppercase", True)),
            "text_type": data.get("text_type", "text"),
            "layers": data.get("layers", ["roads"]),
            "imagery": data.get("imagery", "none"),
            "road_detail": data.get("road_detail", "full"),
            "show_labels": bool(data.get("show_labels", True)),
            "large_area_mode": data.get("large_area_mode", "balanced"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationError(f"Invalid parameters: {exc}", 400) from exc

    if params["units"] not in ("feet", "meters"):
        raise GenerationError("units must be 'feet' or 'meters'", 400)
    if params["text_type"] not in ("text", "mtext"):
        raise GenerationError("text_type must be 'text' or 'mtext'", 400)
    if params["imagery"] not in ("none", *TILE_SOURCES.keys()):
        raise GenerationError(
            f"imagery must be 'none' or one of {list(TILE_SOURCES.keys())}", 400)
    if params["road_detail"] not in ("major", "moderate", "full"):
        raise GenerationError("road_detail must be 'major', 'moderate', or 'full'", 400)
    if params["large_area_mode"] not in LARGE_AREA_PROFILES:
        raise GenerationError(
            f"large_area_mode must be one of {list(LARGE_AREA_PROFILES.keys())}", 400)

    layers = params["layers"]
    if not isinstance(layers, list) or not layers:
        raise GenerationError("layers must be a non-empty list", 400)
    invalid = [l for l in layers if l not in AVAILABLE_LAYERS]
    if invalid:
        raise GenerationError(f"Unknown layers: {invalid}", 400)

    # Rough area check
    lat_mid = math.radians((params["south"] + params["north"]) / 2)
    height_km = (params["north"] - params["south"]) * 111.32
    width_km = (params["east"] - params["west"]) * 111.32 * math.cos(lat_mid)
    area = abs(height_km * width_km)
    if area > MAX_EXPORT_AREA_KM2:
        raise GenerationError(
            f"Selected area ~{area:.1f} km² exceeds {MAX_EXPORT_AREA_KM2} km² maximum export area.",
            400)
    params["area_km2"] = area
    return params


@app.route("/api/generate", methods=["POST"])
def generate():
    """Queue an export. Returns 202 with a job id; poll /api/jobs/<id>."""
    try:
        params = _validate_params(request.get_json(force=True, silent=True))
    except GenerationError as exc:
        return jsonify({"error": str(exc)}), exc.http_status

    _purge_expired_jobs()
    job = Job(id=uuid.uuid4().hex, params=params, client=_client_meta())
    with _jobs_lock:
        _jobs[job.id] = job

    log.info(_kv(
        event="export_start",
        job=job.id,
        area_km2=params["area_km2"],
        layers=",".join(params["layers"]),
        imagery=params["imagery"],
        detail=params["road_detail"],
        mode=params["large_area_mode"] if params["area_km2"] > DIRECT_QUERY_MAX_AREA_KM2 else "direct",
        units=params["units"],
        **job.client,
    ))
    _executor.submit(_run_job, job)

    return jsonify({
        "job_id": job.id,
        "status": job.status,
        "status_url": f"/api/jobs/{job.id}",
        "poll_ms": POLL_INTERVAL_MS,
    }), 202


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"error": "Unknown or expired job."}), 404
    body = {
        "job_id": job.id,
        "status": job.status,
        "elapsed_s": round(time.time() - job.created, 1),
    }
    if job.status == "done":
        body["download_url"] = f"/api/jobs/{job.id}/download"
        body["filename"] = job.filename
        body["features"] = job.feature_count
    elif job.status == "error":
        body["error"] = job.error
        body["http_status"] = job.http_status
    return jsonify(body)


@app.route("/api/jobs/<job_id>/download")
def job_download(job_id):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"error": "Unknown or expired job."}), 404
    if job.status != "done" or job.result is None:
        return jsonify({"error": f"Job is {job.status}, nothing to download."}), 409
    return send_file(
        io.BytesIO(job.result),
        download_name=job.filename,
        as_attachment=True,
        mimetype=job.mimetype,
    )

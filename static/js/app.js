// VMAP - Leaflet map with rectangle drawing and DXF generation

const map = L.map("map").setView([39.5, -98.35], 5);

// Base layers for preview
const baseLayers = {
    "OpenStreetMap": L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "&copy; OpenStreetMap contributors",
        maxZoom: 19,
    }),
    "Esri Satellite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        attribution: "&copy; Esri",
        maxZoom: 18,
    }),
    "Esri Topo": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", {
        attribution: "&copy; Esri",
        maxZoom: 18,
    }),
};

// Add default layer
baseLayers["OpenStreetMap"].addTo(map);

// Add layer control to top-right
L.control.layers(baseLayers, null, { position: "topright" }).addTo(map);

// Drawing layer
const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
    draw: {
        rectangle: {
            shapeOptions: {
                color: "#2563eb",
                weight: 2,
                fillOpacity: 0.1,
            },
        },
        polygon: false,
        polyline: false,
        circle: false,
        marker: false,
        circlemarker: false,
    },
    edit: {
        featureGroup: drawnItems,
        remove: true,
    },
});
map.addControl(drawControl);

let currentBounds = null;

const btnGenerate = document.getElementById("btn-generate");
const boundsInfo = document.getElementById("bounds-info");
const statusEl = document.getElementById("status");

function updateBoundsDisplay(bounds) {
    document.getElementById("val-north").textContent = bounds.getNorth().toFixed(6);
    document.getElementById("val-south").textContent = bounds.getSouth().toFixed(6);
    document.getElementById("val-east").textContent = bounds.getEast().toFixed(6);
    document.getElementById("val-west").textContent = bounds.getWest().toFixed(6);
    boundsInfo.style.display = "block";
}

function setStatus(msg, type) {
    statusEl.textContent = msg;
    statusEl.className = "status " + (type || "");
}

map.on(L.Draw.Event.CREATED, function (e) {
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    currentBounds = e.layer.getBounds();
    updateBoundsDisplay(currentBounds);
    btnGenerate.disabled = false;
    setStatus("");
});

map.on(L.Draw.Event.DELETED, function () {
    currentBounds = null;
    boundsInfo.style.display = "none";
    btnGenerate.disabled = true;
    setStatus("");
});

map.on(L.Draw.Event.EDITED, function () {
    const layers = drawnItems.getLayers();
    if (layers.length > 0) {
        currentBounds = layers[0].getBounds();
        updateBoundsDisplay(currentBounds);
    }
});

// Extract a readable message from a failed response (JSON error, short
// text, or a generic status line for opaque proxy error pages).
async function readError(resp) {
    let errMsg = `Server error ${resp.status}`;
    try {
        const err = await resp.json();
        errMsg = err.error || errMsg;
    } catch {
        const text = await resp.text().catch(() => "");
        if (text.length < 200) errMsg = text || errMsg;
    }
    return errMsg;
}

async function postJson(url, body) {
    const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(await readError(resp));
    return resp.json();
}

const JOB_MAX_WAIT_MS = 15 * 60 * 1000;

// Poll /api/jobs/<id> until done or error. Transient network failures on a
// single poll are retried rather than failing the whole export.
async function pollJob(job, onTick) {
    const intervalMs = job.poll_ms || 2000;
    const deadline = Date.now() + JOB_MAX_WAIT_MS;
    let consecutiveFailures = 0;
    while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, intervalMs));
        let state;
        try {
            const resp = await fetch(job.status_url, { cache: "no-store" });
            if (resp.status === 404) throw new Error(await readError(resp));
            if (!resp.ok) {
                if (++consecutiveFailures >= 5) throw new Error(await readError(resp));
                continue;
            }
            state = await resp.json();
            consecutiveFailures = 0;
        } catch (err) {
            if (err.message.startsWith("Unknown or expired job")) throw err;
            if (++consecutiveFailures >= 5) throw err;
            continue;
        }
        if (state.status === "done") return state;
        if (state.status === "error") throw new Error(state.error || "Export failed.");
        if (onTick) onTick(state);
    }
    throw new Error("Export timed out. Try a smaller area or the 'fast' large-area mode.");
}

btnGenerate.addEventListener("click", async function () {
    if (!currentBounds) return;

    const units = document.getElementById("units").value;
    const showLabels = document.getElementById("show-labels").checked;
    const uppercase = document.getElementById("uppercase").checked;
    const textType = document.getElementById("text-type").value;
    const layers = [...document.querySelectorAll('input[name="layer"]:checked')].map(el => el.value);
    const imagery = document.getElementById("imagery").value;
    const roadDetail = document.getElementById("road-detail").value;
    const largeAreaMode = document.getElementById("large-area-mode").value;

    if (layers.length === 0 && imagery === "none") {
        setStatus("Select at least one layer or background imagery.", "error");
        return;
    }

    // Ensure at least one layer is sent (server requires non-empty list)
    const effectiveLayers = layers.length > 0 ? layers : ["roads"];

    const latMid = ((currentBounds.getNorth() + currentBounds.getSouth()) / 2) * Math.PI / 180;
    const heightKm = (currentBounds.getNorth() - currentBounds.getSouth()) * 111.32;
    const widthKm = (currentBounds.getEast() - currentBounds.getWest()) * 111.32 * Math.cos(latMid);
    const areaKm2 = Math.abs(heightKm * widthKm);
    const isLargeSelection = areaKm2 > 25;

    const payload = {
        south: currentBounds.getSouth(),
        west: currentBounds.getWest(),
        north: currentBounds.getNorth(),
        east: currentBounds.getEast(),
        units: units,
        show_labels: showLabels,
        uppercase: uppercase,
        text_type: textType,
        layers: effectiveLayers,
        imagery: imagery,
        road_detail: roadDetail,
        large_area_mode: largeAreaMode,
    };

    btnGenerate.disabled = true;
    const largeHint = isLargeSelection ? ` (large area: ${largeAreaMode} tiled fetch)` : "";
    const statusMsg = imagery !== "none"
        ? `Fetching features and imagery…${largeHint}`
        : `Fetching features and generating DXF…${largeHint}`;
    setStatus(statusMsg, "loading");

    try {
        // Queue the export, then poll. Each request stays short so proxy /
        // worker timeouts can't kill a long Overpass fetch mid-flight.
        const job = await postJson("/api/generate", payload);
        const startedAt = Date.now();
        const baseMsg = statusMsg;

        const done = await pollJob(job, function (state) {
            const secs = Math.round((Date.now() - startedAt) / 1000);
            const phase = state.status === "queued" ? "Queued" : "Working";
            setStatus(`${baseMsg} ${phase}, ${secs}s elapsed.`, "loading");
        });

        // Download the file
        const resp = await fetch(done.download_url);
        if (!resp.ok) throw new Error(await readError(resp));
        const blob = await resp.blob();
        const disposition = resp.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?(.+?)"?$/);
        const filename = match ? match[1] : (done.filename || (imagery !== "none" ? "vicinity_map.zip" : "vicinity_map.dxf"));

        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);

        const successMsg = imagery !== "none"
            ? "ZIP downloaded! Extract both files to the same folder, then open the DXF."
            : "DXF generated and downloaded.";
        setStatus(successMsg, "success");
    } catch (err) {
        setStatus(err.message, "error");
    } finally {
        btnGenerate.disabled = false;
    }
});

// VMAP - Leaflet map with rectangle drawing and DXF generation

const map = L.map("map").setView([39.5, -98.35], 5);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
}).addTo(map);

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

btnGenerate.addEventListener("click", async function () {
    if (!currentBounds) return;

    const units = document.getElementById("units").value;
    const uppercase = document.getElementById("uppercase").checked;
    const textType = document.getElementById("text-type").value;
    const layers = [...document.querySelectorAll('input[name="layer"]:checked')].map(el => el.value);
    const imagery = document.getElementById("imagery").value;

    if (layers.length === 0 && imagery === "none") {
        setStatus("Select at least one layer or background imagery.", "error");
        return;
    }

    // Ensure at least one layer is sent (server requires non-empty list)
    const effectiveLayers = layers.length > 0 ? layers : ["roads"];

    const payload = {
        south: currentBounds.getSouth(),
        west: currentBounds.getWest(),
        north: currentBounds.getNorth(),
        east: currentBounds.getEast(),
        units: units,
        uppercase: uppercase,
        text_type: textType,
        layers: effectiveLayers,
        imagery: imagery,
    };

    btnGenerate.disabled = true;
    const statusMsg = imagery !== "none"
        ? "Fetching features and imagery\u2026"
        : "Fetching features and generating DXF\u2026";
    setStatus(statusMsg, "loading");

    try {
        const resp = await fetch("/api/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.error || `Server error ${resp.status}`);
        }

        // Download the file
        const blob = await resp.blob();
        const disposition = resp.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?(.+?)"?$/);
        const filename = match ? match[1] : "vicinity_map.dxf";

        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);

        setStatus("DXF generated and downloaded.", "success");
    } catch (err) {
        setStatus(err.message, "error");
    } finally {
        btnGenerate.disabled = false;
    }
});

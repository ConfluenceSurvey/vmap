#!/usr/bin/env python3
"""VMAP - Vicinity Map Generator for Carlson Survey / IntelliCAD.

Starts a Flask server and opens the browser to the map UI.
"""

import os
import threading
import webbrowser

from vmap.server import app

PORT = int(os.environ.get("PORT", 5050))
HOST = os.environ.get("HOST", "127.0.0.1")


def open_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    # Only open browser in local development mode
    if HOST == "127.0.0.1" and os.environ.get("FLASK_ENV") != "production":
        threading.Timer(1.0, open_browser).start()
    app.run(host=HOST, port=PORT, debug=False)

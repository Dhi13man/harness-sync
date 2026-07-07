"""Local demo server for the state-tracking view of harness_sync.

Serves the single-page UI and streams live sync events over SSE. Localhost-only
by default — this triggers a real filesystem sync on the machine it runs on.

    python3 server.py            # http://127.0.0.1:8765
    PORT=9000 python3 server.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request

import sync_tracker

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@app.get("/")
def index() -> Response:
    return app.send_static_file("index.html")


@app.get("/api/detect")
def api_detect():
    return jsonify(sync_tracker.detect())


@app.get("/api/run")
def api_run() -> Response:
    # Validate at the boundary; the tracker trusts its inputs.
    mode = "dry" if request.args.get("mode") == "dry" else "live"
    try:
        delay_ms = int(request.args.get("delay", "400"))
    except ValueError:
        delay_ms = 400
    delay_ms = max(0, min(delay_ms, 5000))

    def generate():
        yield ": connected\n\n"
        for event in sync_tracker.run_events(mode=mode, delay_ms=delay_ms):
            yield _sse(event)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # defeat proxy buffering if one is in front
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"meta-agent-sync demo → http://{host}:{port}")
    # threaded so the SSE generator's sleeps never block other requests.
    app.run(host=host, port=port, threaded=True, debug=False)

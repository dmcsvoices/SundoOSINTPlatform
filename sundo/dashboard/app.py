from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Flask, render_template

from sundo.config import DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_STATIC

logger = logging.getLogger(__name__)

app = Flask(__name__)


def _load_graph() -> dict:
    graph_path = DASHBOARD_STATIC / "graph.json"
    if graph_path.exists():
        try:
            return json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse graph.json: %s", exc)
    return {"nodes": [], "edges": []}


@app.route("/")
def index() -> str:
    """Render the Cytoscape.js network view."""
    graph = _load_graph()
    return render_template("index.html", graph_json=json.dumps(graph))


@app.route("/api/graph")
def api_graph() -> tuple[str, int, dict[str, str]]:
    """Serve graph.json as JSON API."""
    graph = _load_graph()
    return (
        json.dumps(graph),
        200,
        {"Content-Type": "application/json"},
    )


def run_dashboard() -> None:
    """Start the Flask dashboard server."""
    DASHBOARD_STATIC.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    logger.info("Dashboard starting on %s:%d", DASHBOARD_HOST, DASHBOARD_PORT)
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    run_dashboard()

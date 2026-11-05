"""Flask application factory and REST API endpoints for SkyScry."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, render_template, request

from src import __version__
from src.db import VALID_LOCATION_TYPES, WeatherDatabase
from src.ingest.sdr_stream import SDRStream

logger = logging.getLogger(__name__)


def create_app(
    db: WeatherDatabase,
    sdr_stream: Optional[SDRStream] = None,
    status_provider: Optional[Callable[[], dict[str, Any]]] = None,
) -> Flask:
    """Create and configure the SkyScry Flask web application and REST API."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["JSON_SORT_KEYS"] = False

    start_monotonic = time.monotonic()
    started_at_iso = datetime.now(timezone.utc).isoformat()

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the single-page SkyScry WebUI dashboard."""
        return render_template("index.html", version=__version__)

    @app.route("/api/status", methods=["GET"])
    def api_status() -> Response:
        """Return daemon uptime, SDR stream status, database size, and packet totals."""
        uptime_seconds = round(time.monotonic() - start_monotonic, 1)
        db_stats = db.get_db_stats()

        sdr_status = "disconnected"
        last_error: Optional[str] = None
        frequency = "433.92M"

        if sdr_stream is not None:
            frequency = sdr_stream.config.frequency
            sdr_status = "running" if sdr_stream.is_running else "disconnected"
            last_error = sdr_stream.last_error

        extra_status = status_provider() if status_provider is not None else {}

        payload: dict[str, Any] = {
            "version": __version__,
            "started_at": started_at_iso,
            "uptime_seconds": uptime_seconds,
            "sdr_status": extra_status.get("sdr_status", sdr_status),
            "sdr_frequency": frequency,
            "sdr_last_error": extra_status.get("sdr_last_error", last_error),
            "database_path": db_stats["database_path"],
            "database_size_mb": db_stats["database_size_mb"],
            "database_size_bytes": db_stats["database_size_bytes"],
            "total_readings": db_stats["total_readings"],
            "total_sensors": db_stats["total_sensors"],
            "active_sensors": db_stats["active_sensors"],
            "earliest_timestamp": db_stats["earliest_timestamp"],
            "latest_timestamp": db_stats["latest_timestamp"],
        }
        return jsonify(payload)

    @app.route("/api/sensors", methods=["GET"])
    def api_sensors() -> Response:
        """Return all discovered 433 MHz sensors with their latest telemetry and user metadata."""
        sensors = db.get_all_sensors()
        return jsonify({"count": len(sensors), "sensors": sensors})

    @app.route("/api/sensors/<path:sensor_key>", methods=["PATCH"])
    def api_update_sensor(sensor_key: str) -> tuple[Response, int] | Response:
        """Update user metadata (`alias`, `location_type`, `is_trusted`) for a specific sensor."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Request body must be a valid JSON object."}), 400

        alias_arg: Optional[str] = None
        if "alias" in body:
            alias_arg = "" if body["alias"] is None else str(body["alias"])

        location_arg: Optional[str] = None
        if "location_type" in body and body["location_type"] is not None:
            location_arg = str(body["location_type"]).strip().lower()
            if location_arg not in VALID_LOCATION_TYPES:
                return (
                    jsonify(
                        {
                            "error": (
                                f"Invalid location_type '{location_arg}'. "
                                f"Allowed values: {', '.join(sorted(VALID_LOCATION_TYPES))}."
                            )
                        }
                    ),
                    400,
                )

        trusted_arg: Optional[bool] = None
        if "is_trusted" in body and body["is_trusted"] is not None:
            if isinstance(body["is_trusted"], bool):
                trusted_arg = body["is_trusted"]
            elif isinstance(body["is_trusted"], (int, float)):
                trusted_arg = bool(int(body["is_trusted"]) != 0)
            elif isinstance(body["is_trusted"], str):
                trusted_arg = body["is_trusted"].strip().lower() in ("1", "true", "yes")

        try:
            updated = db.update_sensor_metadata(
                sensor_key=sensor_key,
                alias=alias_arg,
                location_type=location_arg,
                is_trusted=trusted_arg,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if updated is None:
            return jsonify({"error": f"Sensor '{sensor_key}' not found."}), 404

        logger.info(
            "Updated sensor metadata for %s: alias=%r, location_type=%s, is_trusted=%s",
            sensor_key,
            updated["alias"],
            updated["location_type"],
            updated["is_trusted"],
        )
        return jsonify({"sensor": updated})

    @app.route("/api/readings", methods=["GET"])
    def api_readings() -> tuple[Response, int] | Response:
        """Query historical time-series telemetry with optional sensor_key, hours, and limit."""
        sensor_key = request.args.get("sensor_key")
        try:
            hours = float(request.args.get("hours", "24"))
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            return jsonify({"error": "Query parameters 'hours' and 'limit' must be numeric."}), 400

        readings = db.get_recent_readings(
            sensor_key=sensor_key if sensor_key else None,
            hours=hours,
            limit=limit,
        )
        return jsonify(
            {
                "count": len(readings),
                "sensor_key": sensor_key or None,
                "hours": hours,
                "limit": limit,
                "readings": readings,
            }
        )

    return app


"""Flask application factory and REST API endpoints for SkyScry (v0.0.4)."""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, render_template, request

from src import __version__
from src.db import VALID_HIDDEN_METRICS, VALID_LOCATION_TYPES, WeatherDatabase, format_utc_z
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
    started_at_iso = format_utc_z(datetime.now(timezone.utc))

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the 4-view SkyScry WebUI application."""
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

    @app.route("/api/overview", methods=["GET"])
    def api_overview() -> Response:
        """Return synthesized outdoor/indoor ensemble weather, animated condition state, and local nowcast."""
        overview = db.get_weather_overview()
        return jsonify(overview)

    @app.route("/api/charts", methods=["GET"])
    def api_charts() -> tuple[Response, int] | Response:
        """Return time-bucketed, smoothed chart series and outdoor ensemble average lines."""
        try:
            hours = float(request.args.get("hours", "24"))
        except ValueError:
            return jsonify({"error": "Query parameter 'hours' must be numeric."}), 400

        bucket_raw = request.args.get("bucket_minutes", "auto").strip().lower()
        bucket_minutes: Optional[int] = None
        if bucket_raw and bucket_raw != "auto":
            try:
                bucket_minutes = int(bucket_raw)
            except ValueError:
                return jsonify({"error": "Query parameter 'bucket_minutes' must be 'auto' or integer."}), 400

        location_filter = request.args.get("location", "outdoor").strip().lower()
        chart_data = db.get_bucketed_chart_series(
            hours=hours,
            bucket_minutes=bucket_minutes,
            location_filter=location_filter,
        )
        return jsonify(chart_data)

    @app.route("/api/sensors", methods=["GET"])
    def api_sensors() -> Response:
        """Return all discovered 433 MHz sensors in deterministic order with RF & battery lifecycle info."""
        sensors = db.get_all_sensors()
        return jsonify({"count": len(sensors), "sensors": sensors})

    @app.route("/api/sensors/<path:sensor_key>", methods=["PATCH"])
    def api_update_sensor(sensor_key: str) -> tuple[Response, int] | Response:
        """Update user metadata (`alias`, `location_type`, `is_trusted`, `hidden_metrics`, `mark_battery_replaced`)."""
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

        hidden_arg: Optional[list[str]] = None
        if "hidden_metrics" in body and body["hidden_metrics"] is not None:
            if not isinstance(body["hidden_metrics"], list):
                return jsonify({"error": "'hidden_metrics' must be a list of metric names."}), 400
            invalid = [
                str(m)
                for m in body["hidden_metrics"]
                if str(m).strip().lower() not in VALID_HIDDEN_METRICS
            ]
            if invalid:
                return (
                    jsonify(
                        {
                            "error": (
                                f"Invalid metric(s) in hidden_metrics: {invalid}. "
                                f"Allowed: {sorted(VALID_HIDDEN_METRICS)}"
                            )
                        }
                    ),
                    400,
                )
            hidden_arg = [str(m).strip().lower() for m in body["hidden_metrics"]]

        mark_battery_replaced = bool(body.get("mark_battery_replaced", False))

        try:
            updated = db.update_sensor_metadata(
                sensor_key=sensor_key,
                alias=alias_arg,
                location_type=location_arg,
                is_trusted=trusted_arg,
                hidden_metrics=hidden_arg,
                mark_battery_replaced=mark_battery_replaced,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if updated is None:
            return jsonify({"error": f"Sensor '{sensor_key}' not found."}), 404

        logger.info(
            "Updated sensor metadata for %s: alias=%r, location=%s, trusted=%s, hidden=%s, batt_reset=%s",
            sensor_key,
            updated["alias"],
            updated["location_type"],
            updated["is_trusted"],
            updated["hidden_metrics"],
            mark_battery_replaced,
        )
        return jsonify({"sensor": updated})

    @app.route("/api/readings", methods=["GET"])
    def api_readings() -> tuple[Response, int] | Response:
        """Query historical time-series telemetry with optional sensor_key, hours, limit, and CSV/JSON export."""
        sensor_key = request.args.get("sensor_key")
        export_fmt = request.args.get("format", "json").strip().lower()
        try:
            hours = float(request.args.get("hours", "24"))
            limit = int(request.args.get("limit", "250"))
        except ValueError:
            return jsonify({"error": "Query parameters 'hours' and 'limit' must be numeric."}), 400

        readings = db.get_recent_readings(
            sensor_key=sensor_key if sensor_key else None,
            hours=hours,
            limit=limit,
        )

        if export_fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "id",
                    "timestamp",
                    "sensor_key",
                    "display_name",
                    "location_type",
                    "temp_f",
                    "temp_c",
                    "dew_point_f",
                    "humidity",
                    "wind_avg_mph",
                    "wind_gust_mph",
                    "wind_cardinal",
                    "rain_cumulative_in",
                    "uv_index",
                    "lux",
                    "rssi",
                    "snr",
                    "noise",
                    "battery_ok",
                ]
            )
            for r in readings:
                writer.writerow(
                    [
                        r.get("id"),
                        r.get("timestamp"),
                        r.get("sensor_key"),
                        r.get("display_name"),
                        r.get("location_type"),
                        r.get("temp_f"),
                        r.get("temp_c"),
                        r.get("dew_point_f"),
                        r.get("humidity"),
                        r.get("wind_avg_mph"),
                        r.get("wind_gust_mph"),
                        r.get("wind_cardinal"),
                        r.get("rain_cumulative_in"),
                        r.get("uv_index"),
                        r.get("lux"),
                        r.get("rssi"),
                        r.get("snr"),
                        r.get("noise"),
                        r.get("battery_ok"),
                    ]
                )
            return Response(
                buf.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=skyscry_telemetry.csv"},
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

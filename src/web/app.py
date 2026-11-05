"""Flask application factory and REST API endpoints for SkyScry (v0.0.9)."""

from __future__ import annotations

import csv
import hmac
import io
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from src import __version__
from src.db import VALID_HIDDEN_METRICS, VALID_LOCATION_TYPES, WeatherDatabase, format_utc_z
from src.ingest.reference_weather import ReferenceWeatherService
from src.ingest.sdr_stream import SDRStream
from src.updater import get_full_update_status, trigger_manual_update

logger = logging.getLogger(__name__)


def _resolve_security_settings(
    admin_pin: Optional[str],
    read_only_public: Optional[bool],
) -> tuple[Optional[str], bool]:
    """Resolve effective ADMIN_PIN and READ_ONLY_PUBLIC from arguments or environment."""
    eff_pin = admin_pin
    if eff_pin is None:
        env_pin = os.environ.get("ADMIN_PIN")
        if env_pin and env_pin.strip():
            eff_pin = env_pin.strip()
    elif eff_pin.strip() == "":
        eff_pin = None

    if read_only_public is not None:
        eff_ro = bool(read_only_public)
    else:
        env_ro = os.environ.get("READ_ONLY_PUBLIC", "").strip().lower()
        eff_ro = env_ro in ("1", "true", "yes", "on")

    return eff_pin, eff_ro


def create_app(
    db: WeatherDatabase,
    sdr_stream: Optional[SDRStream] = None,
    ref_service: Optional[ReferenceWeatherService] = None,
    status_provider: Optional[Callable[[], dict[str, Any]]] = None,
    admin_pin: Optional[str] = None,
    read_only_public: Optional[bool] = None,
) -> Flask:
    """Create and configure the SkyScry Flask web application and REST API."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["JSON_SORT_KEYS"] = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    # Wrap WSGI app with ProxyFix so Cloudflare Tunnel (cloudflared) and reverse
    # proxies pass accurate client IPs (X-Forwarded-For), HTTPS scheme, and host.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    reference_svc = ref_service or ReferenceWeatherService(db=db)
    start_monotonic = time.monotonic()
    started_at_iso = format_utc_z(datetime.now(timezone.utc))

    # Global JSON error handlers so `/api/*` endpoints NEVER return an HTML `<!doctype html>` page
    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException) -> tuple[Response, int] | HTTPException:
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": exc.description or str(exc),
                        "code": exc.code or 500,
                    }
                ),
                int(exc.code or 500),
            )
        return exc

    @app.errorhandler(Exception)
    def handle_unhandled_exception(exc: Exception) -> tuple[Response, int]:
        logger.exception("Unhandled error on %s %s: %s", request.method, request.path, exc)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                }
            ),
            500,
        )

    def _require_write_auth(body: Optional[dict[str, Any]] = None) -> Optional[tuple[Response, int]]:
        """Verify ADMIN_PIN or READ_ONLY_PUBLIC policy before allowing write/maintenance actions."""
        eff_pin, eff_ro = _resolve_security_settings(admin_pin, read_only_public)

        if eff_pin:
            candidate = (
                request.headers.get("X-Admin-Pin")
                or (str(body.get("admin_pin")) if isinstance(body, dict) and body.get("admin_pin") is not None else None)
                or request.args.get("admin_pin")
                or ""
            ).strip()
            if not candidate or not hmac.compare_digest(candidate, eff_pin):
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "Admin PIN required to perform this action.",
                            "auth_required": True,
                        }
                    ),
                    401,
                )
            return None

        if eff_ro:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Public Read-Only mode is active (READ_ONLY_PUBLIC=true). Write actions are disabled.",
                        "read_only": True,
                    }
                ),
                403,
            )

        return None

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the 4-view SkyScry WebUI application."""
        return render_template("index.html", version=__version__)

    @app.route("/api/status", methods=["GET"])
    def api_status() -> Response:
        """Return daemon uptime, SDR stream status, database size, security state, and packet totals."""
        uptime_seconds = round(time.monotonic() - start_monotonic, 1)
        db_stats = db.get_db_stats()
        coords = db.get_location_coordinates()
        update_info = get_full_update_status()
        eff_pin, eff_ro = _resolve_security_settings(admin_pin, read_only_public)

        sdr_status = "disconnected"
        last_error: Optional[str] = None
        frequency = "433.92M"

        if sdr_stream is not None:
            frequency = sdr_stream.config.frequency
            sdr_status = "running" if sdr_stream.is_running else "disconnected"
            last_error = sdr_stream.last_error

        extra_status = status_provider() if status_provider is not None else {}

        payload: dict[str, Any] = {
            "ok": True,
            "version": __version__,
            "git_commit": update_info.get("git_commit"),
            "rtl433_version": update_info.get("rtl433_version"),
            "started_at": started_at_iso,
            "uptime_seconds": uptime_seconds,
            "sdr_status": extra_status.get("sdr_status", sdr_status),
            "sdr_frequency": frequency,
            "sdr_last_error": extra_status.get("sdr_last_error", last_error),
            "coordinates": coords,
            "auth_required": bool(eff_pin),
            "read_only_public": bool(eff_ro and not eff_pin),
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

    @app.route("/api/update-status", methods=["GET"])
    def api_update_status() -> Response:
        """Return current version, Git commit hash, and `rtl_433` binary release version."""
        return jsonify({"ok": True, **get_full_update_status()})

    @app.route("/api/trigger-update", methods=["POST"])
    def api_trigger_update() -> tuple[Response, int] | Response:
        """Check GitHub for new SkyScry commits, upgrade pip dependencies, and check `rtl_433` releases."""
        body = request.get_json(silent=True) or {}
        auth_err = _require_write_auth(body if isinstance(body, dict) else None)
        if auth_err is not None:
            return auth_err
        db.checkpoint_wal()
        result = trigger_manual_update()
        return jsonify({"ok": True, **result})

    @app.route("/api/overview", methods=["GET"])
    def api_overview() -> Response:
        """Return synthesized outdoor/indoor ensemble weather, barometer (`inHg`), and local nowcast."""
        overview = db.get_weather_overview()
        return jsonify(overview)

    @app.route("/api/charts", methods=["GET"])
    def api_charts() -> tuple[Response, int] | Response:
        """Return time-bucketed, smoothed chart series with multi-checkbox location & sensor filtering."""
        try:
            hours = float(request.args.get("hours", "24"))
        except ValueError:
            return jsonify({"ok": False, "error": "Query parameter 'hours' must be numeric."}), 400

        bucket_raw = request.args.get("bucket_minutes", "auto").strip().lower()
        bucket_minutes: Optional[int] = None
        if bucket_raw and bucket_raw != "auto":
            try:
                bucket_minutes = int(bucket_raw)
            except ValueError:
                return jsonify({"ok": False, "error": "Query parameter 'bucket_minutes' must be 'auto' or integer."}), 400

        location_filter = request.args.get("location", "outdoor").strip().lower()

        locations_param: Optional[list[str]] = None
        if "locations" in request.args:
            raw_locs = request.args.get("locations", "").strip()
            locations_param = [x.strip().lower() for x in raw_locs.split(",") if x.strip()]

        sensors_param: Optional[list[str]] = None
        if "sensors" in request.args:
            raw_sensors = request.args.get("sensors", "").strip()
            sensors_param = [x.strip() for x in raw_sensors.split(",") if x.strip()]

        include_ref = request.args.get("include_reference", "").strip().lower() in ("1", "true", "yes")
        if locations_param and "reference" in locations_param:
            include_ref = True

        chart_data = db.get_bucketed_chart_series(
            hours=hours,
            bucket_minutes=bucket_minutes,
            location_filter=location_filter,
            locations=locations_param,
            sensor_keys=sensors_param,
            include_reference=include_ref,
        )
        return jsonify(chart_data)

    @app.route("/api/sensors", methods=["GET"])
    def api_sensors() -> Response:
        """Return all discovered 433 MHz sensors in deterministic order with RF & battery lifecycle info."""
        sensors = db.get_all_sensors()
        return jsonify({"ok": True, "count": len(sensors), "sensors": sensors})

    @app.route("/api/sensors/<path:sensor_key>", methods=["POST", "PATCH"])
    def api_update_sensor(sensor_key: str) -> tuple[Response, int] | Response:
        """Update user metadata (`alias`, `location_type`, `is_trusted`, `hidden_metrics`, `mark_battery_replaced`, `purge_sensor_outliers`)."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Request body must be a valid JSON object."}), 400

        auth_err = _require_write_auth(body)
        if auth_err is not None:
            return auth_err

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
                            "ok": False,
                            "error": (
                                f"Invalid location_type '{location_arg}'. "
                                f"Allowed values: {', '.join(sorted(VALID_LOCATION_TYPES))}."
                            ),
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
                return jsonify({"ok": False, "error": "'hidden_metrics' must be a list of metric names."}), 400
            invalid = [
                str(m)
                for m in body["hidden_metrics"]
                if str(m).strip().lower() not in VALID_HIDDEN_METRICS
            ]
            if invalid:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": (
                                f"Invalid metric(s) in hidden_metrics: {invalid}. "
                                f"Allowed: {sorted(VALID_HIDDEN_METRICS)}"
                            ),
                        }
                    ),
                    400,
                )
            hidden_arg = [str(m).strip().lower() for m in body["hidden_metrics"]]

        mark_battery_replaced = bool(body.get("mark_battery_replaced", False))
        purge_sensor_outliers = bool(body.get("purge_sensor_outliers", False))

        try:
            updated = db.update_sensor_metadata(
                sensor_key=sensor_key,
                alias=alias_arg,
                location_type=location_arg,
                is_trusted=trusted_arg,
                hidden_metrics=hidden_arg,
                mark_battery_replaced=mark_battery_replaced,
                purge_sensor_outliers=purge_sensor_outliers,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.exception("Error updating sensor %s: %s", sensor_key, exc)
            return jsonify({"ok": False, "error": str(exc)}), 500

        if updated is None:
            return jsonify({"ok": False, "error": f"Sensor '{sensor_key}' not found."}), 404

        logger.info(
            "Updated sensor metadata for %s: alias=%r, location=%s, trusted=%s, hidden=%s, batt_reset=%s, purge=%s",
            sensor_key,
            updated["alias"],
            updated["location_type"],
            updated["is_trusted"],
            updated["hidden_metrics"],
            mark_battery_replaced,
            purge_sensor_outliers,
        )
        return jsonify({"ok": True, "sensor": updated})

    @app.route("/api/maintenance/purge-outliers", methods=["POST"])
    def api_purge_outliers() -> tuple[Response, int] | Response:
        """Purge skewed/corrupted historical readings, TPMS packets, and appliance probes from SQLite."""
        body = request.get_json(silent=True) or {}
        auth_err = _require_write_auth(body if isinstance(body, dict) else None)
        if auth_err is not None:
            return auth_err
        sensor_key = body.get("sensor_key") if isinstance(body, dict) else None
        result = db.expunge_skewed_history(
            target_sensor_key=str(sensor_key).strip() if sensor_key else None
        )
        return jsonify({"ok": True, "status": "ok", **result})

    @app.route("/api/maintenance/sync-reference", methods=["POST"])
    def api_sync_reference() -> tuple[Response, int] | Response:
        """Sync past 48 hours of regional barometric pressure (`inHg`/`hPa`) & weather history via Open-Meteo."""
        body = request.get_json(silent=True) or {}
        auth_err = _require_write_auth(body if isinstance(body, dict) else None)
        if auth_err is not None:
            return auth_err
        lat: Optional[float] = None
        lon: Optional[float] = None
        if isinstance(body, dict) and "latitude" in body and "longitude" in body:
            try:
                lat = float(body["latitude"])
                lon = float(body["longitude"])
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "latitude and longitude must be valid numbers."}), 400

        try:
            sync_result = reference_svc.sync_regional_weather(
                latitude=lat,
                longitude=lon,
                past_hours=48,
            )
            return jsonify({"ok": True, "status": "ok", **sync_result})
        except Exception as exc:
            logger.warning("Open-Meteo regional sync failed: %s", exc)
            return jsonify({"ok": False, "error": f"Failed to sync regional weather: {exc}"}), 502

    @app.route("/api/readings", methods=["GET"])
    def api_readings() -> tuple[Response, int] | Response:
        """Query historical time-series telemetry with optional sensor_key, hours, limit, and CSV/JSON export."""
        sensor_key = request.args.get("sensor_key")
        export_fmt = request.args.get("format", "json").strip().lower()
        try:
            hours = float(request.args.get("hours", "24"))
            limit = int(request.args.get("limit", "250"))
        except ValueError:
            return jsonify({"ok": False, "error": "Query parameters 'hours' and 'limit' must be numeric."}), 400

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
                    "pressure_inhg",
                    "pressure_hpa",
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
                        r.get("pressure_inhg"),
                        r.get("pressure_hpa"),
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
                "ok": True,
                "count": len(readings),
                "sensor_key": sensor_key or None,
                "hours": hours,
                "limit": limit,
                "readings": readings,
            }
        )

    return app

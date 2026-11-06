"""Flask application factory and REST API endpoints for SkyScry (v0.0.10)."""

from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from src import __version__
from src.analytics.climatology import ClimatologyEngine
from src.db import VALID_HIDDEN_METRICS, VALID_LOCATION_TYPES, WeatherDatabase, format_utc_z
from src.ingest.nws_service import NWSWeatherService
from src.ingest.reference_weather import ReferenceWeatherService
from src.ingest.sdr_stream import SDRStream
from src.security import (
    SecurityManager,
    hash_admin_password,
    require_admin_auth,
    verify_admin_password,
)
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
    nws_service: Optional[NWSWeatherService] = None,
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

    eff_pin, eff_ro = _resolve_security_settings(admin_pin, read_only_public)
    security_mgr = SecurityManager(db=db, fallback_pin=eff_pin, read_only_public=eff_ro)

    # Persistent 32-byte cryptographic secret_signing_key for Flask HttpOnly, SameSite='Lax' session tokens
    app.secret_key = security_mgr.ensure_secret_signing_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

    # Wrap WSGI app with ProxyFix so Cloudflare Tunnel (cloudflared) and reverse
    # proxies pass accurate client IPs (X-Forwarded-For), HTTPS scheme, and host.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    reference_svc = ref_service or ReferenceWeatherService(db=db)
    nws_svc = nws_service or NWSWeatherService(db=db)
    climatology_engine = ClimatologyEngine(db=db)
    start_monotonic = time.monotonic()
    started_at_iso = format_utc_z(datetime.now(timezone.utc))

    admin_guard = require_admin_auth(security_mgr)

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

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the 6-view SkyScry v0.0.10 WebUI application."""
        return render_template("index.html", version=__version__)

    # ==========================================================================
    # AUTHENTICATION & HARDENED SECURITY ENDPOINTS
    # ==========================================================================
    @app.route("/api/auth/status", methods=["GET"])
    def api_auth_status() -> Response:
        """Return first-run password setup status, current unlock state, and inactivity timeout."""
        return jsonify(security_mgr.get_auth_status())

    @app.route("/api/auth/setup", methods=["POST"])
    def api_auth_setup() -> tuple[Response, int] | Response:
        """First-run Admin Security Password creation (`password_configured` transitions False -> True permanently)."""
        if security_mgr.is_password_configured():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Admin password is already configured. Unlock Admin controls to change it in Settings.",
                    }
                ),
                403,
            )

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Request body must be a JSON object."}), 400

        new_pw = str(body.get("password") or "").strip()
        confirm_pw = str(body.get("confirm_password") or new_pw).strip()
        if len(new_pw) < 6:
            return jsonify({"ok": False, "error": "Admin password must be at least 6 characters long."}), 400
        if new_pw != confirm_pw:
            return jsonify({"ok": False, "error": "Passwords do not match."}), 400

        pw_hash = hash_admin_password(new_pw)
        db.set_setting("admin_password_hash", pw_hash)
        security_mgr.unlock_session()
        logger.info("First-run Admin Security Password created and encrypted in system_settings.")
        return jsonify(
            {
                "ok": True,
                "message": "Admin Security Password created and session unlocked.",
                **security_mgr.get_auth_status(),
            }
        )

    @app.route("/api/auth/unlock", methods=["POST"])
    def api_auth_unlock() -> tuple[Response, int] | Response:
        """Verify admin password with IP brute-force rate limiting and unlock session."""
        client_ip = request.remote_addr or "unknown"
        allowed, retry_after = security_mgr.rate_limiter.check_allowed(client_ip)
        if not allowed:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Too many failed unlock attempts. Locked out for {retry_after}s.",
                        "retry_after_seconds": retry_after,
                    }
                ),
                429,
            )

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Request body must be a JSON object."}), 400

        candidate = str(body.get("password") or body.get("admin_pin") or "").strip()
        if not candidate:
            return jsonify({"ok": False, "error": "Please enter the Admin Password."}), 400

        stored_hash = db.get_setting("admin_password_hash")
        valid = False
        if stored_hash and verify_admin_password(candidate, stored_hash):
            valid = True
        elif not stored_hash and eff_pin and candidate == eff_pin:
            valid = True

        if not valid:
            rem, lock_sec = security_mgr.rate_limiter.record_failure(client_ip)
            logger.warning("Failed Admin unlock attempt from IP %s (remaining=%d)", client_ip, rem)
            if lock_sec > 0:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"Maximum failed attempts reached. Temporarily locked out for {lock_sec}s.",
                            "retry_after_seconds": lock_sec,
                        }
                    ),
                    429,
                )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Invalid Admin Password ({rem} attempt(s) remaining before lockout).",
                    }
                ),
                401,
            )

        security_mgr.rate_limiter.record_success(client_ip)
        security_mgr.unlock_session()
        return jsonify(
            {
                "ok": True,
                "message": "Admin controls unlocked.",
                **security_mgr.get_auth_status(),
            }
        )

    @app.route("/api/auth/lock", methods=["POST"])
    def api_auth_lock() -> Response:
        """Immediately lock the current Admin session (Read-Only mode)."""
        security_mgr.lock_session()
        return jsonify(
            {
                "ok": True,
                "message": "Admin session locked (Read-Only).",
                **security_mgr.get_auth_status(),
            }
        )

    @app.route("/api/auth/change-password", methods=["POST"])
    @admin_guard
    def api_auth_change_password() -> tuple[Response, int] | Response:
        """Change the encrypted Admin Security Password from the Settings tab."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Request body must be a JSON object."}), 400

        current_pw = str(body.get("current_password") or "").strip()
        new_pw = str(body.get("new_password") or "").strip()
        confirm_pw = str(body.get("confirm_password") or new_pw).strip()

        stored_hash = db.get_setting("admin_password_hash")
        if stored_hash and not verify_admin_password(current_pw, stored_hash):
            return jsonify({"ok": False, "error": "Current Admin Password is incorrect."}), 401

        if len(new_pw) < 6:
            return jsonify({"ok": False, "error": "New password must be at least 6 characters long."}), 400
        if new_pw != confirm_pw:
            return jsonify({"ok": False, "error": "New passwords do not match."}), 400

        db.set_setting("admin_password_hash", hash_admin_password(new_pw))
        security_mgr.touch_admin_session()
        logger.info("Admin Security Password updated via Settings.")
        return jsonify({"ok": True, "message": "Admin password updated successfully."})

    # ==========================================================================
    # DEDICATED SETTINGS ENDPOINTS
    # ==========================================================================
    @app.route("/api/settings", methods=["GET"])
    def api_get_settings() -> Response:
        """Return non-secret station, location, unit, and data-hygiene settings."""
        return jsonify(
            {
                "ok": True,
                "settings": db.get_public_settings(),
                "auth": security_mgr.get_auth_status(),
            }
        )

    @app.route("/api/settings", methods=["POST", "PATCH"])
    @admin_guard
    def api_update_settings() -> tuple[Response, int] | Response:
        """Update station, location, unit, timeout, and data-hygiene settings (requires Admin auth)."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Request body must be a valid JSON object."}), 400

        try:
            updated = db.update_public_settings(body)
            nws_svc.invalidate_cache()
            return jsonify(
                {
                    "ok": True,
                    "message": "System settings saved to SQLite.",
                    "settings": updated,
                    "auth": security_mgr.get_auth_status(),
                }
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # ==========================================================================
    # CORE STATUS, OVERVIEW, NWS, & CLIMATOLOGY ENDPOINTS
    # ==========================================================================
    @app.route("/api/status", methods=["GET"])
    def api_status() -> Response:
        """Return daemon uptime, SDR stream status, database size, security state, and packet totals."""
        uptime_seconds = round(time.monotonic() - start_monotonic, 1)
        db_stats = db.get_db_stats()
        coords = db.get_location_coordinates()
        update_info = get_full_update_status()
        auth_state = security_mgr.get_auth_status()
        pub_settings = db.get_public_settings()

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
            "station_name": pub_settings.get("station_name"),
            "git_commit": update_info.get("git_commit"),
            "rtl433_version": update_info.get("rtl433_version"),
            "started_at": started_at_iso,
            "uptime_seconds": uptime_seconds,
            "sdr_status": extra_status.get("sdr_status", sdr_status),
            "sdr_frequency": frequency,
            "sdr_last_error": extra_status.get("sdr_last_error", last_error),
            "coordinates": coords,
            "units": {
                "temp_unit": pub_settings.get("temp_unit", "F"),
                "wind_unit": pub_settings.get("wind_unit", "mph"),
                "pressure_unit": pub_settings.get("pressure_unit", "inHg"),
                "rain_unit": pub_settings.get("rain_unit", "in"),
            },
            "auth": auth_state,
            "auth_required": not auth_state["unlocked"],
            "read_only_public": not auth_state["unlocked"],
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
    @admin_guard
    def api_trigger_update() -> tuple[Response, int] | Response:
        """Check GitHub for new SkyScry commits, upgrade pip dependencies, and check `rtl_433` releases."""
        db.checkpoint_wal()
        result = trigger_manual_update()
        return jsonify({"ok": True, **result})

    @app.route("/api/overview", methods=["GET"])
    def api_overview() -> Response:
        """Return synthesized outdoor/indoor ensemble weather, daily extremes, rain records, and nowcast."""
        overview = db.get_weather_overview()
        return jsonify(overview)

    @app.route("/api/nws", methods=["GET"])
    def api_nws() -> Response:
        """Return cached official US National Weather Service baseline corroboration, active alerts, and 7-day forecast."""
        force = request.args.get("refresh", "").strip().lower() in ("1", "true", "yes")
        local_ov = db.get_weather_overview()
        nws_payload = nws_svc.fetch_nws_corroboration(local_overview=local_ov, force_refresh=force)
        return jsonify(nws_payload)

    @app.route("/api/climatology", methods=["GET"])
    def api_climatology() -> Response:
        """Return weekly/monthly degree days, seasonal trajectory anomaly narrative, and Weather-Display Records Summary Table."""
        report = climatology_engine.get_climatology_report()
        return jsonify(report)

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
    @admin_guard
    def api_update_sensor(sensor_key: str) -> tuple[Response, int] | Response:
        """Update user metadata (`alias`, `location_type`, `is_trusted`, `hidden_metrics`, `mark_battery_replaced`, `purge_sensor_outliers`)."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "Request body must be a valid JSON object."}), 400

        alias_arg: Optional[str] = None
        if "alias" in body:
            alias_arg = "" if body["alias"] is None else str(body["alias"])[:80]

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
    @admin_guard
    def api_purge_outliers() -> tuple[Response, int] | Response:
        """Purge skewed/corrupted historical readings, TPMS packets, and appliance probes from SQLite."""
        body = request.get_json(silent=True) or {}
        sensor_key = body.get("sensor_key") if isinstance(body, dict) else None
        result = db.expunge_skewed_history(
            target_sensor_key=str(sensor_key).strip() if sensor_key else None
        )
        return jsonify({"ok": True, "status": "ok", **result})

    @app.route("/api/maintenance/vacuum", methods=["POST"])
    @admin_guard
    def api_vacuum_database() -> tuple[Response, int] | Response:
        """Checkpoint WAL, optimize indexes, and VACUUM SQLite database."""
        result = db.vacuum_database()
        return jsonify({"ok": True, "status": "ok", **result})

    @app.route("/api/maintenance/sync-reference", methods=["POST"])
    @admin_guard
    def api_sync_reference() -> tuple[Response, int] | Response:
        """Sync past 48 hours of regional barometric pressure (`inHg`/`hPa`) & weather history via Open-Meteo."""
        body = request.get_json(silent=True) or {}
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
            nws_svc.invalidate_cache()
            return jsonify({"ok": True, "status": "ok", **sync_result})
        except Exception as exc:
            logger.warning("Open-Meteo regional sync failed: %s", exc)
            return jsonify({"ok": False, "error": f"Failed to sync regional weather: {exc}"}), 502

    @app.route("/api/readings", methods=["GET"])
    def api_readings() -> tuple[Response, int] | Response:
        """Query historical time-series telemetry with optional sensor_key, hours, limit (max 2000), and CSV/JSON export."""
        sensor_key = request.args.get("sensor_key")
        export_fmt = request.args.get("format", "json").strip().lower()
        try:
            hours = float(request.args.get("hours", "24"))
            limit = min(2000, max(1, int(request.args.get("limit", "250"))))
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

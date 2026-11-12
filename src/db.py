"""SQLite WAL database engine, outlier expunger, barometric forecaster, lightning tracker, and charting for SkyScry (v0.0.29)."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import statistics
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

from src.analytics.climatology import ClimatologyEngine
from src.models import (
    COMPASS_16_POINTS,
    NormalizedReading,
    compute_dew_point_c,
    compute_feels_like_f,
    compute_signal_bars,
    compute_signal_quality,
)
from src.pipeline.normalizer import (
    MAX_PLAUSIBLE_PRESSURE_HPA,
    MIN_PLAUSIBLE_PRESSURE_HPA,
    check_physical_bounds,
    is_appliance_model,
)

logger = logging.getLogger(__name__)

VALID_LOCATION_TYPES: frozenset[str] = frozenset({"indoor", "outdoor", "ignore", "unassigned"})
VALID_HIDDEN_METRICS: frozenset[str] = frozenset(
    {"temperature", "humidity", "wind", "rain", "uv_light", "pressure", "lightning"}
)

HPA_TO_INHG = 0.029529983071445

DEFAULT_SYSTEM_SETTINGS: dict[str, str] = {
    "station_name": "SkyScry",
    "city_state_label": "",
    "latitude": "0.0",
    "longitude": "0.0",
    "elevation_m": "0.0",
    "nws_office": "",
    "nws_grid_x": "",
    "nws_grid_y": "",
    "radar_zoom": "7",
    "inactivity_timeout_minutes": "15",
    "temp_unit": "F",
    "wind_unit": "mph",
    "pressure_unit": "inHg",
    "rain_unit": "in",
    "auto_update_enabled": "true",
    "outlier_threshold_f": "22.0",
    "dedup_window_seconds": "4.0",
    "auto_expunge_enabled": "true",
    "auto_vacuum_schedule": "daily_0300",
    "ntp_auto_sync": "true",
    "forecast_baro_weight": "1.0",
    "forecast_dew_weight": "1.0",
    "forecast_models_config": "",
    "baseline_temp_offset_f": "0.0",
    "baseline_baro_offset_inhg": "0.0",
    "nwr_stream_url": "",
}

PUBLIC_SETTINGS_KEYS: frozenset[str] = frozenset(DEFAULT_SYSTEM_SETTINGS.keys())

CANONICAL_WEATHER_CONDITIONS: dict[str, str] = {
    "sunny": "Clear / Sunny",
    "partly_cloudy": "Partly Cloudy",
    "cloudy": "Mostly Cloudy / Overcast",
    "fog": "Fog / Mist",
    "rain": "Rain / Showers",
    "storm": "Thunderstorm / Severe",
    "windy": "Breezy / Windy",
    "snow": "Snow / Wintry Mix",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    date_str TEXT PRIMARY KEY,
    temp_min_c REAL,
    temp_max_c REAL,
    temp_mean_c REAL,
    temp_min_f REAL,
    temp_max_f REAL,
    temp_mean_f REAL,
    humidity_min REAL,
    humidity_max REAL,
    humidity_mean REAL,
    hdd REAL DEFAULT 0.0,
    cdd REAL DEFAULT 0.0,
    rain_total_mm REAL DEFAULT 0.0,
    rain_total_in REAL DEFAULT 0.0,
    wind_peak_mps REAL DEFAULT 0.0,
    wind_peak_mph REAL DEFAULT 0.0,
    pressure_min_hpa REAL,
    pressure_max_hpa REAL,
    pressure_range_inhg REAL,
    reading_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reference_weather_history (
    timestamp TEXT PRIMARY KEY,
    pressure_hpa REAL,
    surface_pressure_hpa REAL,
    temp_c REAL,
    humidity REAL,
    wind_avg_mps REAL,
    wind_gust_mps REAL,
    wind_dir_deg REAL,
    rain_mm REAL
);

CREATE TABLE IF NOT EXISTS lightning_strikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    sensor_key TEXT NOT NULL,
    delta_strikes INTEGER NOT NULL,
    distance_mi REAL NOT NULL,
    rfi INTEGER DEFAULT 0,
    confirmed INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_lightning_strikes_time
    ON lightning_strikes (timestamp DESC);

CREATE TABLE IF NOT EXISTS weather_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT UNIQUE NOT NULL,
    event TEXT NOT NULL,
    severity TEXT,
    urgency TEXT,
    certainty TEXT,
    headline TEXT,
    description TEXT,
    instruction TEXT,
    area_desc TEXT,
    sender_name TEXT,
    onset TEXT,
    expires TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_weather_alerts_active
    ON weather_alerts (is_active, onset DESC);

CREATE INDEX IF NOT EXISTS idx_weather_alerts_time
    ON weather_alerts (onset DESC, expires DESC);

CREATE TABLE IF NOT EXISTS learned_weather_model (
    month INTEGER PRIMARY KEY,
    normal_temp_max_f REAL,
    normal_temp_min_f REAL,
    normal_temp_mean_f REAL,
    normal_precip_in REAL,
    rain_day_freq REAL,
    mean_pressure_hpa REAL,
    rain_pressure_threshold_hpa REAL,
    clear_pressure_threshold_hpa REAL,
    max_wind_mph_p90 REAL,
    sample_days INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS weather_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    predicted_condition TEXT,
    predicted_temp_f REAL,
    user_verdict TEXT,
    actual_condition TEXT,
    pressure_hpa REAL,
    dew_spread_f REAL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_weather_feedback_time
    ON weather_feedback (timestamp DESC);

CREATE TABLE IF NOT EXISTS sensors (
    sensor_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    sensor_id TEXT NOT NULL,
    channel TEXT,
    alias TEXT,
    location_type TEXT DEFAULT 'ignore',
    is_trusted INTEGER DEFAULT 0,
    hidden_metrics TEXT DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    battery_ok INTEGER,
    last_rssi REAL,
    last_snr REAL,
    last_noise REAL,
    packet_count INTEGER DEFAULT 0,
    expunged_count INTEGER DEFAULT 0,
    last_expunged_reason TEXT,
    battery_changed_at TEXT,
    battery_low_at TEXT,
    estimated_battery_days INTEGER
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    sensor_id TEXT NOT NULL,
    model TEXT NOT NULL,
    channel TEXT,
    battery_ok INTEGER,
    temp_c REAL,
    humidity REAL,
    pressure_hpa REAL,
    wind_avg_mps REAL,
    wind_gust_mps REAL,
    wind_dir_deg REAL,
    rain_cumulative_mm REAL,
    rain_rate_mm_h REAL,
    uv_index REAL,
    lux REAL,
    strike_count INTEGER,
    storm_dist_mi REAL,
    rfi_detected INTEGER,
    rssi REAL,
    snr REAL,
    noise REAL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_time
    ON sensor_readings (sensor_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_model_sensor_time
    ON sensor_readings (model, sensor_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp
    ON sensor_readings (timestamp);

CREATE TABLE IF NOT EXISTS daily_weather_overrides (
    date_str TEXT PRIMARY KEY,
    verified_status TEXT NOT NULL DEFAULT 'accurate',
    corrected_condition_type TEXT,
    corrected_condition_label TEXT,
    exclude_false_lightning INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_weather_overrides_updated
    ON daily_weather_overrides (updated_at DESC);
"""


def format_utc_z(dt: datetime) -> str:
    """Format a datetime as a strict UTC ISO-8601 string ending in 'Z' (`YYYY-MM-DDTHH:MM:SSZ`)."""
    utc_dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse any stored ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    cleaned = ts_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _deg_to_cardinal(deg: Optional[float]) -> Optional[str]:
    if deg is None:
        return None
    normalized_deg = float(deg) % 360.0
    index = int((normalized_deg + 11.25) // 22.5) % 16
    return COMPASS_16_POINTS[index]


def _parse_hidden_metrics(raw_val: Any) -> list[str]:
    """Safely deserialize `hidden_metrics` JSON array from SQLite."""
    if not raw_val:
        return []
    if isinstance(raw_val, list):
        return [str(x).strip().lower() for x in raw_val if str(x).strip().lower() in VALID_HIDDEN_METRICS]
    if isinstance(raw_val, str):
        try:
            loaded = json.loads(raw_val)
            if isinstance(loaded, list):
                return [
                    str(x).strip().lower()
                    for x in loaded
                    if str(x).strip().lower() in VALID_HIDDEN_METRICS
                ]
        except Exception:
            pass
    return []


def _enrich_reading_dict(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach computed imperial, dew point, compass, pressure_inhg, and RF quality fields."""
    temp_c = row_dict.get("temp_c")
    humidity = row_dict.get("humidity")
    pressure_hpa = row_dict.get("pressure_hpa")
    if pressure_hpa is not None:
        p_val = float(pressure_hpa)
        if p_val < MIN_PLAUSIBLE_PRESSURE_HPA or p_val > MAX_PLAUSIBLE_PRESSURE_HPA:
            pressure_hpa = None
            row_dict["pressure_hpa"] = None
    wind_avg_mps = row_dict.get("wind_avg_mps")
    wind_gust_mps = row_dict.get("wind_gust_mps")
    wind_dir_deg = row_dict.get("wind_dir_deg")
    rain_mm = row_dict.get("rain_cumulative_mm")
    rain_rate_mm = row_dict.get("rain_rate_mm_h")
    battery_raw = row_dict.get("battery_ok")
    snr = row_dict.get("snr")
    rssi = row_dict.get("rssi")

    parsed_ts = parse_utc_iso(row_dict.get("timestamp"))
    if parsed_ts is not None:
        row_dict["timestamp"] = format_utc_z(parsed_ts)

    temp_f = round((float(temp_c) * 9.0 / 5.0) + 32.0, 2) if temp_c is not None else None
    dp_c = compute_dew_point_c(
        float(temp_c) if temp_c is not None else None,
        float(humidity) if humidity is not None else None,
    )
    dp_f = round((dp_c * 9.0 / 5.0) + 32.0, 2) if dp_c is not None else None

    row_dict["temp_f"] = temp_f
    row_dict["dew_point_c"] = dp_c
    row_dict["dew_point_f"] = dp_f
    row_dict["pressure_inhg"] = (
        round(float(pressure_hpa) * HPA_TO_INHG, 2) if pressure_hpa is not None else None
    )
    row_dict["wind_avg_mph"] = (
        round(float(wind_avg_mps) * 2.23693629, 2) if wind_avg_mps is not None else None
    )
    row_dict["wind_gust_mph"] = (
        round(float(wind_gust_mps) * 2.23693629, 2) if wind_gust_mps is not None else None
    )
    row_dict["rain_cumulative_in"] = (
        round(float(rain_mm) / 25.4, 3) if rain_mm is not None else None
    )
    row_dict["rain_rate_in_h"] = (
        round(float(rain_rate_mm) / 25.4, 3) if rain_rate_mm is not None else None
    )
    row_dict["wind_cardinal"] = _deg_to_cardinal(wind_dir_deg)
    row_dict["signal_quality"] = compute_signal_quality(snr, rssi)
    row_dict["signal_bars"] = compute_signal_bars(snr, rssi)
    if battery_raw is not None:
        row_dict["battery_ok"] = bool(int(battery_raw) != 0)
    rfi_raw = row_dict.get("rfi_detected")
    if rfi_raw is not None:
        row_dict["rfi_detected"] = 1 if int(rfi_raw) != 0 else 0
    return row_dict


_IP_GEO_ATTEMPTED = False
_IP_GEO_LOCK = threading.Lock()


def auto_resolve_ip_coordinates(timeout: float = 4.0) -> Optional[tuple[float, float, str]]:
    """Query a free non-key IP geolocation endpoint once if station coordinates are unset."""
    global _IP_GEO_ATTEMPTED
    with _IP_GEO_LOCK:
        if _IP_GEO_ATTEMPTED:
            return None
        _IP_GEO_ATTEMPTED = True

    endpoints = [
        ("http://ip-api.com/json/?fields=lat,lon,city,regionCode", "ip-api"),
        ("https://get.geojs.io/v1/ip/geo.json", "geojs"),
    ]
    for url, service in endpoints:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "SkyScry/0.0.12 (https://github.com/Cave404/SkyScry)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if not isinstance(data, dict):
                    continue
                if service == "ip-api":
                    lat = float(data.get("lat") or 0.0)
                    lon = float(data.get("lon") or 0.0)
                    city = str(data.get("city") or "").strip()
                    region = str(data.get("regionCode") or "").strip()
                else:
                    lat = float(data.get("latitude") or 0.0)
                    lon = float(data.get("longitude") or 0.0)
                    city = str(data.get("city") or "").strip()
                    region = str(data.get("region") or "").strip()
                if abs(lat) > 0.001 or abs(lon) > 0.001:
                    label = f"{city}, {region}".strip(", ")
                    logger.info("Auto-resolved station coordinates via %s: (%.4f, %.4f) %s", service, lat, lon, label)
                    return round(lat, 4), round(lon, 4), label
        except Exception as exc:
            logger.debug("IP geolocation query to %s failed: %s", url, exc)
    return None


class WeatherDatabase:
    """Thread-safe SQLite WAL database manager with opt-in sensor trust, TPMS/outlier expunging, and barometric synthesis."""

    def __init__(self, db_path: str | Path = "data/skyscry.db") -> None:
        self.db_path = Path(db_path)
        if self.db_path.name != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            str(self.db_path),
            timeout=15.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._nws_service: Optional[Any] = None
        self._configure_connection()
        self._initialize_schema()
        logger.debug("Initialized SQLite WAL database at %s", self.db_path)

    def set_nws_service(self, svc: Any) -> None:
        """Register NWS weather service instance for smart strike corroboration and PoP lookups."""
        self._nws_service = svc

    def get_current_pop_percent(self) -> Optional[int]:
        """Query current probability of precipitation (PoP %) from NWS service if available."""
        if self._nws_service is not None:
            try:
                return self._nws_service.get_current_pop_percent()
            except Exception:
                pass
        return None

    def _configure_connection(self) -> None:
        """Enable WAL journal mode and NORMAL synchronization for concurrent read/write performance."""
        with self._lock:
            if self._conn is None:
                return
            cursor = self._conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA busy_timeout=5000;")
            finally:
                cursor.close()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """Idempotently add a column to `table` after inspecting `PRAGMA table_info()`."""
        if self._conn is None:
            return
        existing_cols = {
            str(row["name"]).lower()
            for row in self._conn.execute(f"PRAGMA table_info({table});").fetchall()
        }
        if column.lower() not in existing_cols:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition};")
            except sqlite3.OperationalError:
                pass

    def _initialize_schema(self) -> None:
        """Create tables, run idempotent migrations, and expunge TPMS/skewed historical outliers."""
        with self._lock:
            if self._conn is None:
                return
            with self._conn:
                self._conn.executescript(SCHEMA_SQL)

                # Idempotent column checks via PRAGMA table_info
                self._ensure_column("system_settings", "updated_at", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column("sensors", "hidden_metrics", "TEXT DEFAULT '[]'")
                self._ensure_column("sensors", "last_snr", "REAL")
                self._ensure_column("sensors", "last_noise", "REAL")
                self._ensure_column("sensors", "last_rssi", "REAL")
                self._ensure_column("sensors", "packet_count", "INTEGER DEFAULT 0")
                self._ensure_column("sensors", "expunged_count", "INTEGER DEFAULT 0")
                self._ensure_column("sensors", "last_expunged_reason", "TEXT")
                self._ensure_column("sensors", "battery_changed_at", "TEXT")
                self._ensure_column("sensors", "battery_low_at", "TEXT")
                self._ensure_column("sensors", "estimated_battery_days", "INTEGER")
                self._ensure_column("sensors", "sensor_key", "TEXT")

                self._ensure_column("sensor_readings", "snr", "REAL")
                self._ensure_column("sensor_readings", "noise", "REAL")
                self._ensure_column("sensor_readings", "rssi", "REAL")
                self._ensure_column("sensor_readings", "sensor_key", "TEXT")
                self._ensure_column("sensor_readings", "strike_count", "INTEGER")
                self._ensure_column("sensor_readings", "storm_dist_mi", "REAL")
                self._ensure_column("sensor_readings", "rfi_detected", "INTEGER")
                self._ensure_column("lightning_strikes", "confirmed", "INTEGER DEFAULT 1")
                self._ensure_column("weather_alerts", "instruction", "TEXT")
                self._ensure_column("weather_alerts", "area_desc", "TEXT")
                self._ensure_column("weather_alerts", "sender_name", "TEXT")
                self._ensure_column("weather_alerts", "is_active", "INTEGER DEFAULT 1")

                now_iso = format_utc_z(datetime.now(timezone.utc))
                for def_k, def_v in DEFAULT_SYSTEM_SETTINGS.items():
                    self._conn.execute(
                        """
                        INSERT INTO system_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO NOTHING;
                        """,
                        (def_k, def_v, now_iso),
                    )

                # Populate computed/indexed sensor_key column on sensor_readings for fast indexed joins
                self._conn.execute(
                    """
                    UPDATE sensor_readings
                    SET sensor_key = (model || '_' || sensor_id)
                    WHERE sensor_key IS NULL;
                    """
                )
                self._conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_key_time
                    ON sensor_readings (sensor_key, timestamp DESC);
                    """
                )

                # One-time migration for v0.0.8 Opt-In Trust Model:
                # Convert any legacy 'unassigned' sensors (while preserving user-configured 'outdoor' and 'indoor')
                # to location_type = 'ignore' and is_trusted = 0.
                mig_row = self._conn.execute(
                    "SELECT value FROM system_settings WHERE key = 'migration_v008_opt_in_trust';"
                ).fetchone()
                if not mig_row:
                    self._conn.execute(
                        """
                        UPDATE sensors
                        SET location_type = 'ignore', is_trusted = 0
                        WHERE location_type = 'unassigned' OR location_type IS NULL;
                        """
                    )
                    self._conn.execute(
                        "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('migration_v008_opt_in_trust', '1');"
                    )

                self._repair_shifted_timestamps()
                self._auto_heal_trusted_sensors()

        # Run startup TPMS, barometric outlier, appliance auto-purge, and lightning noise gate
        self.expunge_skewed_history()
        self.auto_classify_isolated_strikes()
        self._auto_heal_trusted_sensors()

    def _auto_heal_trusted_sensors(self) -> None:
        """Ensure sensors configured as outdoor or indoor are trusted, and auto-restore known weather stations if fleet was wiped."""
        with self._lock:
            if self._conn is None:
                return
            with self._conn:
                # 1. Any sensor marked as outdoor or indoor must have is_trusted = 1
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET is_trusted = 1
                    WHERE location_type IN ('outdoor', 'indoor') AND (is_trusted = 0 OR is_trusted IS NULL);
                    """
                )

                # Ensure indoor station #8364 is location_type = 'indoor', is_trusted = 1
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET location_type = 'indoor', is_trusted = 1
                    WHERE sensor_id = '8364' OR sensor_key LIKE '%_8364';
                    """
                )

                # 2. Check if zero outdoor sensors exist in the fleet
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM sensors WHERE location_type = 'outdoor' AND is_trusted = 1;"
                ).fetchone()
                outdoor_count = int(row["c"]) if row else 0

                if outdoor_count == 0:
                    reading_sensors = self._conn.execute(
                        """
                        SELECT DISTINCT model, sensor_id, COALESCE(sensor_key, model || '_' || sensor_id) AS s_key,
                                        MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen, COUNT(*) AS pkts
                        FROM sensor_readings
                        WHERE (
                            model LIKE 'Acurite-Tower%' OR
                            model LIKE 'Cotech%' OR
                            model LIKE 'LaCrosse%' OR
                            model LIKE 'Oregon%' OR
                            model LIKE 'Acurite-606TX%' OR
                            model LIKE 'Acurite-5n1%' OR
                            model LIKE 'Acurite-Atlas%'
                        )
                        GROUP BY model, sensor_id;
                        """
                    ).fetchall()

                    restored = 0
                    for rs in reading_sensors:
                        s_key = rs["s_key"]
                        m = rs["model"]
                        sid = str(rs["sensor_id"])
                        is_indoor = (sid == "8364")
                        loc = "indoor" if is_indoor else "outdoor"

                        self._conn.execute(
                            """
                            INSERT INTO sensors (
                                sensor_key, model, sensor_id, channel, alias,
                                location_type, is_trusted, hidden_metrics,
                                first_seen, last_seen, battery_ok,
                                packet_count, expunged_count
                            ) VALUES (?, ?, ?, NULL, NULL, ?, 1, '[]', ?, ?, 1, ?, 0)
                            ON CONFLICT(sensor_key) DO UPDATE SET
                                location_type = excluded.location_type,
                                is_trusted = 1;
                            """,
                            (s_key, m, sid, loc, rs["first_seen"], rs["last_seen"], rs["pkts"]),
                        )
                        restored += 1

                    if restored > 0:
                        logger.info(
                            "[AUTO-HEAL] Restored %d known weather station(s) from historical telemetry to active trusted fleet.",
                            restored,
                        )

    def _repair_shifted_timestamps(self) -> None:
        """Repair historical rows where naive local `rtl_433` time was stored with `+00:00`."""
        if self._conn is None:
            return
        now_utc = datetime.now(timezone.utc)
        rows = self._conn.execute(
            "SELECT id, timestamp FROM sensor_readings WHERE timestamp LIKE '%+00:00';"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        for r in rows:
            raw_ts = str(r["timestamp"])
            dt_parsed = parse_utc_iso(raw_ts)
            if dt_parsed is None:
                continue
            diff_hours = (now_utc - dt_parsed).total_seconds() / 3600.0
            if diff_hours >= 3.5:
                naive_local = dt_parsed.replace(tzinfo=None)
                corrected_utc = naive_local.astimezone().astimezone(timezone.utc)
                if abs((now_utc - corrected_utc).total_seconds()) < abs((now_utc - dt_parsed).total_seconds()):
                    updates.append((format_utc_z(corrected_utc), int(r["id"])))
            else:
                updates.append((format_utc_z(dt_parsed), int(r["id"])))

        if updates:
            self._conn.executemany(
                "UPDATE sensor_readings SET timestamp = ? WHERE id = ?;",
                updates,
            )

    def _has_storm_weather_corroboration(
        self,
        target_dt: datetime,
        is_historical: bool = False,
    ) -> tuple[bool, str]:
        """Return (True, reason) when local sensors, reference data, alerts, or NWS indicate rain/storm."""
        if self._conn is None:
            return False, ""

        target_utc = target_dt.astimezone(timezone.utc) if target_dt.tzinfo else target_dt.replace(tzinfo=timezone.utc)
        target_iso = format_utc_z(target_utc)
        start_iso = format_utc_z(target_utc - timedelta(minutes=45 if is_historical else 30))
        end_iso = format_utc_z(target_utc + timedelta(minutes=45)) if is_historical else target_iso

        # 1. Local trusted outdoor rainfall (rate > 0 or >= 0.2mm cumulative delta)
        try:
            rain_row = self._conn.execute(
                """
                SELECT
                    MAX(CASE WHEN sr.rain_rate_mm_h IS NOT NULL AND sr.rain_rate_mm_h > 0 THEN 1 ELSE 0 END) AS has_rate,
                    MIN(sr.rain_cumulative_mm) AS min_rain,
                    MAX(sr.rain_cumulative_mm) AS max_rain
                FROM sensor_readings sr
                JOIN sensors s ON s.model = sr.model AND s.sensor_id = sr.sensor_id
                WHERE s.is_trusted = 1
                  AND s.location_type = 'outdoor'
                  AND sr.timestamp BETWEEN ? AND ?;
                """,
                (start_iso, end_iso),
            ).fetchone()
            if rain_row:
                if int(rain_row["has_rate"] or 0) > 0:
                    return True, "Local rain_rate_mm_h > 0"
                min_r = rain_row["min_rain"]
                max_r = rain_row["max_rain"]
                if min_r is not None and max_r is not None and (float(max_r) - float(min_r)) >= 0.2:
                    return True, f"Local rainfall delta {float(max_r) - float(min_r):.2f}mm"
        except Exception as exc:
            logger.debug("Local rain corroboration check skipped: %s", exc)

        # 2. Local trusted outdoor Dew Point Spread < 4.0°F
        try:
            temp_rows = self._conn.execute(
                """
                SELECT sr.temp_c, sr.humidity
                FROM sensor_readings sr
                JOIN sensors s ON s.model = sr.model AND s.sensor_id = sr.sensor_id
                WHERE s.is_trusted = 1
                  AND s.location_type = 'outdoor'
                  AND sr.temp_c IS NOT NULL
                  AND sr.humidity IS NOT NULL
                  AND sr.timestamp BETWEEN ? AND ?
                ORDER BY ABS(strftime('%s', sr.timestamp) - strftime('%s', ?)) ASC, sr.id DESC
                LIMIT 3;
                """,
                (start_iso, end_iso, target_iso),
            ).fetchall()
            for tr in temp_rows:
                tc = tr["temp_c"]
                rh = tr["humidity"]
                dp_c = compute_dew_point_c(float(tc), float(rh))
                if dp_c is not None:
                    spread_f = (float(tc) - float(dp_c)) * 9.0 / 5.0
                    if spread_f < 4.0:
                        return True, f"Outdoor dew spread {spread_f:.1f}°F < 4.0°F"
        except Exception as exc:
            logger.debug("Dew spread corroboration check skipped: %s", exc)

        # 3. Reference / Open-Meteo archived rain around timestamp (>= 0.25mm)
        try:
            ref_start = format_utc_z(target_utc - timedelta(minutes=60))
            ref_end = format_utc_z(target_utc + timedelta(minutes=60)) if is_historical else target_iso
            ref_row = self._conn.execute(
                """
                SELECT MAX(rain_mm) AS max_ref_rain
                FROM reference_weather_history
                WHERE timestamp BETWEEN ? AND ?;
                """,
                (ref_start, ref_end),
            ).fetchone()
            if ref_row and ref_row["max_ref_rain"] is not None and float(ref_row["max_ref_rain"]) >= 0.25:
                return True, f"Reference weather rain {float(ref_row['max_ref_rain']):.2f}mm"
        except Exception as exc:
            logger.debug("Reference rain corroboration check skipped: %s", exc)

        # 4. Archived / Active Weather Alerts overlapping target timestamp
        try:
            alert_rows = self._conn.execute(
                """
                SELECT event
                FROM weather_alerts
                WHERE COALESCE(onset, effective, first_seen) <= ?
                  AND COALESCE(expires, last_seen, first_seen) >= ?;
                """,
                (target_iso, target_iso),
            ).fetchall()
            for ar in alert_rows:
                ev = str(ar["event"] or "")
                ev_low = ev.lower()
                if any(k in ev_low for k in ("thunder", "t-storm", "lightning", "tornado", "storm", "rain", "shower", "squall")):
                    return True, f"Weather alert ({ev})"
        except Exception as exc:
            logger.debug("Weather alert corroboration check skipped: %s", exc)

        # 5. Live NWS / Open-Meteo current condition & forecast period check
        if not is_historical and self._nws_service is not None and hasattr(self._nws_service, "has_storm_or_rain_corroboration"):
            try:
                ok_nws, reason_nws = self._nws_service.has_storm_or_rain_corroboration()
                if ok_nws:
                    return True, reason_nws
            except Exception as exc:
                logger.debug("Live NWS storm corroboration check skipped: %s", exc)

        return False, ""

    def get_daily_weather_overrides(self) -> dict[str, dict[str, Any]]:
        """Return persisted calendar day verification & correction overrides keyed by YYYY-MM-DD."""
        with self._lock:
            if self._conn is None:
                return {}
            rows = self._conn.execute(
                """
                SELECT date_str, verified_status, corrected_condition_type,
                       corrected_condition_label, exclude_false_lightning, notes, updated_at
                FROM daily_weather_overrides;
                """
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            ds = str(r["date_str"])
            out[ds] = {
                "date": ds,
                "verified_status": str(r["verified_status"] or "accurate"),
                "corrected_condition_type": r["corrected_condition_type"],
                "corrected_condition_label": r["corrected_condition_label"],
                "exclude_false_lightning": bool(int(r["exclude_false_lightning"] or 0)),
                "notes": str(r["notes"] or ""),
                "updated_at": str(r["updated_at"] or ""),
            }
        return out

    def auto_classify_isolated_strikes(self) -> int:
        """Re-evaluate lightning_strikes from the past 72h using 15m burst density and weather corroboration."""
        now_utc = datetime.now(timezone.utc)
        cutoff_72h = format_utc_z(now_utc - timedelta(hours=72))
        with self._lock:
            if self._conn is None:
                return 0
            overrides = self.get_daily_weather_overrides()
            with self._conn:
                rows = self._conn.execute(
                    """
                    SELECT id, timestamp, DATE(timestamp, 'localtime') AS local_date,
                           delta_strikes, distance_mi, COALESCE(confirmed, 1) AS confirmed
                    FROM lightning_strikes
                    WHERE timestamp >= ?
                    ORDER BY timestamp ASC, id ASC;
                    """,
                    (cutoff_72h,),
                ).fetchall()
                if not rows:
                    return 0

                parsed: list[tuple[int, datetime, str, int]] = []
                for row in rows:
                    dt = parse_utc_iso(str(row["timestamp"]))
                    if dt is None:
                        continue
                    parsed.append((int(row["id"]), dt, str(row["local_date"] or ""), int(row["confirmed"])))

                n = len(parsed)
                if n == 0:
                    return 0

                weather_cache: dict[str, tuple[bool, str]] = {}
                demoted = 0
                promoted = 0

                for i, (row_id, dt, local_date, prev_conf) in enumerate(parsed):
                    ov = overrides.get(local_date)
                    target_conf: Optional[int] = None
                    if ov is not None:
                        if ov.get("exclude_false_lightning"):
                            target_conf = 0
                        elif ov.get("corrected_condition_type") == "storm":
                            target_conf = 1
                        elif ov.get("corrected_condition_type"):
                            target_conf = 0

                    if target_conf is None:
                        # 1. Burst Density: >= 3 distinct strike packets in any 15m sliding window covering packet i
                        burst_ok = False
                        if n >= 3:
                            a_min = max(0, i - 2)
                            a_max = min(n - 3, i)
                            for a in range(a_min, a_max + 1):
                                if (parsed[a + 2][1] - parsed[a][1]).total_seconds() <= 900.0:
                                    burst_ok = True
                                    break

                        # 2. Historical Weather Corroboration (cached by hour bucket)
                        corr_ok = False
                        if not burst_ok:
                            bucket = dt.strftime("%Y-%m-%dT%H")
                            if bucket not in weather_cache:
                                weather_cache[bucket] = self._has_storm_weather_corroboration(dt, is_historical=True)
                            corr_ok = weather_cache[bucket][0]

                        target_conf = 1 if (burst_ok or corr_ok) else 0

                    if target_conf != prev_conf:
                        self._conn.execute(
                            "UPDATE lightning_strikes SET confirmed = ? WHERE id = ?;",
                            (target_conf, row_id),
                        )
                        if target_conf == 0:
                            demoted += 1
                        else:
                            promoted += 1

                if demoted > 0 or promoted > 0:
                    logger.info(
                        "[LIGHTNING NOISE GATE] 72h re-evaluation complete: %d isolated EMI transient(s) suppressed (confirmed=0), %d confirmed (confirmed=1).",
                        demoted,
                        promoted,
                    )
                return demoted

    @staticmethod
    def make_sensor_key(model: str, sensor_id: str) -> str:
        """Format the canonical `{model}_{sensor_id}` primary key."""
        return f"{model}_{sensor_id}"

    def _get_active_outdoor_median_temp_f(self) -> Optional[float]:
        """Compute the current outdoor ensemble median temperature (°F) across trusted active outdoor sensors."""
        if self._conn is None:
            return None
        cutoff_iso = format_utc_z(datetime.now(timezone.utc) - timedelta(minutes=60))
        rows = self._conn.execute(
            """
            SELECT s.sensor_key, s.hidden_metrics,
                   (SELECT sr.temp_c FROM sensor_readings sr
                    WHERE sr.model = s.model AND sr.sensor_id = s.sensor_id
                      AND sr.timestamp >= ? AND sr.temp_c IS NOT NULL
                    ORDER BY sr.timestamp DESC, sr.id DESC LIMIT 1) AS latest_tc
            FROM sensors s
            WHERE s.is_trusted = 1
              AND s.location_type = 'outdoor'
              AND s.packet_count >= 2;
            """,
            (cutoff_iso,),
        ).fetchall()

        temps_f: list[float] = []
        for r in rows:
            hidden = set(_parse_hidden_metrics(r["hidden_metrics"]))
            if "temperature" in hidden or r["latest_tc"] is None:
                continue
            tf = (float(r["latest_tc"]) * 9.0 / 5.0) + 32.0
            if -25.0 <= tf <= 130.0:
                temps_f.append(tf)

        if len(temps_f) >= 2:
            return float(statistics.median(temps_f))
        return None

    def _record_expunged_packet(self, reading: NormalizedReading, reason: str) -> None:
        """Increment `expunged_count` and store `last_expunged_reason` on the sensor if it already exists."""
        sensor_key = self.make_sensor_key(reading.model, reading.sensor_id)
        if self._conn is None:
            return
        with self._conn:
            self._conn.execute(
                """
                UPDATE sensors
                SET expunged_count = COALESCE(expunged_count, 0) + 1,
                    last_expunged_reason = ?
                WHERE sensor_key = ?;
                """,
                (reason, sensor_key),
            )

    def expunge_skewed_history(self, target_sensor_key: Optional[str] = None) -> dict[str, Any]:
        """Purge vehicle TPMS, soil/appliance probes, invalid barometric pressures, and bit-flip outliers from SQLite."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")

            deleted_readings = 0
            deleted_sensors = 0

            with self._conn:
                # 1. Completely purge vehicle TPMS, soil moisture, and appliance models from sensors & sensor_readings
                all_s = self._conn.execute("SELECT sensor_key, model FROM sensors;").fetchall()
                tpms_or_app_keys: list[str] = []
                for s in all_s:
                    if is_appliance_model(s["model"]):
                        tpms_or_app_keys.append(str(s["sensor_key"]))

                if tpms_or_app_keys:
                    ph = ",".join("?" for _ in tpms_or_app_keys)
                    cur_del_sr = self._conn.execute(
                        f"DELETE FROM sensor_readings WHERE COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph});",
                        tpms_or_app_keys,
                    )
                    deleted_readings += int(cur_del_sr.rowcount or 0)
                    cur_del_s = self._conn.execute(
                        f"DELETE FROM sensors WHERE sensor_key IN ({ph});",
                        tpms_or_app_keys,
                    )
                    deleted_sensors += int(cur_del_s.rowcount or 0)

                # Also sweep any raw readings matching vehicle/TPMS/soil prefixes directly in sensor_readings
                distinct_models = self._conn.execute(
                    "SELECT DISTINCT model FROM sensor_readings;"
                ).fetchall()
                bad_models = [str(r["model"]) for r in distinct_models if is_appliance_model(str(r["model"]))]
                if bad_models:
                    ph_m = ",".join("?" for _ in bad_models)
                    cur_m = self._conn.execute(
                        f"DELETE FROM sensor_readings WHERE model IN ({ph_m});",
                        bad_models,
                    )
                    deleted_readings += int(cur_m.rowcount or 0)

                # 2. Delete or clamp any impossible barometric pressure readings outside [870, 1085] hPa (e.g. 2130 hPa / 62.90 inHg)
                cur_baro = self._conn.execute(
                    """
                    DELETE FROM sensor_readings
                    WHERE pressure_hpa IS NOT NULL
                      AND (pressure_hpa < 870.0 OR pressure_hpa > 1085.0);
                    """
                )
                deleted_readings += int(cur_baro.rowcount or 0)

                self._conn.execute(
                    """
                    DELETE FROM reference_weather_history
                    WHERE pressure_hpa IS NOT NULL
                      AND (pressure_hpa < 870.0 OR pressure_hpa > 1085.0);
                    """
                )

                # 3. Delete readings that violate hard physical meteorological bounds
                #    (temp_f < -25F [-31.67C] or temp_f > 130F [54.44C], humidity < 1 or > 100, wind > 130 mph [58.1 m/s])
                where_target_del = "AND COALESCE(sensor_key, model || '_' || sensor_id) = ?" if target_sensor_key else ""
                where_target_sr = "AND COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id) = ?" if target_sensor_key else ""
                params_target = (target_sensor_key,) if target_sensor_key else ()

                cur_bounds = self._conn.execute(
                    f"""
                    DELETE FROM sensor_readings
                    WHERE (
                        temp_c < -31.67
                        OR temp_c > 54.44
                        OR humidity < 1.0
                        OR humidity > 100.0
                        OR wind_avg_mps > 58.1
                        OR wind_gust_mps > 58.1
                    ) {where_target_del};
                    """,
                    params_target,
                )
                deleted_readings += int(cur_bounds.rowcount or 0)

                # 4. Consensus Hourly Median Outlier Sweep:
                #    Group outdoor readings by hour and purge any reading deviating > 24F (13.33C)
                #    from that hour's multi-sensor median.
                rows_cand = self._conn.execute(
                    f"""
                    SELECT sr.id, sr.timestamp, sr.temp_c,
                           COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id) AS skey,
                           COALESCE(s.location_type, 'ignore') AS loc
                    FROM sensor_readings sr
                    LEFT JOIN sensors s ON s.sensor_key = COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id)
                    WHERE sr.temp_c IS NOT NULL
                      AND COALESCE(s.location_type, 'ignore') = 'outdoor'
                      {where_target_sr};
                    """,
                    params_target,
                ).fetchall()

                by_hour: dict[str, list[sqlite3.Row]] = {}
                for r in rows_cand:
                    hr_key = str(r["timestamp"])[:13]
                    by_hour.setdefault(hr_key, []).append(r)

                outlier_ids: list[tuple[int]] = []
                outlier_per_sensor: dict[str, int] = {}
                for _hr, group in by_hour.items():
                    if len(group) < 3:
                        continue
                    med_c = float(statistics.median([float(x["temp_c"]) for x in group]))
                    for x in group:
                        if abs(float(x["temp_c"]) - med_c) > 13.33:  # > 24.0 F deviation from hourly median
                            outlier_ids.append((int(x["id"]),))
                            skey = str(x["skey"])
                            outlier_per_sensor[skey] = outlier_per_sensor.get(skey, 0) + 1

                if outlier_ids:
                    self._conn.executemany(
                        "DELETE FROM sensor_readings WHERE id = ?;",
                        outlier_ids,
                    )
                    deleted_readings += len(outlier_ids)
                    for skey, cnt in outlier_per_sensor.items():
                        self._conn.execute(
                            """
                            UPDATE sensors
                            SET expunged_count = COALESCE(expunged_count, 0) + ?,
                                last_expunged_reason = 'Historical reading deviated >24F from hourly outdoor median'
                            WHERE sensor_key = ?;
                            """,
                            (cnt, skey),
                        )

                # 5. Recompute accurate packet_count on all sensors
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET packet_count = (
                        SELECT COUNT(*) FROM sensor_readings sr
                        WHERE sr.model = sensors.model AND sr.sensor_id = sensors.sensor_id
                    );
                    """
                )

            if deleted_readings > 0 or deleted_sensors > 0:
                logger.info(
                    "[AUTO-EXPUNGE] Purged %d skewed/TPMS reading(s) and %d non-weather sensor(s) from SQLite.",
                    deleted_readings,
                    deleted_sensors,
                )

            return {
                "deleted_readings": deleted_readings,
                "deleted_sensors": deleted_sensors,
            }

    def insert_reading(self, reading: NormalizedReading) -> Optional[int]:
        """Validate, filter TPMS/appliances/outliers, and persist a NormalizedReading into SQLite.

        Newly discovered sensors default strictly to `location_type='ignore'` and `is_trusted=0` (Opt-In Trust Model).
        """
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Cannot insert into a closed WeatherDatabase connection.")

            sensor_key = self.make_sensor_key(reading.model, reading.sensor_id)

            # 1. Hard-drop if this model is a vehicle TPMS, soil probe, or kitchen/fridge appliance
            if is_appliance_model(reading.model):
                logger.debug(
                    "[AUTO-EXPUNGED NON-WEATHER] Ignored TPMS/soil/appliance probe %s",
                    sensor_key,
                )
                return None

            # 2. Check Hard Physical Meteorological Bounds (-25F to 130F, RH 1-100%, Pressure 870-1085 hPa, Wind <= 130 mph)
            bounds_err = check_physical_bounds(reading)
            if bounds_err is not None:
                logger.warning(
                    "[AUTO-EXPUNGED OUTLIER] Rejected %s: %s (SNR=%s dB)",
                    sensor_key,
                    bounds_err,
                    reading.snr,
                )
                self._record_expunged_packet(reading, bounds_err)
                return None

            existing = self._conn.execute(
                """
                SELECT location_type, is_trusted, battery_ok, last_seen, first_seen,
                       battery_changed_at, battery_low_at, estimated_battery_days, packet_count
                FROM sensors WHERE sensor_key = ?;
                """,
                (sensor_key,),
            ).fetchone()

            # 3. Rate-of-Change Spike Filter & Dynamic Outdoor Consensus Median Filter
            if reading.temp_f is not None:
                prev_row = self._conn.execute(
                    """
                    SELECT timestamp, temp_c FROM sensor_readings
                    WHERE model = ? AND sensor_id = ? AND temp_c IS NOT NULL
                    ORDER BY timestamp DESC, id DESC LIMIT 1;
                    """,
                    (reading.model, reading.sensor_id),
                ).fetchone()

                if prev_row is not None and prev_row["temp_c"] is not None:
                    prev_dt = parse_utc_iso(prev_row["timestamp"])
                    prev_tf = (float(prev_row["temp_c"]) * 9.0 / 5.0) + 32.0
                    if prev_dt is not None:
                        elapsed_min = abs((reading.timestamp - prev_dt).total_seconds()) / 60.0
                        temp_jump_f = abs(reading.temp_f - prev_tf)
                        if elapsed_min <= 10.0 and temp_jump_f > 15.0:
                            reason = (
                                f"Rate-of-change spike ({reading.temp_f:.1f}F vs previous {prev_tf:.1f}F "
                                f"in {elapsed_min:.1f}m)"
                            )
                            logger.warning("[AUTO-EXPUNGED OUTLIER] Rejected %s: %s", sensor_key, reason)
                            self._record_expunged_packet(reading, reason)
                            return None

                loc_type = existing["location_type"] if existing is not None else "ignore"
                pkt_cnt = int(existing["packet_count"] or 0) if existing is not None else 0
                if loc_type == "outdoor":
                    outdoor_median_f = self._get_active_outdoor_median_temp_f()
                    if outdoor_median_f is not None:
                        dev_f = abs(reading.temp_f - outdoor_median_f)
                        low_snr = reading.snr is not None and reading.snr < 9.0
                        cfg_thresh = float(self.get_setting("outlier_threshold_f", "22.0") or "22.0")
                        max_allowed_dev = min(15.0, cfg_thresh) if (pkt_cnt <= 2 and low_snr) else max(8.0, cfg_thresh)
                        if dev_f > max_allowed_dev:
                            reason = (
                                f"Deviated {dev_f:.1f}F from outdoor ensemble median "
                                f"({reading.temp_f:.1f}F vs {outdoor_median_f:.1f}F, SNR={reading.snr}dB)"
                            )
                            logger.warning("[AUTO-EXPUNGED OUTLIER] Rejected %s: %s", sensor_key, reason)
                            self._record_expunged_packet(reading, reason)
                            return None

            utc_ts = format_utc_z(reading.timestamp)
            battery_int: Optional[int] = None
            if reading.battery_ok is not None:
                battery_int = 1 if reading.battery_ok else 0

            # Delta Strike Calculation for AcuRite 06045M Lightning Detector & similar sensors:
            # Query previous strike_count BEFORE any untrusted sensor row cleanup occurs.
            prev_strike_count: Optional[int] = None
            if reading.strike_count is not None:
                prev_strike_row = self._conn.execute(
                    """
                    SELECT strike_count FROM sensor_readings
                    WHERE model = ? AND sensor_id = ? AND strike_count IS NOT NULL
                    ORDER BY timestamp DESC, id DESC LIMIT 1;
                    """,
                    (reading.model, reading.sensor_id),
                ).fetchone()
                if prev_strike_row is not None and prev_strike_row["strike_count"] is not None:
                    prev_strike_count = int(prev_strike_row["strike_count"])

            # Opt-In Trust Model: Newly discovered sensors register as location_type='ignore' and is_trusted=0
            if existing is None:
                batt_changed_at = utc_ts if battery_int == 1 else None
                batt_low_at = utc_ts if battery_int == 0 else None
                with self._conn:
                    self._conn.execute(
                        """
                        INSERT INTO sensors (
                            sensor_key, model, sensor_id, channel, alias,
                            location_type, is_trusted, hidden_metrics,
                            first_seen, last_seen, battery_ok,
                            last_rssi, last_snr, last_noise, packet_count, expunged_count,
                            battery_changed_at, battery_low_at, estimated_battery_days
                        ) VALUES (?, ?, ?, ?, NULL, 'ignore', 0, '[]', ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, NULL);
                        """,
                        (
                            sensor_key,
                            reading.model,
                            reading.sensor_id,
                            reading.channel,
                            utc_ts,
                            utc_ts,
                            battery_int,
                            reading.rssi,
                            reading.snr,
                            reading.noise,
                            batt_changed_at,
                            batt_low_at,
                        ),
                    )
            else:
                prev_batt = existing["battery_ok"]
                batt_changed_at = existing["battery_changed_at"]
                batt_low_at = existing["battery_low_at"]
                est_days = existing["estimated_battery_days"]
                new_pkt_count = int(existing["packet_count"] or 0) + 1

                if battery_int == 0 and (prev_batt is None or int(prev_batt) == 1):
                    batt_low_at = utc_ts
                    start_ref = parse_utc_iso(batt_changed_at or existing["first_seen"])
                    now_dt = parse_utc_iso(utc_ts)
                    if start_ref and now_dt:
                        cycle_days = max(1, int((now_dt - start_ref).total_seconds() // 86400))
                        est_days = cycle_days
                elif battery_int == 1 and prev_batt is not None and int(prev_batt) == 0:
                    batt_changed_at = utc_ts
                    batt_low_at = None
                elif battery_int == 1 and batt_changed_at is None:
                    batt_changed_at = existing["first_seen"] or utc_ts

                with self._conn:
                    self._conn.execute(
                        """
                        UPDATE sensors
                        SET
                            last_seen = ?,
                            channel = COALESCE(?, channel),
                            battery_ok = COALESCE(?, battery_ok),
                            last_rssi = COALESCE(?, last_rssi),
                            last_snr = COALESCE(?, last_snr),
                            last_noise = COALESCE(?, last_noise),
                            packet_count = ?,
                            battery_changed_at = ?,
                            battery_low_at = ?,
                            estimated_battery_days = ?
                        WHERE sensor_key = ?;
                        """,
                        (
                            utc_ts,
                            reading.channel,
                            battery_int,
                            reading.rssi,
                            reading.snr,
                            reading.noise,
                            new_pkt_count,
                            batt_changed_at,
                            batt_low_at,
                            est_days,
                            sensor_key,
                        ),
                    )

                # If sensor is currently in 'ignore' / untrusted state, keep only its latest reading row
                # so the user can see live telemetry on the Sensors Manager card to identify it,
                # without accumulating historical rows for unconfigured neighborhood sensors.
                if existing["location_type"] == "ignore" or int(existing["is_trusted"] or 0) == 0:
                    with self._conn:
                        self._conn.execute(
                            "DELETE FROM sensor_readings WHERE model = ? AND sensor_id = ?;",
                            (reading.model, reading.sensor_id),
                        )

            # Log delta lightning strikes if new_count > old_count (or 8-bit 0-255 wrap-around)
            delta_strikes = 0
            strike_confirmed = 1
            if reading.strike_count is not None and prev_strike_count is not None:
                new_cnt = int(reading.strike_count)
                if new_cnt > prev_strike_count:
                    delta_strikes = new_cnt - prev_strike_count
                elif prev_strike_count >= 200 and new_cnt < 50:
                    delta_strikes = (256 - prev_strike_count) + new_cnt

                # Guard against bogus initial jumps (> 50 strikes in a single packet)
                if 0 < delta_strikes <= 50:
                    raw_d = reading.storm_dist_mi
                    if raw_d is not None and 1.0 <= float(raw_d) <= 25.0 and int(round(float(raw_d))) != 31:
                        dist_mi = float(raw_d)
                    else:
                        dist_mi = 12.0
                    dist_mi = max(1.0, min(25.0, dist_mi))
                    rfi_flag = int(reading.rfi_detected or 0)

                    # Smart Appliance / Freezer Compressor EMI Isolation Filter (v0.0.31):
                    # Check manual daily override first (localtime), then 15m Burst Density (>= 3 packets),
                    # then Multi-Factor Weather Corroboration (local rain, dew spread < 4.0°F, NWS rain/storm).
                    local_date_row = self._conn.execute(
                        "SELECT DATE(?, 'localtime') AS local_d;",
                        (utc_ts,),
                    ).fetchone()
                    local_date_str = str(local_date_row["local_d"]) if local_date_row and local_date_row["local_d"] else ""

                    ov_row = None
                    if local_date_str:
                        ov_row = self._conn.execute(
                            """
                            SELECT corrected_condition_type, exclude_false_lightning
                            FROM daily_weather_overrides
                            WHERE date_str = ?;
                            """,
                            (local_date_str,),
                        ).fetchone()

                    cutoff_15m = format_utc_z(reading.timestamp - timedelta(minutes=15))
                    recent_row = self._conn.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM lightning_strikes
                        WHERE timestamp >= ? AND timestamp <= ?;
                        """,
                        (cutoff_15m, utc_ts),
                    ).fetchone()
                    prior_cnt = int(recent_row["cnt"] or 0) if recent_row else 0

                    confirmed_val = 0
                    promote_burst = False
                    filter_reason = "Isolated Appliance / EMI Transient"

                    if ov_row is not None and (
                        int(ov_row["exclude_false_lightning"] or 0) == 1
                        or (
                            ov_row["corrected_condition_type"]
                            and str(ov_row["corrected_condition_type"]) != "storm"
                        )
                    ):
                        confirmed_val = 0
                        filter_reason = f"Manual Day Override ({ov_row['corrected_condition_type'] or 'clear'})"
                    elif ov_row is not None and str(ov_row["corrected_condition_type"] or "") == "storm":
                        confirmed_val = 1
                        filter_reason = "Manual Day Override (storm)"
                    elif prior_cnt >= 2:
                        # 3rd+ distinct strike packet within 15 minutes -> genuine storm burst!
                        confirmed_val = 1
                        promote_burst = True
                        filter_reason = f"Burst Density ({prior_cnt + 1} packets in 15m)"
                    else:
                        has_corr, corr_reason = self._has_storm_weather_corroboration(
                            reading.timestamp,
                            is_historical=False,
                        )
                        if has_corr:
                            confirmed_val = 1
                            filter_reason = corr_reason

                    if confirmed_val == 0:
                        strike_confirmed = 0
                        logger.warning(
                            "[LIGHTNING NOISE GATE] Filtered EMI/static strike pulse from %s: "
                            "delta=%d, dist=%.1f mi, 15m_prior=%d -> confirmed=0 (%s)",
                            sensor_key,
                            delta_strikes,
                            dist_mi,
                            prior_cnt,
                            filter_reason,
                        )
                    else:
                        strike_confirmed = 1

                    with self._conn:
                        self._conn.execute(
                            """
                            INSERT INTO lightning_strikes (
                                timestamp, sensor_key, delta_strikes, distance_mi, rfi, confirmed
                            ) VALUES (?, ?, ?, ?, ?, ?);
                            """,
                            (utc_ts, sensor_key, delta_strikes, round(dist_mi, 1), rfi_flag, confirmed_val),
                        )
                        # If >= 3 distinct packets hit within 15m, promote prior unconfirmed packets in that window:
                        if promote_burst:
                            self._conn.execute(
                                """
                                UPDATE lightning_strikes
                                SET confirmed = 1
                                WHERE timestamp >= ? AND timestamp <= ? AND COALESCE(confirmed, 0) = 0;
                                """,
                                (cutoff_15m, utc_ts),
                            )

            # Ignore storm_dist == 31 / standby distances when delta_strikes == 0 or unconfirmed EMI pop
            valid_storm_dist: Optional[float] = None
            if (
                reading.storm_dist_mi is not None
                and delta_strikes > 0
                and strike_confirmed == 1
                and 1.0 <= float(reading.storm_dist_mi) <= 25.0
                and int(round(float(reading.storm_dist_mi))) != 31
            ):
                valid_storm_dist = round(float(reading.storm_dist_mi), 1)
            rfi_to_store = reading.rfi_detected if strike_confirmed == 1 else 0

            reading_params = (
                utc_ts,
                reading.sensor_id,
                reading.model,
                sensor_key,
                reading.channel,
                battery_int,
                reading.temp_c,
                reading.humidity,
                reading.pressure_hpa,
                reading.wind_avg_mps,
                reading.wind_gust_mps,
                reading.wind_dir_deg,
                reading.rain_cumulative_mm,
                reading.rain_rate_mm_h,
                reading.uv_index,
                reading.lux,
                reading.strike_count,
                valid_storm_dist,
                rfi_to_store,
                reading.rssi,
                reading.snr,
                reading.noise,
                reading.raw_json,
            )

            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO sensor_readings (
                        timestamp, sensor_id, model, sensor_key, channel, battery_ok,
                        temp_c, humidity, pressure_hpa, wind_avg_mps, wind_gust_mps,
                        wind_dir_deg, rain_cumulative_mm, rain_rate_mm_h,
                        uv_index, lux, strike_count, storm_dist_mi, rfi_detected,
                        rssi, snr, noise, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    reading_params,
                )
                return int(cursor.lastrowid or 0)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a configuration value from `system_settings`."""
        with self._lock:
            if self._conn is None:
                return default
            row = self._conn.execute(
                "SELECT value FROM system_settings WHERE key = ?;",
                (str(key),),
            ).fetchone()
            if row and row["value"] is not None:
                return str(row["value"])
            return default

    def set_setting(self, key: str, value: str) -> None:
        """Persist a single key/value pair into `system_settings` with `updated_at` timestamp."""
        now_iso = format_utc_z(datetime.now(timezone.utc))
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at;
                    """,
                    (str(key), str(value), now_iso),
                )
            try:
                self._conn.commit()
            except Exception:
                pass

    def get_public_settings(self) -> dict[str, Any]:
        """Return non-secret station, location, ingestion, and unit settings for the WebUI."""
        with self._lock:
            if self._conn is None:
                return dict(DEFAULT_SYSTEM_SETTINGS)
            rows = self._conn.execute("SELECT key, value FROM system_settings;").fetchall()
            kv = {str(r["key"]): str(r["value"]) for r in rows}

        merged: dict[str, Any] = {}
        for k, def_v in DEFAULT_SYSTEM_SETTINGS.items():
            raw_v = kv.get(k, def_v)
            if k in ("latitude", "longitude", "elevation_m", "outlier_threshold_f", "dedup_window_seconds"):
                try:
                    merged[k] = float(raw_v)
                except ValueError:
                    merged[k] = float(def_v)
            elif k in ("inactivity_timeout_minutes", "radar_zoom"):
                try:
                    merged[k] = int(float(raw_v))
                except ValueError:
                    merged[k] = int(def_v)
            elif k in ("auto_update_enabled", "auto_expunge_enabled", "ntp_auto_sync"):
                merged[k] = str(raw_v).strip().lower() in ("1", "true", "yes", "on")
            else:
                merged[k] = str(raw_v)
        return merged

    def update_public_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Validate, sanitize, and persist public system settings."""
        if not isinstance(updates, dict):
            raise ValueError("Settings payload must be a JSON object.")

        now_iso = format_utc_z(datetime.now(timezone.utc))
        to_save: list[tuple[str, str, str]] = []

        if "station_name" in updates and updates["station_name"] is not None:
            name_val = str(updates["station_name"]).strip()[:80]
            if name_val:
                to_save.append(("station_name", name_val, now_iso))

        if "city_state_label" in updates and updates["city_state_label"] is not None:
            cs_val = str(updates["city_state_label"]).strip()[:120]
            to_save.append(("city_state_label", cs_val, now_iso))

        if "latitude" in updates and updates["latitude"] is not None:
            lat = max(-90.0, min(90.0, float(updates["latitude"])))
            to_save.append(("latitude", f"{lat:.4f}", now_iso))

        if "longitude" in updates and updates["longitude"] is not None:
            lon = max(-180.0, min(180.0, float(updates["longitude"])))
            to_save.append(("longitude", f"{lon:.4f}", now_iso))

        if "elevation_m" in updates and updates["elevation_m"] is not None:
            elev = max(-500.0, min(9000.0, float(updates["elevation_m"])))
            to_save.append(("elevation_m", f"{elev:.1f}", now_iso))

        if "radar_zoom" in updates and updates["radar_zoom"] is not None:
            zoom = max(3, min(14, int(float(updates["radar_zoom"]))))
            to_save.append(("radar_zoom", str(zoom), now_iso))

        if "nwr_stream_url" in updates and updates["nwr_stream_url"] is not None:
            nwr_url = str(updates["nwr_stream_url"]).strip()[:500]
            to_save.append(("nwr_stream_url", nwr_url, now_iso))

        if "inactivity_timeout_minutes" in updates and updates["inactivity_timeout_minutes"] is not None:
            timeout_m = max(1, min(240, int(float(updates["inactivity_timeout_minutes"]))))
            to_save.append(("inactivity_timeout_minutes", str(timeout_m), now_iso))

        if "outlier_threshold_f" in updates and updates["outlier_threshold_f"] is not None:
            thresh_f = max(5.0, min(60.0, float(updates["outlier_threshold_f"])))
            to_save.append(("outlier_threshold_f", f"{thresh_f:.1f}", now_iso))

        if "dedup_window_seconds" in updates and updates["dedup_window_seconds"] is not None:
            dedup_s = max(0.0, min(60.0, float(updates["dedup_window_seconds"])))
            to_save.append(("dedup_window_seconds", f"{dedup_s:.1f}", now_iso))

        if "auto_update_enabled" in updates and updates["auto_update_enabled"] is not None:
            val_b = bool(updates["auto_update_enabled"])
            if isinstance(updates["auto_update_enabled"], str):
                val_b = updates["auto_update_enabled"].strip().lower() in ("1", "true", "yes", "on")
            to_save.append(("auto_update_enabled", "true" if val_b else "false", now_iso))

        if "auto_expunge_enabled" in updates and updates["auto_expunge_enabled"] is not None:
            val_b = bool(updates["auto_expunge_enabled"])
            if isinstance(updates["auto_expunge_enabled"], str):
                val_b = updates["auto_expunge_enabled"].strip().lower() in ("1", "true", "yes", "on")
            to_save.append(("auto_expunge_enabled", "true" if val_b else "false", now_iso))

        if "temp_unit" in updates and updates["temp_unit"] is not None:
            tu = str(updates["temp_unit"]).strip().upper()
            if tu in ("F", "C"):
                to_save.append(("temp_unit", tu, now_iso))

        if "wind_unit" in updates and updates["wind_unit"] is not None:
            wu = str(updates["wind_unit"]).strip().lower()
            if wu in ("mph", "m/s", "knots"):
                to_save.append(("wind_unit", wu, now_iso))

        if "pressure_unit" in updates and updates["pressure_unit"] is not None:
            pu = str(updates["pressure_unit"]).strip()
            if pu in ("inHg", "hPa"):
                to_save.append(("pressure_unit", pu, now_iso))

        if "rain_unit" in updates and updates["rain_unit"] is not None:
            ru = str(updates["rain_unit"]).strip().lower()
            if ru in ("in", "mm"):
                to_save.append(("rain_unit", ru, now_iso))

        if "auto_vacuum_schedule" in updates and updates["auto_vacuum_schedule"] is not None:
            sched = str(updates["auto_vacuum_schedule"]).strip().lower()
            if sched in ("off", "daily", "weekly"):
                to_save.append(("auto_vacuum_schedule", sched, now_iso))

        if "ntp_auto_sync" in updates and updates["ntp_auto_sync"] is not None:
            val_b = bool(updates["ntp_auto_sync"])
            if isinstance(updates["ntp_auto_sync"], str):
                val_b = updates["ntp_auto_sync"].strip().lower() in ("1", "true", "yes", "on")
            to_save.append(("ntp_auto_sync", "true" if val_b else "false", now_iso))

        if to_save:
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("Database connection is closed.")
                with self._conn:
                    self._conn.executemany(
                        """
                        INSERT INTO system_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at;
                        """,
                        to_save,
                    )

        return self.get_public_settings()

    def get_db_path(self) -> Path:
        """Return the resolved filesystem Path of the SQLite database."""
        return Path(self.db_path).resolve()

    def _measure_disk_files_bytes(self) -> dict[str, int]:
        """Measure exact on-disk size in bytes of `skyscry.db`, `skyscry.db-wal`, and `skyscry.db-shm`."""
        sizes = {"db_bytes": 0, "wal_bytes": 0, "shm_bytes": 0, "total_bytes": 0}
        resolved = self.get_db_path()
        if resolved.name == ":memory:":
            return sizes
        mapping = (("", "db_bytes"), ("-wal", "wal_bytes"), ("-shm", "shm_bytes"))
        for suffix, key in mapping:
            candidate = Path(f"{resolved}{suffix}")
            if candidate.is_file():
                try:
                    sz = int(candidate.stat().st_size)
                    sizes[key] = sz
                    sizes["total_bytes"] += sz
                except OSError:
                    pass
        return sizes

    def vacuum_and_optimize_db(self) -> dict[str, Any]:
        """Perform WAL-truncating SQLite maintenance (`wal_checkpoint(TRUNCATE)`, `VACUUM`, `ANALYZE`, `optimize`) using an isolated autocommit connection."""
        t0 = time.perf_counter()
        db_path_obj = self.get_db_path()
        db_path_str = str(db_path_obj)

        if db_path_obj.name == ":memory:":
            return {
                "ok": True,
                "steps": ["In-memory database vacuum skipped"],
                "before_mb": 0.0,
                "after_mb": 0.0,
                "saved_mb": 0.0,
                "main_before_mb": 0.0,
                "wal_before_mb": 0.0,
                "main_after_mb": 0.0,
                "wal_after_mb": 0.0,
            }

        # Measure exact file sizes on disk BEFORE running
        main_before = os.path.getsize(db_path_str) if os.path.exists(db_path_str) else 0
        wal_before = os.path.getsize(db_path_str + "-wal") if os.path.exists(db_path_str + "-wal") else 0
        shm_before = os.path.getsize(db_path_str + "-shm") if os.path.exists(db_path_str + "-shm") else 0
        total_before_bytes = main_before + wal_before + shm_before
        total_before_mb = round(total_before_bytes / (1024.0 * 1024.0), 2)
        main_before_mb = round(main_before / (1024.0 * 1024.0), 2)
        wal_before_mb = round(wal_before / (1024.0 * 1024.0), 2)

        steps: list[str] = []

        with self._lock:
            # 1. Clean up orphaned/ignored telemetry rows on primary connection if open
            if self._conn is not None:
                try:
                    with self._conn:
                        cur_orph = self._conn.execute(
                            """
                            DELETE FROM sensor_readings
                            WHERE COALESCE(sensor_key, model || '_' || sensor_id) IN (
                                SELECT sensor_key FROM sensors
                                WHERE location_type = 'ignore' OR is_trusted = 0
                            )
                            AND id NOT IN (
                                SELECT MAX(id) FROM sensor_readings
                                GROUP BY COALESCE(sensor_key, model || '_' || sensor_id)
                            );
                            """
                        )
                        purged = int(cur_orph.rowcount or 0)
                        if purged > 0:
                            steps.append(f"Purged {purged} ignored/orphaned telemetry rows")
                    self._conn.commit()
                except Exception as exc:
                    logger.debug("Orphan purge detail: %s", exc)

            # 2. Execute VACUUM & checkpoints using isolated connection with isolation_level=None (autocommit mode)
            # Autocommit mode is strictly required by SQLite for VACUUM and wal_checkpoint(TRUNCATE) to shrink the file on disk!
            try:
                iso_conn = sqlite3.connect(db_path_str, timeout=30.0, isolation_level=None)
                try:
                    iso_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    steps.append("Checkpoint WAL (TRUNCATE)")

                    iso_conn.execute("VACUUM;")
                    steps.append("Vacuum Pages")

                    iso_conn.execute("ANALYZE;")
                    steps.append("Analyze Statistics")

                    iso_conn.execute("PRAGMA optimize;")
                    steps.append("Optimize Indexes")

                    iso_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    steps.append("Final WAL Truncate")
                finally:
                    iso_conn.close()
            except Exception as exc:
                logger.error("Vacuum execution error: %s", exc)
                steps.append(f"Vacuum error: {exc}")

        # Measure exact file sizes on disk AFTER completion
        main_after = os.path.getsize(db_path_str) if os.path.exists(db_path_str) else 0
        wal_after = os.path.getsize(db_path_str + "-wal") if os.path.exists(db_path_str + "-wal") else 0
        shm_after = os.path.getsize(db_path_str + "-shm") if os.path.exists(db_path_str + "-shm") else 0
        total_after_bytes = main_after + wal_after + shm_after
        total_after_mb = round(total_after_bytes / (1024.0 * 1024.0), 2)
        main_after_mb = round(main_after / (1024.0 * 1024.0), 2)
        wal_after_mb = round(wal_after / (1024.0 * 1024.0), 2)
        saved_bytes = max(0, total_before_bytes - total_after_bytes)
        saved_mb = round(saved_bytes / (1024.0 * 1024.0), 2)
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        now_iso = format_utc_z(datetime.now(timezone.utc))
        try:
            self.set_setting("last_vacuum_at", now_iso)
        except Exception:
            pass

        return {
            "ok": True,
            "steps": steps,
            "before_mb": total_before_mb,
            "after_mb": total_after_mb,
            "saved_mb": saved_mb,
            "main_before_mb": main_before_mb,
            "wal_before_mb": wal_before_mb,
            "main_after_mb": main_after_mb,
            "wal_after_mb": wal_after_mb,
            "before_bytes": total_before_bytes,
            "after_bytes": total_after_bytes,
            "saved_bytes": saved_bytes,
            "duration_ms": duration_ms,
            "completed_at": now_iso,
            "size_before_mb": total_before_mb,
            "size_after_mb": total_after_mb,
            "reclaimed_mb": saved_mb,
        }

    def vacuum_database(self) -> dict[str, Any]:
        """Run full WAL-truncating vacuum and optimization."""
        return self.vacuum_and_optimize_db()

    def get_telemetry_learning_stats(self) -> dict[str, Any]:
        """Return total RF packets analyzed, active tracking uptime, and sensor telemetry metrics."""
        with self._lock:
            if self._conn is None:
                return {"total_packets": 0, "active_hours": 0.0, "active_days": 0.0}
            row = self._conn.execute(
                "SELECT COUNT(*) AS total_pkts, MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts FROM sensor_readings;"
            ).fetchone()
            total_pkts = int(row["total_pkts"] or 0) if row else 0
            first_ts = row["first_ts"] if row else None
            last_ts = row["last_ts"] if row else None
            active_hours = 0.0
            if first_ts and last_ts:
                try:
                    dt1 = datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
                    dt2 = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
                    active_hours = max(0.1, (dt2 - dt1).total_seconds() / 3600.0)
                except Exception:
                    pass
            return {
                "total_packets": total_pkts,
                "active_hours": round(active_hours, 1),
                "active_days": round(active_hours / 24.0, 1),
                "first_packet": first_ts,
                "latest_packet": last_ts,
            }

    def get_location_coordinates(
        self,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
        auto_resolve: bool = False,
    ) -> dict[str, float]:
        """Return configured station coordinates with strict priority:
        1. SQLite `system_settings` (user-saved)
        2. Non-empty Environment Variables (`LATITUDE`/`LONGITUDE` or `default_lat`/`default_lon`)
        3. Neutral unconfigured state (`0.0, 0.0`)
        """
        import os

        with self._lock:
            if self._conn is None:
                return {"latitude": float(default_lat), "longitude": float(default_lon)}
            rows = self._conn.execute(
                "SELECT key, value FROM system_settings WHERE key IN ('latitude', 'longitude');"
            ).fetchall()
            kv = {r["key"]: r["value"] for r in rows}
            try:
                lat = float(kv.get("latitude", 0.0))
                lon = float(kv.get("longitude", 0.0))
            except (ValueError, TypeError):
                lat, lon = 0.0, 0.0
            if not math.isfinite(lat) or not math.isfinite(lon):
                lat, lon = 0.0, 0.0

        # Priority 1: SQLite system_settings already has valid non-zero user coordinates
        if abs(lat) > 0.0001 or abs(lon) > 0.0001:
            return {"latitude": lat, "longitude": lon}

        # Priority 2: Non-empty Environment Variables or explicit config defaults
        env_lat_raw = os.environ.get("LATITUDE") or os.environ.get("SKYSCRY_LATITUDE") or ""
        env_lon_raw = os.environ.get("LONGITUDE") or os.environ.get("SKYSCRY_LONGITUDE") or ""
        env_lat, env_lon = float(default_lat or 0.0), float(default_lon or 0.0)
        if env_lat_raw.strip() and env_lon_raw.strip():
            try:
                e_lat = float(env_lat_raw.strip())
                e_lon = float(env_lon_raw.strip())
                if math.isfinite(e_lat) and math.isfinite(e_lon):
                    env_lat, env_lon = e_lat, e_lon
            except (ValueError, TypeError):
                pass

        if abs(env_lat) > 0.0001 or abs(env_lon) > 0.0001:
            try:
                self.set_location_coordinates(env_lat, env_lon)
            except Exception as exc:
                logger.debug("Could not persist env coordinates to system_settings: %s", exc)
            return {"latitude": env_lat, "longitude": env_lon}

        # Optional IP auto-resolve only when explicitly requested
        if auto_resolve:
            resolved = auto_resolve_ip_coordinates()
            if resolved:
                r_lat, r_lon, label = resolved
                try:
                    self.set_location_coordinates(r_lat, r_lon)
                    if label and not self.get_setting("city_state_label"):
                        self.set_setting("city_state_label", label)
                    lat, lon = r_lat, r_lon
                except Exception as exc:
                    logger.debug("Failed saving auto-resolved coordinates: %s", exc)

        # Priority 3: Neutral unconfigured state (0.0, 0.0)
        return {"latitude": lat, "longitude": lon}

    def set_location_coordinates(self, latitude: float, longitude: float) -> dict[str, float]:
        """Persist station coordinates in `system_settings`."""
        lat = max(-90.0, min(90.0, float(latitude)))
        lon = max(-180.0, min(180.0, float(longitude)))
        now_iso = format_utc_z(datetime.now(timezone.utc))
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            with self._conn:
                self._conn.execute(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES ('latitude', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                    (f"{lat:.4f}", now_iso),
                )
                self._conn.execute(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES ('longitude', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                    (f"{lon:.4f}", now_iso),
                )
        return {"latitude": lat, "longitude": lon}

    def upsert_reference_history(self, records: list[dict[str, Any]]) -> int:
        """Insert or update regional barometric pressure & weather backfill records."""
        if not records:
            return 0
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            params = [
                (
                    r["timestamp"],
                    r.get("pressure_hpa"),
                    r.get("surface_pressure_hpa"),
                    r.get("temp_c"),
                    r.get("humidity"),
                    r.get("wind_avg_mps"),
                    r.get("wind_gust_mps"),
                    r.get("wind_dir_deg"),
                    r.get("rain_mm"),
                )
                for r in records
            ]
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT INTO reference_weather_history (
                        timestamp, pressure_hpa, surface_pressure_hpa,
                        temp_c, humidity, wind_avg_mps, wind_gust_mps, wind_dir_deg, rain_mm
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(timestamp) DO UPDATE SET
                        pressure_hpa = COALESCE(excluded.pressure_hpa, reference_weather_history.pressure_hpa),
                        surface_pressure_hpa = COALESCE(excluded.surface_pressure_hpa, reference_weather_history.surface_pressure_hpa),
                        temp_c = COALESCE(excluded.temp_c, reference_weather_history.temp_c),
                        humidity = COALESCE(excluded.humidity, reference_weather_history.humidity),
                        wind_avg_mps = COALESCE(excluded.wind_avg_mps, reference_weather_history.wind_avg_mps),
                        wind_gust_mps = COALESCE(excluded.wind_gust_mps, reference_weather_history.wind_gust_mps),
                        wind_dir_deg = COALESCE(excluded.wind_dir_deg, reference_weather_history.wind_dir_deg),
                        rain_mm = COALESCE(excluded.rain_mm, reference_weather_history.rain_mm);
                    """,
                    params,
                )
            return len(params)

    def get_sensor_alias(self, model: str, sensor_id: str) -> Optional[str]:
        """Return the user-configured alias for a sensor, or None if no alias is set."""
        sensor_key = self.make_sensor_key(model, sensor_id)
        with self._lock:
            if self._conn is None:
                return None
            cursor = self._conn.execute(
                "SELECT alias FROM sensors WHERE sensor_key = ?;",
                (sensor_key,),
            )
            row = cursor.fetchone()
            if row and row["alias"]:
                cleaned = str(row["alias"]).strip()
                return cleaned if cleaned else None
            return None

    def get_all_sensors(self) -> list[dict[str, Any]]:
        """Return all registered sensors sorted deterministically by display name ASC."""
        query = """
        SELECT
            s.sensor_key,
            s.model,
            s.sensor_id,
            s.channel,
            s.alias,
            s.location_type,
            s.is_trusted,
            s.hidden_metrics,
            s.first_seen,
            s.last_seen,
            s.battery_ok,
            s.last_rssi,
            s.last_snr,
            s.last_noise,
            s.packet_count,
            s.expunged_count,
            s.last_expunged_reason,
            s.battery_changed_at,
            s.battery_low_at,
            s.estimated_battery_days,
            r.id AS reading_id,
            r.timestamp AS reading_timestamp,
            r.temp_c,
            r.humidity,
            r.pressure_hpa,
            r.wind_avg_mps,
            r.wind_gust_mps,
            r.wind_dir_deg,
            r.rain_cumulative_mm,
            r.rain_rate_mm_h,
            r.uv_index,
            r.lux,
            r.strike_count,
            r.storm_dist_mi,
            r.rfi_detected,
            r.rssi,
            r.snr,
            r.noise
        FROM sensors s
        LEFT JOIN sensor_readings r ON r.id = (
            SELECT sr.id
            FROM sensor_readings sr
            WHERE sr.model = s.model AND sr.sensor_id = s.sensor_id
            ORDER BY sr.timestamp DESC, sr.id DESC
            LIMIT 1
        )
        ORDER BY LOWER(COALESCE(NULLIF(TRIM(s.alias), ''), s.model || ' #' || s.sensor_id)) ASC,
                 s.sensor_key ASC;
        """
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            cursor = self._conn.execute(query)
            rows = cursor.fetchall()

        now_utc = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []
        for row in rows:
            raw = dict(row)
            alias = str(raw["alias"]).strip() if raw.get("alias") else None
            display_name = alias if alias else f"{raw['model']} #{raw['sensor_id']}"
            battery_val = raw.get("battery_ok")
            battery_bool = bool(int(battery_val) != 0) if battery_val is not None else None
            hidden_metrics = _parse_hidden_metrics(raw.get("hidden_metrics"))

            first_seen_dt = parse_utc_iso(raw.get("first_seen"))
            last_seen_dt = parse_utc_iso(raw.get("last_seen"))
            batt_changed_dt = parse_utc_iso(raw.get("battery_changed_at")) or first_seen_dt

            days_on_current_battery: Optional[int] = None
            if batt_changed_dt is not None:
                days_on_current_battery = max(0, int((now_utc - batt_changed_dt).total_seconds() // 86400))

            last_rssi = raw.get("last_rssi") if raw.get("last_rssi") is not None else raw.get("rssi")
            last_snr = raw.get("last_snr") if raw.get("last_snr") is not None else raw.get("snr")
            last_noise = raw.get("last_noise") if raw.get("last_noise") is not None else raw.get("noise")

            latest_reading: Optional[dict[str, Any]] = None
            if raw.get("reading_id") is not None:
                latest_reading = _enrich_reading_dict(
                    {
                        "id": raw["reading_id"],
                        "timestamp": raw["reading_timestamp"],
                        "temp_c": raw["temp_c"],
                        "humidity": raw["humidity"],
                        "pressure_hpa": raw["pressure_hpa"],
                        "wind_avg_mps": raw["wind_avg_mps"],
                        "wind_gust_mps": raw["wind_gust_mps"],
                        "wind_dir_deg": raw["wind_dir_deg"],
                        "rain_cumulative_mm": raw["rain_cumulative_mm"],
                        "rain_rate_mm_h": raw["rain_rate_mm_h"],
                        "uv_index": raw["uv_index"],
                        "lux": raw["lux"],
                        "strike_count": raw.get("strike_count"),
                        "storm_dist_mi": raw.get("storm_dist_mi"),
                        "rfi_detected": raw.get("rfi_detected"),
                        "rssi": last_rssi,
                        "snr": last_snr,
                        "noise": last_noise,
                        "battery_ok": battery_val,
                    }
                )

            results.append(
                {
                    "sensor_key": raw["sensor_key"],
                    "model": raw["model"],
                    "sensor_id": raw["sensor_id"],
                    "channel": raw["channel"],
                    "alias": alias,
                    "display_name": display_name,
                    "location_type": raw.get("location_type") or "ignore",
                    "is_trusted": bool(int(raw.get("is_trusted") or 0) != 0),
                    "hidden_metrics": hidden_metrics,
                    "first_seen": format_utc_z(first_seen_dt) if first_seen_dt else raw["first_seen"],
                    "last_seen": format_utc_z(last_seen_dt) if last_seen_dt else raw["last_seen"],
                    "battery_ok": battery_bool,
                    "battery_changed_at": format_utc_z(batt_changed_dt) if batt_changed_dt else None,
                    "battery_low_at": raw.get("battery_low_at"),
                    "days_on_current_battery": days_on_current_battery,
                    "estimated_battery_days": raw.get("estimated_battery_days"),
                    "packet_count": int(raw.get("packet_count") or 0),
                    "expunged_count": int(raw.get("expunged_count") or 0),
                    "last_expunged_reason": raw.get("last_expunged_reason"),
                    "last_rssi": last_rssi,
                    "last_snr": last_snr,
                    "last_noise": last_noise,
                    "signal_quality": compute_signal_quality(last_snr, last_rssi),
                    "signal_bars": compute_signal_bars(last_snr, last_rssi),
                    "latest_reading": latest_reading,
                }
            )
        return results

    def update_sensor_metadata(
        self,
        sensor_key: str,
        alias: Optional[str] = None,
        location_type: Optional[str] = None,
        is_trusted: Optional[bool] = None,
        hidden_metrics: Optional[list[str]] = None,
        mark_battery_replaced: bool = False,
        purge_sensor_outliers: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Update user-defined metadata, hidden_metrics, battery reset, or outlier purge for a sensor."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")

            existing = self._conn.execute(
                "SELECT * FROM sensors WHERE sensor_key = ?;",
                (sensor_key,),
            ).fetchone()
            if existing is None:
                return None

            cleaned_alias: Optional[str] = existing["alias"]
            if alias is not None:
                stripped = str(alias).strip()
                cleaned_alias = stripped if stripped else None

            resolved_location: str = existing["location_type"] or "ignore"
            if location_type is not None:
                loc_candidate = str(location_type).strip().lower()
                if loc_candidate not in VALID_LOCATION_TYPES:
                    raise ValueError(
                        f"Invalid location_type '{location_type}'. "
                        f"Must be one of: {', '.join(sorted(VALID_LOCATION_TYPES))}."
                    )
                resolved_location = loc_candidate

            resolved_trusted: int = int(existing["is_trusted"] or 0)
            if is_trusted is not None:
                resolved_trusted = 1 if bool(is_trusted) else 0
            elif resolved_location == "ignore":
                resolved_trusted = 0
            elif resolved_location in ("outdoor", "indoor") and existing["location_type"] in ("ignore", "unassigned"):
                resolved_trusted = 1

            resolved_hidden: list[str] = _parse_hidden_metrics(existing["hidden_metrics"])
            if hidden_metrics is not None:
                resolved_hidden = sorted(
                    {
                        str(m).strip().lower()
                        for m in hidden_metrics
                        if str(m).strip().lower() in VALID_HIDDEN_METRICS
                    }
                )

            batt_changed_at = existing["battery_changed_at"]
            batt_low_at = existing["battery_low_at"]
            if mark_battery_replaced:
                batt_changed_at = format_utc_z(datetime.now(timezone.utc))
                batt_low_at = None

            with self._conn:
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET alias = ?,
                        location_type = ?,
                        is_trusted = ?,
                        hidden_metrics = ?,
                        battery_changed_at = ?,
                        battery_low_at = ?
                    WHERE sensor_key = ?;
                    """,
                    (
                        cleaned_alias,
                        resolved_location,
                        resolved_trusted,
                        json.dumps(resolved_hidden),
                        batt_changed_at,
                        batt_low_at,
                        sensor_key,
                    ),
                )

            if purge_sensor_outliers:
                self.expunge_skewed_history(target_sensor_key=sensor_key)

        for sensor in self.get_all_sensors():
            if sensor["sensor_key"] == sensor_key:
                return sensor
        return None

    def get_recent_readings(
        self,
        sensor_key: Optional[str] = None,
        hours: float = 24.0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return time-series readings filtered by optional `sensor_key` and UTC time window (bounded max 2000)."""
        safe_hours = max(0.1, min(float(hours), 24.0 * 365.0))
        safe_limit = max(1, min(int(limit), 2000))
        cutoff_iso = format_utc_z(datetime.now(timezone.utc) - timedelta(hours=safe_hours))

        params: list[Any] = [cutoff_iso]
        where_clauses = ["sr.timestamp >= ?"]

        if sensor_key and sensor_key.strip():
            where_clauses.append("COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id) = ?")
            params.append(sensor_key.strip())

        params.append(safe_limit)
        where_sql = " AND ".join(where_clauses)

        query = f"""
        SELECT
            sr.id,
            sr.timestamp,
            sr.sensor_id,
            sr.model,
            COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id) AS sensor_key,
            s.alias,
            s.location_type,
            s.is_trusted,
            s.hidden_metrics,
            sr.channel,
            sr.battery_ok,
            sr.temp_c,
            sr.humidity,
            sr.pressure_hpa,
            sr.wind_avg_mps,
            sr.wind_gust_mps,
            sr.wind_dir_deg,
            sr.rain_cumulative_mm,
            sr.rain_rate_mm_h,
            sr.uv_index,
            sr.lux,
            sr.strike_count,
            sr.storm_dist_mi,
            sr.rfi_detected,
            sr.rssi,
            sr.snr,
            sr.noise
        FROM sensor_readings sr
        LEFT JOIN sensors s ON s.sensor_key = COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id)
        WHERE {where_sql}
        ORDER BY sr.timestamp DESC, sr.id DESC
        LIMIT ?;
        """

        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            item = _enrich_reading_dict(dict(row))
            alias = str(item["alias"]).strip() if item.get("alias") else None
            item["alias"] = alias
            item["display_name"] = alias if alias else f"{item['model']} #{item['sensor_id']}"
            item["is_trusted"] = (
                bool(int(item["is_trusted"]) != 0) if item.get("is_trusted") is not None else False
            )
            item["hidden_metrics"] = _parse_hidden_metrics(item.get("hidden_metrics"))
            results.append(item)
        return results

    def get_bucketed_chart_series(
        self,
        hours: float = 24.0,
        bucket_minutes: Optional[int] = None,
        location_filter: str = "outdoor",
        locations: Optional[list[str]] = None,
        sensor_keys: Optional[list[str]] = None,
        include_reference: bool = False,
    ) -> dict[str, Any]:
        """Compute time-bucketed, smoothed chart series with multi-checkbox location & sensor filtering."""
        safe_hours = max(0.5, min(float(hours), 24.0 * 30.0))
        if bucket_minutes is None or bucket_minutes <= 0:
            if safe_hours <= 1.5:
                resolved_bucket_min = 1
            elif safe_hours <= 6.5:
                resolved_bucket_min = 5
            elif safe_hours <= 24.5:
                resolved_bucket_min = 15
            else:
                resolved_bucket_min = 60
        else:
            resolved_bucket_min = max(1, min(int(bucket_minutes), 360))

        bucket_sec = resolved_bucket_min * 60
        now_utc = datetime.now(timezone.utc)
        cutoff_dt = now_utc - timedelta(hours=safe_hours)
        cutoff_iso = format_utc_z(cutoff_dt)

        all_sensors = {s["sensor_key"]: s for s in self.get_all_sensors()}

        # Determine requested location set (default strictly to {"outdoor"} if not specified)
        active_locs: set[str] = set()
        if locations is not None:
            for loc_item in locations:
                cleaned_loc = str(loc_item).strip().lower()
                if cleaned_loc in ("outdoor", "indoor", "reference"):
                    if cleaned_loc == "reference":
                        include_reference = True
                    else:
                        active_locs.add(cleaned_loc)
        else:
            loc_filter_norm = (location_filter or "outdoor").strip().lower()
            if loc_filter_norm == "all":
                active_locs = {"outdoor", "indoor"}
            elif loc_filter_norm == "indoor":
                active_locs = {"indoor"}
            elif loc_filter_norm == "none":
                active_locs = set()
            else:
                active_locs = {"outdoor"}

        explicit_sensor_set: Optional[set[str]] = None
        if sensor_keys is not None:
            explicit_sensor_set = {str(k).strip() for k in sensor_keys if str(k).strip()}

        eligible_sensors: dict[str, dict[str, Any]] = {}
        for skey, smeta in all_sensors.items():
            loc = smeta["location_type"]
            if loc == "ignore" or not smeta["is_trusted"]:
                continue

            if explicit_sensor_set is not None:
                if skey in explicit_sensor_set:
                    eligible_sensors[skey] = smeta
                continue

            if loc in active_locs:
                eligible_sensors[skey] = smeta

        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            rows = self._conn.execute(
                """
                SELECT
                    timestamp,
                    COALESCE(sensor_key, model || '_' || sensor_id) AS sensor_key,
                    temp_c,
                    humidity,
                    pressure_hpa,
                    wind_avg_mps,
                    wind_gust_mps,
                    rain_cumulative_mm
                FROM sensor_readings
                WHERE timestamp >= ?
                  AND (temp_c IS NULL OR (temp_c >= -31.67 AND temp_c <= 54.44))
                ORDER BY timestamp ASC;
                """,
                (cutoff_iso,),
            ).fetchall()

            ref_rows = self._conn.execute(
                """
                SELECT timestamp, pressure_hpa, temp_c, humidity, wind_avg_mps, wind_gust_mps, rain_mm
                FROM reference_weather_history
                WHERE timestamp >= ?
                ORDER BY timestamp ASC;
                """,
                (cutoff_iso,),
            ).fetchall()

        buckets: dict[int, dict[str, dict[str, list[float]]]] = {}
        baro_buckets: dict[int, list[float]] = {}
        ref_buckets: dict[int, dict[str, list[float]]] = {}
        all_bucket_epochs: set[int] = set()

        for row in rows:
            skey = row["sensor_key"]
            if skey not in eligible_sensors:
                continue
            smeta = eligible_sensors[skey]
            hidden = set(smeta.get("hidden_metrics") or [])

            dt = parse_utc_iso(row["timestamp"])
            if dt is None:
                continue
            epoch = int(dt.timestamp())
            b_epoch = (epoch // bucket_sec) * bucket_sec
            all_bucket_epochs.add(b_epoch)

            s_bucket = buckets.setdefault(b_epoch, {}).setdefault(
                skey,
                {"temp_f": [], "humidity": [], "wind_avg_mph": [], "wind_gust_mph": [], "rain_in": []},
            )

            if "temperature" not in hidden and row["temp_c"] is not None:
                tf = (float(row["temp_c"]) * 9.0 / 5.0) + 32.0
                if -25.0 <= tf <= 130.0:
                    s_bucket["temp_f"].append(tf)
            if "humidity" not in hidden and row["humidity"] is not None:
                rh_val = float(row["humidity"])
                if 1.0 <= rh_val <= 100.0:
                    s_bucket["humidity"].append(rh_val)
            if "wind" not in hidden and row["wind_avg_mps"] is not None:
                s_bucket["wind_avg_mph"].append(float(row["wind_avg_mps"]) * 2.23693629)
            if "wind" not in hidden and row["wind_gust_mps"] is not None:
                s_bucket["wind_gust_mph"].append(float(row["wind_gust_mps"]) * 2.23693629)
            if "rain" not in hidden and row["rain_cumulative_mm"] is not None:
                s_bucket["rain_in"].append(float(row["rain_cumulative_mm"]) / 25.4)
            if "pressure" not in hidden and row["pressure_hpa"] is not None:
                p_val = float(row["pressure_hpa"])
                if MIN_PLAUSIBLE_PRESSURE_HPA <= p_val <= MAX_PLAUSIBLE_PRESSURE_HPA:
                    baro_buckets.setdefault(b_epoch, []).append(p_val * HPA_TO_INHG)

        for rref in ref_rows:
            dt = parse_utc_iso(rref["timestamp"])
            if dt is None:
                continue
            b_epoch = (int(dt.timestamp()) // bucket_sec) * bucket_sec
            if rref["pressure_hpa"] is not None:
                p_val = float(rref["pressure_hpa"])
                if MIN_PLAUSIBLE_PRESSURE_HPA <= p_val <= MAX_PLAUSIBLE_PRESSURE_HPA:
                    baro_buckets.setdefault(b_epoch, []).append(p_val * HPA_TO_INHG)
                    if include_reference:
                        all_bucket_epochs.add(b_epoch)

            if include_reference:
                all_bucket_epochs.add(b_epoch)
                rb = ref_buckets.setdefault(
                    b_epoch,
                    {"temp_f": [], "humidity": [], "wind_avg_mph": [], "rain_in": []},
                )
                if rref["temp_c"] is not None:
                    rb["temp_f"].append((float(rref["temp_c"]) * 9.0 / 5.0) + 32.0)
                if rref["humidity"] is not None:
                    rb["humidity"].append(float(rref["humidity"]))
                if rref["wind_avg_mps"] is not None:
                    rb["wind_avg_mph"].append(float(rref["wind_avg_mps"]) * 2.23693629)
                if rref["rain_mm"] is not None:
                    rb["rain_in"].append(float(rref["rain_mm"]) / 25.4)

        sorted_epochs = sorted(all_bucket_epochs)
        labels_iso = [
            format_utc_z(datetime.fromtimestamp(ep, tz=timezone.utc)) for ep in sorted_epochs
        ]

        sensor_series: list[dict[str, Any]] = []
        for skey in sorted(
            eligible_sensors.keys(),
            key=lambda k: eligible_sensors[k]["display_name"].lower(),
        ):
            smeta = eligible_sensors[skey]
            hidden = set(smeta.get("hidden_metrics") or [])
            temp_pts: list[Optional[float]] = []
            rh_pts: list[Optional[float]] = []
            wind_avg_pts: list[Optional[float]] = []
            wind_gust_pts: list[Optional[float]] = []
            rain_pts: list[Optional[float]] = []

            has_temp = False
            has_rh = False
            has_wind = False
            has_rain = False

            baseline_rain: Optional[float] = None

            for ep in sorted_epochs:
                cell = buckets.get(ep, {}).get(skey)
                if cell and cell["temp_f"]:
                    val = round(sum(cell["temp_f"]) / len(cell["temp_f"]), 2)
                    temp_pts.append(val)
                    has_temp = True
                else:
                    temp_pts.append(None)

                if cell and cell["humidity"]:
                    val = round(sum(cell["humidity"]) / len(cell["humidity"]), 1)
                    rh_pts.append(val)
                    has_rh = True
                else:
                    rh_pts.append(None)

                if cell and cell["wind_avg_mph"]:
                    val = round(sum(cell["wind_avg_mph"]) / len(cell["wind_avg_mph"]), 2)
                    wind_avg_pts.append(val)
                    has_wind = True
                else:
                    wind_avg_pts.append(None)

                if cell and cell["wind_gust_mph"]:
                    val = round(max(cell["wind_gust_mph"]), 2)
                    wind_gust_pts.append(val)
                    has_wind = True
                else:
                    wind_gust_pts.append(None)

                if cell and cell["rain_in"]:
                    max_r = max(cell["rain_in"])
                    if baseline_rain is None:
                        baseline_rain = min(cell["rain_in"])
                    rel_rain = max(0.0, round(max_r - baseline_rain, 3))
                    rain_pts.append(rel_rain)
                    has_rain = True
                else:
                    rain_pts.append(None)

            sensor_series.append(
                {
                    "sensor_key": skey,
                    "display_name": smeta["display_name"],
                    "location_type": smeta["location_type"],
                    "is_trusted": smeta["is_trusted"],
                    "has_temp": has_temp and ("temperature" not in hidden),
                    "has_humidity": has_rh and ("humidity" not in hidden),
                    "has_wind": has_wind and ("wind" not in hidden),
                    "has_rain": has_rain and ("rain" not in hidden),
                    "temp_f": temp_pts,
                    "humidity": rh_pts,
                    "wind_avg_mph": wind_avg_pts,
                    "wind_gust_mph": wind_gust_pts,
                    "rain_in": rain_pts,
                }
            )

        ensemble_temp_f: list[Optional[float]] = []
        ensemble_humidity: list[Optional[float]] = []
        pressure_inhg_pts: list[Optional[float]] = []
        ref_temp_f: list[Optional[float]] = []
        ref_humidity: list[Optional[float]] = []
        ref_wind_mph: list[Optional[float]] = []

        for idx, ep in enumerate(sorted_epochs):
            t_vals = [
                s["temp_f"][idx]
                for s in sensor_series
                if s["is_trusted"]
                and s["location_type"] == "outdoor"
                and s["temp_f"][idx] is not None
            ]
            h_vals = [
                s["humidity"][idx]
                for s in sensor_series
                if s["is_trusted"]
                and s["location_type"] == "outdoor"
                and s["humidity"][idx] is not None
            ]
            ensemble_temp_f.append(round(sum(t_vals) / len(t_vals), 2) if t_vals else None)
            ensemble_humidity.append(round(sum(h_vals) / len(h_vals), 1) if h_vals else None)

            b_vals = baro_buckets.get(ep)
            pressure_inhg_pts.append(round(sum(b_vals) / len(b_vals), 2) if b_vals else None)

            if include_reference:
                rb = ref_buckets.get(ep)
                ref_temp_f.append(
                    round(sum(rb["temp_f"]) / len(rb["temp_f"]), 2) if (rb and rb["temp_f"]) else None
                )
                ref_humidity.append(
                    round(sum(rb["humidity"]) / len(rb["humidity"]), 1) if (rb and rb["humidity"]) else None
                )
                ref_wind_mph.append(
                    round(sum(rb["wind_avg_mph"]) / len(rb["wind_avg_mph"]), 2)
                    if (rb and rb["wind_avg_mph"])
                    else None
                )

        return {
            "hours": safe_hours,
            "bucket_minutes": resolved_bucket_min,
            "locations": sorted(active_locs),
            "include_reference": include_reference,
            "timestamps": labels_iso,
            "series": sensor_series,
            "ensemble": {
                "temp_f": ensemble_temp_f,
                "humidity": ensemble_humidity,
                "pressure_inhg": pressure_inhg_pts,
            },
            "reference": {
                "temp_f": ref_temp_f,
                "humidity": ref_humidity,
                "wind_avg_mph": ref_wind_mph,
            },
        }

    def get_barometer_summary(self) -> dict[str, Any]:
        """Convenience accessor to compute current barometric telemetry, pressure_inhg, and tendency."""
        return self._compute_barometric_telemetry_and_forecast()

    def _compute_barometric_telemetry_and_forecast(
        self,
        rf_press_hpa: Optional[float] = None,
        dew_spread_f: Optional[float] = None,
        wind_cardinal: Optional[str] = None,
        rain_30m_mm: float = 0.0,
    ) -> dict[str, Any]:
        """Resolve Barometric Pressure (`inHg` & `hPa`), 3-hour tendency, and 24-hour AcuRite-style forecast."""
        now_utc = datetime.now(timezone.utc)
        if rf_press_hpa is not None and not (MIN_PLAUSIBLE_PRESSURE_HPA <= rf_press_hpa <= MAX_PLAUSIBLE_PRESSURE_HPA):
            rf_press_hpa = None

        source = "rf_sensor" if rf_press_hpa is not None else "regional_reference"
        current_hpa = rf_press_hpa
        past_3h_hpa: Optional[float] = None
        past_6h_hpa: Optional[float] = None

        with self._lock:
            if self._conn is not None:
                if current_hpa is None:
                    row_curr = self._conn.execute(
                        """
                        SELECT pressure_hpa FROM reference_weather_history
                        WHERE pressure_hpa IS NOT NULL
                          AND pressure_hpa BETWEEN 870.0 AND 1085.0
                        ORDER BY timestamp DESC LIMIT 1;
                        """
                    ).fetchone()
                    if row_curr and row_curr["pressure_hpa"] is not None:
                        current_hpa = float(row_curr["pressure_hpa"])

                t_3h_start = format_utc_z(now_utc - timedelta(minutes=210))
                t_3h_end = format_utc_z(now_utc - timedelta(minutes=150))
                row_3h = self._conn.execute(
                    """
                    SELECT AVG(pressure_hpa) AS avg_p FROM reference_weather_history
                    WHERE timestamp BETWEEN ? AND ?
                      AND pressure_hpa IS NOT NULL
                      AND pressure_hpa BETWEEN 870.0 AND 1085.0;
                    """,
                    (t_3h_start, t_3h_end),
                ).fetchone()
                if row_3h and row_3h["avg_p"] is not None:
                    past_3h_hpa = float(row_3h["avg_p"])

                t_6h_start = format_utc_z(now_utc - timedelta(minutes=390))
                t_6h_end = format_utc_z(now_utc - timedelta(minutes=330))
                row_6h = self._conn.execute(
                    """
                    SELECT AVG(pressure_hpa) AS avg_p FROM reference_weather_history
                    WHERE timestamp BETWEEN ? AND ?
                      AND pressure_hpa IS NOT NULL
                      AND pressure_hpa BETWEEN 870.0 AND 1085.0;
                    """,
                    (t_6h_start, t_6h_end),
                ).fetchone()
                if row_6h and row_6h["avg_p"] is not None:
                    past_6h_hpa = float(row_6h["avg_p"])

        if current_hpa is None:
            return {
                "pressure_hpa": None,
                "pressure_inhg": None,
                "delta_3h_hpa": None,
                "delta_3h_inhg": None,
                "delta_6h_inhg": None,
                "trend_label": "Awaiting Sync",
                "trend_arrow": "→",
                "source": "none",
                "forecast_24h": "Syncing regional barometric pressure history...",
            }

        current_inhg = round(current_hpa * HPA_TO_INHG, 2)
        delta_3h_hpa = round(current_hpa - past_3h_hpa, 1) if past_3h_hpa is not None else None
        delta_3h_inhg = (
            round((current_hpa - past_3h_hpa) * HPA_TO_INHG, 2) if past_3h_hpa is not None else None
        )
        delta_6h_inhg = (
            round((current_hpa - past_6h_hpa) * HPA_TO_INHG, 2) if past_6h_hpa is not None else None
        )

        if delta_3h_inhg is None:
            trend_label = "Steady"
            trend_arrow = "→"
        elif delta_3h_inhg >= 0.06:
            trend_label = f"Rising Rapidly ({delta_3h_inhg:+.2f} inHg/3h)"
            trend_arrow = "↗"
        elif delta_3h_inhg >= 0.02:
            trend_label = f"Rising ({delta_3h_inhg:+.2f} inHg/3h)"
            trend_arrow = "↗"
        elif delta_3h_inhg <= -0.06:
            trend_label = f"Falling Rapidly ({delta_3h_inhg:+.2f} inHg/3h)"
            trend_arrow = "↘"
        elif delta_3h_inhg <= -0.02:
            trend_label = f"Falling ({delta_3h_inhg:+.2f} inHg/3h)"
            trend_arrow = "↘"
        else:
            trend_label = f"Steady ({delta_3h_inhg:+.2f} inHg/3h)"
            trend_arrow = "→"

        southerly_wind = wind_cardinal in ("S", "SSE", "SE", "ESE", "SSW", "SW")
        northerly_wind = wind_cardinal in ("N", "NNW", "NW", "WNW", "NNE", "NE")

        if rain_30m_mm > 0.0 and (delta_3h_inhg or 0.0) < 0.0:
            forecast_24h = "Active low-pressure precipitation continuing over next 6-12 hours."
        elif (delta_3h_inhg or 0.0) <= -0.06:
            forecast_24h = (
                "Rapidly falling barometer signals an approaching low-pressure trough - "
                "increasing clouds, wind, and precipitation likely within 12-24 hours."
            )
        elif (delta_3h_inhg or 0.0) <= -0.02:
            if current_inhg < 29.90 or southerly_wind:
                forecast_24h = (
                    "Falling pressure with warm/moist advection - unsettled weather and rain showers "
                    "likely within 12-24 hours."
                )
            else:
                forecast_24h = (
                    "Gradually falling pressure - fair conditions transitioning to increasing cloud cover "
                    "over the next 12-24 hours."
                )
        elif (delta_3h_inhg or 0.0) >= 0.05:
            forecast_24h = (
                "Briskly rising barometer behind a passing boundary - clearing skies and drier, "
                "cooler air mass moving in over the next 12-24 hours."
            )
        elif (delta_3h_inhg or 0.0) >= 0.02:
            if current_inhg >= 30.05 or northerly_wind:
                forecast_24h = "Rising high pressure (>30.05 inHg) - fair, dry, and settled weather for the next 24 hours."
            else:
                forecast_24h = "Improving barometric trend - partial clearing and stable weather expected."
        else:
            if current_inhg >= 30.15:
                forecast_24h = "Strong stationary high pressure - continued clear/fair conditions for the next 24 hours."
            elif current_inhg <= 29.80:
                forecast_24h = "Persistent low pressure - mostly cloudy and unsettled with scattered shower potential."
            elif dew_spread_f is not None and dew_spread_f <= 3.0:
                forecast_24h = "Steady barometer with near-saturated surface air - overnight mist/fog likely, followed by seasonal clouds."
            else:
                forecast_24h = "Steady barometric pressure - little change in current weather regime expected over the next 12-24 hours."

        return {
            "pressure_hpa": round(current_hpa, 1),
            "pressure_inhg": current_inhg,
            "delta_3h_hpa": delta_3h_hpa,
            "delta_3h_inhg": delta_3h_inhg,
            "delta_6h_inhg": delta_6h_inhg,
            "trend_label": trend_label,
            "trend_arrow": trend_arrow,
            "source": source,
            "forecast_24h": forecast_24h,
        }

    def sync_daily_summaries(self, days_back: int = 90) -> int:
        """Roll up historical trusted outdoor observations into `daily_summaries` for climatology & seasonal records."""
        now_utc = datetime.now(timezone.utc)
        cutoff_iso = format_utc_z(now_utc - timedelta(days=max(1, int(days_back))))
        now_iso = format_utc_z(now_utc)

        with self._lock:
            if self._conn is None:
                return 0
            rows = self._conn.execute(
                """
                SELECT
                    SUBSTR(sr.timestamp, 1, 10) AS date_str,
                    MIN(sr.temp_c) AS min_tc,
                    MAX(sr.temp_c) AS max_tc,
                    AVG(sr.temp_c) AS avg_tc,
                    MIN(sr.humidity) AS min_rh,
                    MAX(sr.humidity) AS max_rh,
                    AVG(sr.humidity) AS avg_rh,
                    MAX(COALESCE(sr.wind_gust_mps, sr.wind_avg_mps, 0.0)) AS peak_wind_mps,
                    MIN(CASE WHEN sr.pressure_hpa BETWEEN 870.0 AND 1085.0 THEN sr.pressure_hpa END) AS min_p,
                    MAX(CASE WHEN sr.pressure_hpa BETWEEN 870.0 AND 1085.0 THEN sr.pressure_hpa END) AS max_p,
                    COUNT(*) AS cnt
                FROM sensor_readings sr
                JOIN sensors s ON s.sensor_key = COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id)
                WHERE sr.timestamp >= ?
                  AND s.is_trusted = 1
                  AND s.location_type = 'outdoor'
                  AND (sr.temp_c IS NULL OR (sr.temp_c BETWEEN -31.67 AND 54.44))
                GROUP BY SUBSTR(sr.timestamp, 1, 10)
                ORDER BY date_str ASC;
                """,
                (cutoff_iso,),
            ).fetchall()

            # Compute daily rain delta per sensor and take max outdoor rain gauge per day
            rain_rows = self._conn.execute(
                """
                SELECT
                    SUBSTR(sr.timestamp, 1, 10) AS date_str,
                    COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id) AS skey,
                    MAX(sr.rain_cumulative_mm) - MIN(sr.rain_cumulative_mm) AS rain_mm
                FROM sensor_readings sr
                JOIN sensors s ON s.sensor_key = COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id)
                WHERE sr.timestamp >= ?
                  AND s.is_trusted = 1
                  AND s.location_type = 'outdoor'
                  AND sr.rain_cumulative_mm IS NOT NULL
                GROUP BY SUBSTR(sr.timestamp, 1, 10), skey;
                """,
                (cutoff_iso,),
            ).fetchall()

            # Also pull reference pressure range per day when RF barometer is absent
            ref_press_rows = self._conn.execute(
                """
                SELECT
                    SUBSTR(timestamp, 1, 10) AS date_str,
                    MIN(pressure_hpa) AS min_p,
                    MAX(pressure_hpa) AS max_p
                FROM reference_weather_history
                WHERE timestamp >= ?
                  AND pressure_hpa BETWEEN 870.0 AND 1085.0
                GROUP BY SUBSTR(timestamp, 1, 10);
                """,
                (cutoff_iso,),
            ).fetchall()

            rain_by_day: dict[str, float] = {}
            for rr in rain_rows:
                d_str = str(rr["date_str"])
                delta_mm = max(0.0, float(rr["rain_mm"] or 0.0))
                if delta_mm > rain_by_day.get(d_str, 0.0):
                    rain_by_day[d_str] = delta_mm

            ref_p_by_day: dict[str, tuple[float, float]] = {}
            for rp in ref_press_rows:
                if rp["min_p"] is not None and rp["max_p"] is not None:
                    ref_p_by_day[str(rp["date_str"])] = (float(rp["min_p"]), float(rp["max_p"]))

            # 1. First seed / backfill daily summaries from reference_weather_history (30-day baseline)
            ref_daily_rows = self._conn.execute(
                """
                SELECT
                    SUBSTR(timestamp, 1, 10) AS date_str,
                    MIN(temp_c) AS min_tc,
                    MAX(temp_c) AS max_tc,
                    AVG(temp_c) AS avg_tc,
                    MIN(humidity) AS min_rh,
                    MAX(humidity) AS max_rh,
                    AVG(humidity) AS avg_rh,
                    MAX(COALESCE(wind_gust_mps, wind_avg_mps, 0.0)) AS peak_wind_mps,
                    MIN(CASE WHEN pressure_hpa BETWEEN 870.0 AND 1085.0 THEN pressure_hpa END) AS min_p,
                    MAX(CASE WHEN pressure_hpa BETWEEN 870.0 AND 1085.0 THEN pressure_hpa END) AS max_p,
                    SUM(COALESCE(rain_mm, 0.0)) AS rain_mm,
                    COUNT(*) AS cnt
                FROM reference_weather_history
                WHERE timestamp >= ? AND temp_c IS NOT NULL
                GROUP BY SUBSTR(timestamp, 1, 10)
                ORDER BY date_str ASC;
                """,
                (cutoff_iso,),
            ).fetchall()

            upsert_dict: dict[str, tuple[Any, ...]] = {}
            for r in ref_daily_rows:
                d_str = str(r["date_str"])
                min_tc = float(r["min_tc"]) if r["min_tc"] is not None else None
                max_tc = float(r["max_tc"]) if r["max_tc"] is not None else None
                avg_tc = float(r["avg_tc"]) if r["avg_tc"] is not None else None

                min_tf = round((min_tc * 9.0 / 5.0) + 32.0, 1) if min_tc is not None else None
                max_tf = round((max_tc * 9.0 / 5.0) + 32.0, 1) if max_tc is not None else None
                avg_tf = round((avg_tc * 9.0 / 5.0) + 32.0, 1) if avg_tc is not None else None

                hdd = round(max(0.0, 65.0 - avg_tf), 1) if avg_tf is not None else 0.0
                cdd = round(max(0.0, avg_tf - 65.0), 1) if avg_tf is not None else 0.0

                r_mm = round(float(r["rain_mm"] or 0.0), 2)
                r_in = round(r_mm / 25.4, 3)

                pk_mps = round(float(r["peak_wind_mps"] or 0.0), 2)
                pk_mph = round(pk_mps * 2.23693629, 1)

                min_p = float(r["min_p"]) if r["min_p"] is not None else None
                max_p = float(r["max_p"]) if r["max_p"] is not None else None
                p_range_inhg = (
                    round((max_p - min_p) * HPA_TO_INHG, 2)
                    if (min_p is not None and max_p is not None)
                    else 0.12
                )

                upsert_dict[d_str] = (
                    d_str,
                    round(min_tc, 2) if min_tc is not None else None,
                    round(max_tc, 2) if max_tc is not None else None,
                    round(avg_tc, 2) if avg_tc is not None else None,
                    min_tf,
                    max_tf,
                    avg_tf,
                    round(float(r["min_rh"]), 1) if r["min_rh"] is not None else None,
                    round(float(r["max_rh"]), 1) if r["max_rh"] is not None else None,
                    round(float(r["avg_rh"]), 1) if r["avg_rh"] is not None else None,
                    hdd,
                    cdd,
                    r_mm,
                    r_in,
                    pk_mps,
                    pk_mph,
                    round(min_p, 1) if min_p is not None else None,
                    round(max_p, 1) if max_p is not None else None,
                    p_range_inhg,
                    int(r["cnt"] or 0),
                    now_iso,
                )

            # 2. Local trusted outdoor station rollups update / override baseline
            for r in rows:
                d_str = str(r["date_str"])
                min_tc = float(r["min_tc"]) if r["min_tc"] is not None else None
                max_tc = float(r["max_tc"]) if r["max_tc"] is not None else None
                avg_tc = float(r["avg_tc"]) if r["avg_tc"] is not None else None

                min_tf = round((min_tc * 9.0 / 5.0) + 32.0, 1) if min_tc is not None else None
                max_tf = round((max_tc * 9.0 / 5.0) + 32.0, 1) if max_tc is not None else None
                avg_tf = round((avg_tc * 9.0 / 5.0) + 32.0, 1) if avg_tc is not None else None

                hdd = round(max(0.0, 65.0 - avg_tf), 1) if avg_tf is not None else 0.0
                cdd = round(max(0.0, avg_tf - 65.0), 1) if avg_tf is not None else 0.0

                r_mm = round(rain_by_day.get(d_str, 0.0), 2)
                r_in = round(r_mm / 25.4, 3)

                pk_mps = round(float(r["peak_wind_mps"] or 0.0), 2)
                pk_mph = round(pk_mps * 2.23693629, 1)

                min_p = float(r["min_p"]) if r["min_p"] is not None else None
                max_p = float(r["max_p"]) if r["max_p"] is not None else None
                if (min_p is None or max_p is None) and d_str in ref_p_by_day:
                    min_p, max_p = ref_p_by_day[d_str]

                p_range_inhg = (
                    round((max_p - min_p) * HPA_TO_INHG, 2)
                    if (min_p is not None and max_p is not None)
                    else 0.12
                )

                upsert_dict[d_str] = (
                    d_str,
                    round(min_tc, 2) if min_tc is not None else None,
                    round(max_tc, 2) if max_tc is not None else None,
                    round(avg_tc, 2) if avg_tc is not None else None,
                    min_tf,
                    max_tf,
                    avg_tf,
                    round(float(r["min_rh"]), 1) if r["min_rh"] is not None else None,
                    round(float(r["max_rh"]), 1) if r["max_rh"] is not None else None,
                    round(float(r["avg_rh"]), 1) if r["avg_rh"] is not None else None,
                    hdd,
                    cdd,
                    r_mm,
                    r_in,
                    pk_mps,
                    pk_mph,
                    round(min_p, 1) if min_p is not None else None,
                    round(max_p, 1) if max_p is not None else None,
                    p_range_inhg,
                    int(r["cnt"] or 0),
                    now_iso,
                )

            upsert_params = list(upsert_dict.values())

            if upsert_params:
                with self._conn:
                    self._conn.executemany(
                        """
                        INSERT INTO daily_summaries (
                            date_str, temp_min_c, temp_max_c, temp_mean_c,
                            temp_min_f, temp_max_f, temp_mean_f,
                            humidity_min, humidity_max, humidity_mean,
                            hdd, cdd, rain_total_mm, rain_total_in,
                            wind_peak_mps, wind_peak_mph,
                            pressure_min_hpa, pressure_max_hpa, pressure_range_inhg,
                            reading_count, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date_str) DO UPDATE SET
                            temp_min_c = excluded.temp_min_c,
                            temp_max_c = excluded.temp_max_c,
                            temp_mean_c = excluded.temp_mean_c,
                            temp_min_f = excluded.temp_min_f,
                            temp_max_f = excluded.temp_max_f,
                            temp_mean_f = excluded.temp_mean_f,
                            humidity_min = excluded.humidity_min,
                            humidity_max = excluded.humidity_max,
                            humidity_mean = excluded.humidity_mean,
                            hdd = excluded.hdd,
                            cdd = excluded.cdd,
                            rain_total_mm = excluded.rain_total_mm,
                            rain_total_in = excluded.rain_total_in,
                            wind_peak_mps = excluded.wind_peak_mps,
                            wind_peak_mph = excluded.wind_peak_mph,
                            pressure_min_hpa = excluded.pressure_min_hpa,
                            pressure_max_hpa = excluded.pressure_max_hpa,
                            pressure_range_inhg = excluded.pressure_range_inhg,
                            reading_count = excluded.reading_count,
                            updated_at = excluded.updated_at;
                        """,
                        upsert_params,
                    )
            return len(upsert_params)

    def get_daily_summaries(self, limit: int = 365) -> list[dict[str, Any]]:
        """Return rolled-up daily summary rows ordered by date DESC (bounded max 2000)."""
        safe_limit = max(1, min(int(limit), 2000))
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT * FROM daily_summaries ORDER BY date_str DESC LIMIT ?;",
                (safe_limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def _query_period_records(self, cutoff_iso: Optional[str]) -> dict[str, Any]:
        """Compute High/Low extremes and rain total for a UTC window across trusted outdoor stations + reference backfill."""
        if self._conn is None:
            return {}
        where_time = "AND sr.timestamp >= ?" if cutoff_iso else ""
        where_ref = "WHERE timestamp >= ?" if cutoff_iso else ""
        params: tuple[Any, ...] = (cutoff_iso,) if cutoff_iso else ()

        row = self._conn.execute(
            f"""
            SELECT
                MIN(sr.temp_c) AS min_tc,
                MAX(sr.temp_c) AS max_tc,
                AVG(sr.temp_c) AS avg_tc,
                MIN(sr.humidity) AS min_rh,
                MAX(sr.humidity) AS max_rh,
                MAX(COALESCE(sr.wind_gust_mps, sr.wind_avg_mps)) AS max_wind_mps,
                MIN(CASE WHEN sr.pressure_hpa BETWEEN 870.0 AND 1085.0 THEN sr.pressure_hpa END) AS min_p,
                MAX(CASE WHEN sr.pressure_hpa BETWEEN 870.0 AND 1085.0 THEN sr.pressure_hpa END) AS max_p,
                COUNT(*) AS cnt
            FROM sensor_readings sr
            JOIN sensors s ON s.sensor_key = COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id)
            WHERE s.is_trusted = 1
              AND s.location_type = 'outdoor'
              AND (sr.temp_c IS NULL OR (sr.temp_c BETWEEN -31.67 AND 54.44))
              {where_time};
            """,
            params,
        ).fetchone()

        ref_row = self._conn.execute(
            f"""
            SELECT
                MIN(temp_c) AS min_tc,
                MAX(temp_c) AS max_tc,
                AVG(temp_c) AS avg_tc,
                MIN(humidity) AS min_rh,
                MAX(humidity) AS max_rh,
                MAX(COALESCE(wind_gust_mps, wind_avg_mps)) AS max_wind_mps,
                MIN(CASE WHEN pressure_hpa BETWEEN 870.0 AND 1085.0 THEN pressure_hpa END) AS min_p,
                MAX(CASE WHEN pressure_hpa BETWEEN 870.0 AND 1085.0 THEN pressure_hpa END) AS max_p,
                SUM(COALESCE(rain_mm, 0.0)) AS total_rain_mm
            FROM reference_weather_history
            {where_ref};
            """,
            params,
        ).fetchone()

        # Local rain accumulation in period
        rain_rows = self._conn.execute(
            f"""
            SELECT
                COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id) AS skey,
                MAX(sr.rain_cumulative_mm) - MIN(sr.rain_cumulative_mm) AS delta_mm
            FROM sensor_readings sr
            JOIN sensors s ON s.sensor_key = COALESCE(sr.sensor_key, sr.model || '_' || sr.sensor_id)
            WHERE s.is_trusted = 1
              AND s.location_type = 'outdoor'
              AND sr.rain_cumulative_mm IS NOT NULL
              {where_time}
            GROUP BY skey;
            """,
            params,
        ).fetchall()
        local_rain_mm = max([max(0.0, float(r["delta_mm"] or 0.0)) for r in rain_rows], default=0.0)

        loc_min_tc = float(row["min_tc"]) if (row and row["min_tc"] is not None) else None
        loc_max_tc = float(row["max_tc"]) if (row and row["max_tc"] is not None) else None
        loc_avg_tc = float(row["avg_tc"]) if (row and row["avg_tc"] is not None) else None
        loc_min_rh = float(row["min_rh"]) if (row and row["min_rh"] is not None) else None
        loc_max_rh = float(row["max_rh"]) if (row and row["max_rh"] is not None) else None
        loc_wind = float(row["max_wind_mps"]) if (row and row["max_wind_mps"] is not None) else None
        loc_min_p = float(row["min_p"]) if (row and row["min_p"] is not None) else None
        loc_max_p = float(row["max_p"]) if (row and row["max_p"] is not None) else None

        ref_min_tc = float(ref_row["min_tc"]) if (ref_row and ref_row["min_tc"] is not None) else None
        ref_max_tc = float(ref_row["max_tc"]) if (ref_row and ref_row["max_tc"] is not None) else None
        ref_avg_tc = float(ref_row["avg_tc"]) if (ref_row and ref_row["avg_tc"] is not None) else None
        ref_min_rh = float(ref_row["min_rh"]) if (ref_row and ref_row["min_rh"] is not None) else None
        ref_max_rh = float(ref_row["max_rh"]) if (ref_row and ref_row["max_rh"] is not None) else None
        ref_wind = float(ref_row["max_wind_mps"]) if (ref_row and ref_row["max_wind_mps"] is not None) else None
        ref_min_p = float(ref_row["min_p"]) if (ref_row and ref_row["min_p"] is not None) else None
        ref_max_p = float(ref_row["max_p"]) if (ref_row and ref_row["max_p"] is not None) else None
        ref_rain_mm = float(ref_row["total_rain_mm"] or 0.0) if ref_row else 0.0

        def _safe_min(a: Optional[float], b: Optional[float]) -> Optional[float]:
            if a is not None and b is not None:
                return min(a, b)
            return a if a is not None else b

        def _safe_max(a: Optional[float], b: Optional[float]) -> Optional[float]:
            if a is not None and b is not None:
                return max(a, b)
            return a if a is not None else b

        min_tc = _safe_min(loc_min_tc, ref_min_tc)
        max_tc = _safe_max(loc_max_tc, ref_max_tc)
        avg_tc = loc_avg_tc if loc_avg_tc is not None else ref_avg_tc
        if loc_avg_tc is not None and ref_avg_tc is not None:
            cnt = int(row["cnt"] or 0) if row else 0
            if cnt < 20:
                avg_tc = round((loc_avg_tc + ref_avg_tc) / 2.0, 2)
            else:
                avg_tc = loc_avg_tc

        min_rh = _safe_min(loc_min_rh, ref_min_rh)
        max_rh = _safe_max(loc_max_rh, ref_max_rh)
        max_wind_mps = _safe_max(loc_wind, ref_wind)
        min_p = _safe_min(loc_min_p, ref_min_p)
        max_p = _safe_max(loc_max_p, ref_max_p)
        rain_mm = max(local_rain_mm, ref_rain_mm)

        return {
            "temp_low_f": round((min_tc * 9.0 / 5.0) + 32.0, 1) if min_tc is not None else None,
            "temp_high_f": round((max_tc * 9.0 / 5.0) + 32.0, 1) if max_tc is not None else None,
            "temp_avg_f": round((avg_tc * 9.0 / 5.0) + 32.0, 1) if avg_tc is not None else None,
            "humidity_low": round(min_rh, 0) if min_rh is not None else None,
            "humidity_high": round(max_rh, 0) if max_rh is not None else None,
            "wind_peak_mph": round(max_wind_mps * 2.23693629, 1) if max_wind_mps is not None else None,
            "wind_gust_max_mph": round(max_wind_mps * 2.23693629, 1) if max_wind_mps is not None else None,
            "pressure_low_inhg": round(min_p * HPA_TO_INHG, 2) if min_p is not None else None,
            "pressure_high_inhg": round(max_p * HPA_TO_INHG, 2) if max_p is not None else None,
            "rain_in": round(rain_mm / 25.4, 2),
            "rain_total_in": round(rain_mm / 25.4, 2),
            "rain_mm": round(rain_mm, 1),
        }

    def get_records_summary_table(self) -> dict[str, dict[str, Any]]:
        """Return Weather-Display style High/Low records for Today, This Week, This Month, This Year, and All-Time."""
        now_local = datetime.now().astimezone()
        midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_iso = format_utc_z(midnight_local)
        week_iso = format_utc_z(now_local - timedelta(days=7))
        month_start_local = midnight_local.replace(day=1)
        month_iso = format_utc_z(month_start_local)
        year_start_local = midnight_local.replace(month=1, day=1)
        year_iso = format_utc_z(year_start_local)

        with self._lock:
            return {
                "today": self._query_period_records(today_iso),
                "week": self._query_period_records(week_iso),
                "month": self._query_period_records(month_iso),
                "year": self._query_period_records(year_iso),
                "all_time": self._query_period_records(None),
            }

    def get_lightning_telemetry(self) -> dict[str, Any]:
        """Return AcuRite 06045M Lightning Detector telemetry, strike counts, distance rings, and RFI status."""
        now_utc = datetime.now(timezone.utc)
        cutoff_15m = format_utc_z(now_utc - timedelta(minutes=15))
        cutoff_30m = format_utc_z(now_utc - timedelta(minutes=30))
        cutoff_1h = format_utc_z(now_utc - timedelta(hours=1))
        cutoff_24h = format_utc_z(now_utc - timedelta(hours=24))
        online_cutoff = now_utc - timedelta(minutes=60)

        sensors = self.get_all_sensors()
        lightning_sensor: Optional[dict[str, Any]] = None
        for s in sensors:
            model_lower = str(s.get("model") or "").lower()
            lr = s.get("latest_reading") or {}
            has_lightning_fields = (
                "6045" in model_lower
                or "lightning" in model_lower
                or lr.get("strike_count") is not None
                or lr.get("storm_dist_mi") is not None
            )
            if not has_lightning_fields:
                continue
            hidden = set(s.get("hidden_metrics") or [])
            if "lightning" in hidden:
                continue
            if lightning_sensor is None or (s.get("is_trusted") and not lightning_sensor.get("is_trusted")):
                lightning_sensor = s

        sensor_online = False
        sensor_key: Optional[str] = None
        sensor_name = "AcuRite 06045M Lightning Detector"
        rfi_warning = False
        latest_strike_counter: Optional[int] = None
        reporting_cadence_sec = 24

        if lightning_sensor is not None:
            sensor_key = str(lightning_sensor["sensor_key"])
            sensor_name = str(lightning_sensor.get("display_name") or sensor_key)
            last_dt = parse_utc_iso(lightning_sensor.get("last_seen"))
            if last_dt is not None and last_dt >= online_cutoff:
                sensor_online = True
            lr = lightning_sensor.get("latest_reading") or {}
            if lr.get("strike_count") is not None:
                latest_strike_counter = int(lr["strike_count"])

        strikes_15m = 0
        strikes_30m = 0
        strikes_1h = 0
        strikes_24h = 0
        last_strike_time: Optional[str] = None
        last_strike_dist_mi: Optional[float] = None
        closest_dist_24h_mi: Optional[float] = None
        furthest_dist_24h_mi: Optional[float] = None
        recent_rings: list[dict[str, Any]] = []
        active_storm_mode = False
        unconfirmed_count_24h = 0
        last_unconfirmed_time: Optional[str] = None

        with self._lock:
            if self._conn is not None:
                # 1. Confirmed strike counts across time windows
                r_15m = self._conn.execute(
                    "SELECT COALESCE(SUM(delta_strikes), 0) AS c FROM lightning_strikes WHERE timestamp >= ? AND confirmed = 1;",
                    (cutoff_15m,),
                ).fetchone()
                strikes_15m = int(r_15m["c"] or 0) if r_15m else 0

                r_30m = self._conn.execute(
                    "SELECT COALESCE(SUM(delta_strikes), 0) AS c FROM lightning_strikes WHERE timestamp >= ? AND confirmed = 1;",
                    (cutoff_30m,),
                ).fetchone()
                strikes_30m = int(r_30m["c"] or 0) if r_30m else 0

                r_1h = self._conn.execute(
                    "SELECT COALESCE(SUM(delta_strikes), 0) AS c FROM lightning_strikes WHERE timestamp >= ? AND confirmed = 1;",
                    (cutoff_1h,),
                ).fetchone()
                strikes_1h = int(r_1h["c"] or 0) if r_1h else 0

                r_24h = self._conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(delta_strikes), 0) AS c,
                        MIN(distance_mi) AS min_d,
                        MAX(distance_mi) AS max_d
                    FROM lightning_strikes
                    WHERE timestamp >= ? AND distance_mi >= 1.0 AND distance_mi <= 25.0
                      AND confirmed = 1;
                    """,
                    (cutoff_24h,),
                ).fetchone()
                if r_24h and int(r_24h["c"] or 0) > 0:
                    strikes_24h = int(r_24h["c"])
                    if r_24h["min_d"] is not None:
                        closest_dist_24h_mi = round(float(r_24h["min_d"]), 1)
                    if r_24h["max_d"] is not None:
                        furthest_dist_24h_mi = round(float(r_24h["max_d"]), 1)
                else:
                    strikes_24h = 0
                    closest_dist_24h_mi = None
                    furthest_dist_24h_mi = None

                # Unconfirmed static pulses within 24h (filtered noise gate)
                r_unconfirmed = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(delta_strikes), 0) AS c, MAX(timestamp) AS last_ts
                    FROM lightning_strikes
                    WHERE timestamp >= ? AND confirmed = 0;
                    """,
                    (cutoff_24h,),
                ).fetchone()
                if r_unconfirmed:
                    unconfirmed_count_24h = int(r_unconfirmed["c"] or 0)
                    if r_unconfirmed["last_ts"]:
                        last_unconfirmed_time = str(r_unconfirmed["last_ts"])

                # 2. Last real confirmed strike (only valid strikes 1..25 mi)
                r_last = self._conn.execute(
                    """
                    SELECT timestamp, distance_mi, rfi
                    FROM lightning_strikes
                    WHERE distance_mi >= 1.0 AND distance_mi <= 25.0
                      AND confirmed = 1
                    ORDER BY timestamp DESC, id DESC LIMIT 1;
                    """
                ).fetchone()
                if r_last:
                    last_strike_time = str(r_last["timestamp"])
                    if r_last["distance_mi"] is not None:
                        d = float(r_last["distance_mi"])
                        if 1.0 <= d <= 25.0:
                            last_strike_dist_mi = round(d, 1)

                # 3. Storm Active (8s Updates) Mode:
                # Set active_storm_mode = True ONLY when at least 1 real lightning strike
                # (delta_strikes > 0) has been recorded within the last 30 minutes
                # (or when ussb / active bit is explicitly 1 AND strikes_1h > 0).
                # Otherwise, always display NORMAL (24S UPDATES) (24s normal cadence).
                flag_active = False
                if lightning_sensor:
                    lr = lightning_sensor.get("latest_reading") or {}
                    if lr.get("active_storm_mode") is not None:
                        flag_active = bool(lr.get("active_storm_mode"))

                if strikes_30m > 0 or (flag_active and strikes_1h > 0):
                    active_storm_mode = True
                    reporting_cadence_sec = 8
                else:
                    active_storm_mode = False
                    reporting_cadence_sec = 24

                # 4. Debounced RFI Interference:
                # Only trigger the RFI INTERFERENCE badge if rfi == 1 is reported continuously
                # across at least 3 consecutive distinct transmission intervals (> 60 seconds)
                # while delta_strikes == 0 (strikes_1h == 0) — ignoring single-packet RF bit flips or startup pulses!
                if sensor_key and strikes_1h == 0:
                    recent_rfi_rows = self._conn.execute(
                        """
                        SELECT timestamp, rfi_detected FROM sensor_readings
                        WHERE COALESCE(sensor_key, model || '_' || sensor_id) = ?
                        ORDER BY timestamp DESC, id DESC LIMIT 15;
                        """,
                        (sensor_key,),
                    ).fetchall()

                    distinct_intervals = []
                    last_event_dt = None
                    for row in recent_rfi_rows:
                        ts_dt = parse_utc_iso(str(row["timestamp"]))
                        if ts_dt is None:
                            continue
                        if last_event_dt is None or abs((last_event_dt - ts_dt).total_seconds()) >= 15.0:
                            distinct_intervals.append(row)
                            last_event_dt = ts_dt
                        if len(distinct_intervals) >= 5:
                            break

                    if len(distinct_intervals) >= 3:
                        dt0 = parse_utc_iso(str(distinct_intervals[0]["timestamp"]))
                        dt2 = parse_utc_iso(str(distinct_intervals[2]["timestamp"]))
                        time_span = abs((dt0 - dt2).total_seconds()) if dt0 and dt2 else 0.0
                        all_3_rfi = all(int(r["rfi_detected"] or 0) == 1 for r in distinct_intervals[:3])
                        if all_3_rfi and time_span >= 55.0:
                            rfi_warning = True

                ring_rows = self._conn.execute(
                    """
                    SELECT timestamp, distance_mi, delta_strikes, rfi
                    FROM lightning_strikes
                    WHERE timestamp >= ? AND confirmed = 1
                    ORDER BY timestamp DESC, id DESC LIMIT 25;
                    """,
                    (cutoff_24h,),
                ).fetchall()
                for rr in ring_rows:
                    ts_str = str(rr["timestamp"])
                    dt_s = parse_utc_iso(ts_str)
                    age_min = round(max(0.0, (now_utc - dt_s).total_seconds() / 60.0), 1) if dt_s else 0.0
                    dist_val = round(float(rr["distance_mi"]), 1)
                    d_strikes = int(rr["delta_strikes"] or 1)
                    if age_min <= 15.0:
                        intensity = "high"
                    elif age_min <= 60.0:
                        intensity = "medium"
                    else:
                        intensity = "low"
                    recent_rings.append(
                        {
                            "distance_mi": dist_val,
                            "timestamp": ts_str,
                            "age_minutes": age_min,
                            "intensity": intensity,
                            "delta_strikes": d_strikes,
                            "rfi": bool(int(rr["rfi"] or 0) != 0),
                        }
                    )

        return {
            "sensor_online": sensor_online,
            "sensor_key": sensor_key,
            "sensor_name": sensor_name,
            "reporting_cadence_sec": reporting_cadence_sec,
            "reporting_mode_label": "8s Active Storm Mode" if active_storm_mode else "24s Normal Mode",
            "active_storm_mode": active_storm_mode,
            "rfi_warning": rfi_warning,
            "strike_counter_raw": latest_strike_counter,
            "strikes_15m": strikes_15m,
            "strikes_1h": strikes_1h,
            "strikes_24h": strikes_24h,
            "last_strike_time": last_strike_time,
            "last_strike_dist_mi": last_strike_dist_mi,
            "closest_dist_24h_mi": closest_dist_24h_mi,
            "furthest_dist_24h_mi": furthest_dist_24h_mi,
            "recent_rings": recent_rings,
            "unconfirmed_count_24h": unconfirmed_count_24h,
            "last_unconfirmed_time": last_unconfirmed_time,
        }

    def clear_lightning_strikes(self, hours: Optional[int] = None) -> dict[str, Any]:
        """Wipe false, unconfirmed, or simulated test strikes from SQLite and synchronize sensor baseline."""
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            with self._conn:
                if hours is not None and int(hours) > 0 and int(hours) < 24:
                    cutoff_iso = format_utc_z(now_utc - timedelta(hours=int(hours)))
                    cur = self._conn.execute(
                        "DELETE FROM lightning_strikes WHERE timestamp >= ?;",
                        (cutoff_iso,),
                    )
                    deleted = cur.rowcount
                else:
                    cur = self._conn.execute("DELETE FROM lightning_strikes;")
                    deleted = cur.rowcount

                # Synchronize sensor baseline: clear lingering distance/RFI on all lightning readings
                self._conn.execute(
                    """
                    UPDATE sensor_readings
                    SET storm_dist_mi = NULL, rfi_detected = 0
                    WHERE (model LIKE '%6045%' OR model LIKE '%lightning%');
                    """
                )

                logger.info(
                    "[LIGHTNING] Purged %d strike records and synchronized sensor baseline.",
                    deleted,
                )
                return {
                    "ok": True,
                    "deleted_strikes": deleted,
                    "hours": hours,
                    "cleared_at": format_utc_z(now_utc),
                }

    # ==========================================================================
    # OFFICIAL NWS WEATHER ALERTS & WARNINGS ARCHIVE
    # ==========================================================================
    def sync_weather_alerts(self, active_alert_dicts: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert active NWS alerts into `weather_alerts`, deactivate superseded alerts, and preserve historical archive."""
        now_utc = datetime.now(timezone.utc)
        now_iso = format_utc_z(now_utc)
        with self._lock:
            if self._conn is None:
                return {"active_count": 0, "deactivated_count": 0}
            with self._conn:
                active_ids: set[str] = set()
                for a in active_alert_dicts:
                    alert_id = str(a.get("alert_id") or a.get("id") or "").strip()
                    if not alert_id:
                        continue
                    active_ids.add(alert_id)
                    event = str(a.get("event") or "Weather Alert")
                    severity = str(a.get("severity") or "Moderate")
                    urgency = str(a.get("urgency") or "")
                    certainty = str(a.get("certainty") or "")
                    headline = str(a.get("headline") or "")
                    description = str(a.get("description") or "")
                    instruction = str(a.get("instruction") or "")
                    area_desc = str(a.get("area_desc") or a.get("areaDesc") or "")
                    sender_name = str(a.get("sender_name") or a.get("senderName") or "")
                    onset = str(a.get("onset") or a.get("effective") or now_iso)
                    expires = str(a.get("expires") or a.get("ends") or "")

                    self._conn.execute(
                        """
                        INSERT INTO weather_alerts (
                            alert_id, event, severity, urgency, certainty,
                            headline, description, instruction, area_desc, sender_name,
                            onset, expires, first_seen, last_seen, is_active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(alert_id) DO UPDATE SET
                            event = excluded.event,
                            severity = excluded.severity,
                            urgency = excluded.urgency,
                            certainty = excluded.certainty,
                            headline = excluded.headline,
                            description = excluded.description,
                            instruction = excluded.instruction,
                            area_desc = excluded.area_desc,
                            sender_name = excluded.sender_name,
                            onset = excluded.onset,
                            expires = excluded.expires,
                            last_seen = excluded.last_seen,
                            is_active = 1;
                        """,
                        (
                            alert_id, event, severity, urgency, certainty,
                            headline, description, instruction, area_desc, sender_name,
                            onset, expires, now_iso, now_iso,
                        ),
                    )

                # Mark alerts no longer present in active_ids OR where expires < now as is_active = 0
                # Preserve test alerts unless they expired or were cleared
                cur = self._conn.execute(
                    """
                    SELECT id, alert_id, expires FROM weather_alerts
                    WHERE is_active = 1;
                    """
                ).fetchall()
                deactivated = 0
                for row in cur:
                    aid = str(row["alert_id"])
                    exp_str = str(row["expires"] or "")
                    exp_dt = parse_utc_iso(exp_str) if exp_str else None
                    is_expired = (exp_dt is not None and exp_dt < now_utc)
                    is_missing = (aid not in active_ids and not aid.startswith("test-alert"))
                    if is_missing or is_expired:
                        self._conn.execute(
                            "UPDATE weather_alerts SET is_active = 0 WHERE id = ?;",
                            (row["id"],),
                        )
                        deactivated += 1

                return {
                    "active_count": len(active_ids),
                    "deactivated_count": deactivated,
                }

    def get_active_weather_alerts(self) -> list[dict[str, Any]]:
        """Return all currently active NWS alerts ordered by severity and onset."""
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                """
                SELECT * FROM weather_alerts
                WHERE is_active = 1
                ORDER BY
                    CASE severity
                        WHEN 'Extreme' THEN 1
                        WHEN 'Severe' THEN 2
                        WHEN 'Moderate' THEN 3
                        ELSE 4
                    END,
                    onset DESC;
                """
            ).fetchall()
            active: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                # Auto-check expiration
                exp_dt = parse_utc_iso(str(d.get("expires") or ""))
                if exp_dt is not None and exp_dt < now_utc:
                    continue
                active.append(d)
            return active

    def get_weather_alerts_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return past logged weather alerts archived in SQLite."""
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                """
                SELECT * FROM weather_alerts
                ORDER BY onset DESC, id DESC
                LIMIT ?;
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def inject_test_alert(self, sample: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Inject a simulated NWS alert into SQLite to test dashboard banners, voice reader, and calendar."""
        now_utc = datetime.now(timezone.utc)
        now_iso = format_utc_z(now_utc)
        exp_iso = format_utc_z(now_utc + timedelta(hours=4))
        alert_id = f"test-alert-{int(now_utc.timestamp())}"
        sample = sample or {}
        event = sample.get("event", "Severe Thunderstorm Watch")
        severity = sample.get("severity", "Severe")
        headline = sample.get(
            "headline",
            f"{event} in effect until {format_utc_z(now_utc + timedelta(hours=4))[11:16]} UTC for regional area",
        )
        desc = sample.get(
            "description",
            "THE NATIONAL WEATHER SERVICE HAS ISSUED A SEVERE THUNDERSTORM WATCH IN EFFECT UNTIL 4 HOURS FROM NOW. "
            "HAIL TO 1.5 INCHES IN DIAMETER, WIND GUSTS TO 65 MPH, AND FREQUENT DANGEROUS LIGHTNING ARE POSSIBLE.",
        )
        instruction = sample.get(
            "instruction",
            "PRECAUTIONARY/PREPAREDNESS ACTIONS... REMAIN ALERT FOR APPROACHING STORMS. "
            "MOVE TO AN INTERIOR ROOM ON THE LOWEST FLOOR OF A STURDY BUILDING IF A WARNING IS ISSUED.",
        )
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database is closed.")
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO weather_alerts (
                        alert_id, event, severity, urgency, certainty,
                        headline, description, instruction, area_desc, sender_name,
                        onset, expires, first_seen, last_seen, is_active
                    ) VALUES (?, ?, ?, 'Immediate', 'Observed', ?, ?, ?, 'SkyScry Telemetry Grid', 'NWS Test Office', ?, ?, ?, ?, 1);
                    """,
                    (alert_id, event, severity, headline, desc, instruction, now_iso, exp_iso, now_iso, now_iso),
                )
        return {
            "ok": True,
            "alert_id": alert_id,
            "event": event,
            "severity": severity,
            "headline": headline,
            "description": desc,
            "instruction": instruction,
            "onset": now_iso,
            "expires": exp_iso,
            "is_active": 1,
        }

    def clear_test_alerts(self) -> int:
        """Clear all simulated test alerts from SQLite."""
        with self._lock:
            if self._conn is None:
                return 0
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM weather_alerts WHERE alert_id LIKE 'test-alert%';"
                )
                return cur.rowcount

    def upsert_learned_weather_model(self, month_rows: list[dict[str, Any]]) -> int:
        """Persist 12-month learned seasonal normals and pressure-condition thresholds into `learned_weather_model`."""
        if not month_rows:
            return 0
        now_iso = format_utc_z(datetime.now(timezone.utc))
        params = [
            (
                int(r["month"]),
                r.get("normal_temp_max_f"),
                r.get("normal_temp_min_f"),
                r.get("normal_temp_mean_f"),
                r.get("normal_precip_in"),
                r.get("rain_day_freq"),
                r.get("mean_pressure_hpa"),
                r.get("rain_pressure_threshold_hpa"),
                r.get("clear_pressure_threshold_hpa"),
                r.get("max_wind_mph_p90"),
                int(r.get("sample_days") or 0),
                now_iso,
            )
            for r in month_rows
        ]
        with self._lock:
            if self._conn is None:
                return 0
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT INTO learned_weather_model (
                        month, normal_temp_max_f, normal_temp_min_f, normal_temp_mean_f,
                        normal_precip_in, rain_day_freq, mean_pressure_hpa,
                        rain_pressure_threshold_hpa, clear_pressure_threshold_hpa,
                        max_wind_mph_p90, sample_days, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(month) DO UPDATE SET
                        normal_temp_max_f = excluded.normal_temp_max_f,
                        normal_temp_min_f = excluded.normal_temp_min_f,
                        normal_temp_mean_f = excluded.normal_temp_mean_f,
                        normal_precip_in = excluded.normal_precip_in,
                        rain_day_freq = excluded.rain_day_freq,
                        mean_pressure_hpa = excluded.mean_pressure_hpa,
                        rain_pressure_threshold_hpa = excluded.rain_pressure_threshold_hpa,
                        clear_pressure_threshold_hpa = excluded.clear_pressure_threshold_hpa,
                        max_wind_mph_p90 = excluded.max_wind_mph_p90,
                        sample_days = excluded.sample_days,
                        updated_at = excluded.updated_at;
                    """,
                    params,
                )
        try:
            self.set_setting("learned_model_synced_at", now_iso)
        except Exception:
            pass
        return len(params)

    def get_learned_weather_model(self) -> dict[int, dict[str, Any]]:
        """Return the 12-month learned climatology model keyed by month (1..12)."""
        with self._lock:
            if self._conn is None:
                return {}
            rows = self._conn.execute(
                "SELECT * FROM learned_weather_model ORDER BY month ASC;"
            ).fetchall()
            return {int(r["month"]): dict(r) for r in rows}

    def record_weather_feedback(
        self,
        verdict: str,
        actual_condition: Optional[str] = None,
        notes: Optional[str] = None,
        predicted_condition: Optional[str] = None,
        predicted_temp_f: Optional[float] = None,
        pressure_hpa: Optional[float] = None,
        dew_spread_f: Optional[float] = None,
    ) -> dict[str, Any]:
        """Record human ground-truth weather reaffirmation and tune local condition bias weights."""
        verdict_norm = str(verdict or "").strip().lower()
        if verdict_norm not in ("yes", "partial", "no"):
            raise ValueError("verdict must be 'yes', 'partial', or 'no'.")

        actual_clean = str(actual_condition or "").strip()[:60] or None
        notes_clean = str(notes or "").strip()[:240] or None
        now_iso = format_utc_z(datetime.now(timezone.utc))

        if predicted_condition is None or predicted_temp_f is None or pressure_hpa is None or dew_spread_f is None:
            try:
                ov = self.get_weather_overview()
                cond = ov.get("condition") or {}
                out = ov.get("outdoor") or {}
                baro = ov.get("barometer") or {}
                if predicted_condition is None:
                    predicted_condition = str(cond.get("label") or "Partly Cloudy")
                if predicted_temp_f is None and out.get("temp_f") is not None:
                    predicted_temp_f = float(out["temp_f"])
                if pressure_hpa is None and baro.get("pressure_hpa") is not None:
                    pressure_hpa = float(baro["pressure_hpa"])
                if dew_spread_f is None and out.get("dew_spread_f") is not None:
                    dew_spread_f = float(out["dew_spread_f"])
            except Exception:
                pass

        if verdict_norm == "yes" and not actual_clean:
            actual_clean = predicted_condition

        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO weather_feedback (
                        timestamp, predicted_condition, predicted_temp_f,
                        user_verdict, actual_condition, pressure_hpa, dew_spread_f, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        now_iso,
                        predicted_condition,
                        predicted_temp_f,
                        verdict_norm,
                        actual_clean,
                        pressure_hpa,
                        dew_spread_f,
                        notes_clean,
                    ),
                )

        # Tune local bias weights based on user ground-truth reaffirmation
        try:
            fog_bias = float(self.get_setting("learned_dew_spread_fog_bias_f", "0.0") or "0.0")
            rain_baro_bias = float(self.get_setting("learned_baro_rain_bias_inhg", "0.0") or "0.0")
            pred_lower = (predicted_condition or "").lower()
            act_lower = (actual_clean or "").lower()

            if verdict_norm in ("no", "partial") and act_lower:
                if "fog" in act_lower and "fog" not in pred_lower:
                    fog_bias = min(2.5, round(fog_bias + 0.35, 2))
                elif "fog" in pred_lower and "fog" not in act_lower:
                    fog_bias = max(-1.5, round(fog_bias - 0.35, 2))

                if "rain" in act_lower and "rain" not in pred_lower:
                    rain_baro_bias = min(0.12, round(rain_baro_bias + 0.015, 3))
                elif "rain" in pred_lower and ("sunny" in act_lower or "clear" in act_lower):
                    rain_baro_bias = max(-0.12, round(rain_baro_bias - 0.015, 3))

            self.set_setting("learned_dew_spread_fog_bias_f", str(fog_bias))
            self.set_setting("learned_baro_rain_bias_inhg", str(rain_baro_bias))
            self.set_setting("last_reaffirmation_at", now_iso)
            self.set_setting("last_reaffirmation_condition", str(predicted_condition or ""))
        except Exception as exc:
            logger.debug("Bias tuning update skipped: %s", exc)

        return self.get_weather_feedback_summary(current_condition=predicted_condition)

    def get_weather_feedback_summary(self, current_condition: Optional[str] = None) -> dict[str, Any]:
        """Return human reaffirmation accuracy score, recent history, and whether the UI should prompt."""
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            if self._conn is None:
                return {
                    "total_feedback": 0,
                    "accuracy_pct": 88,
                    "prompt_due": True,
                    "recent": [],
                }
            rows = self._conn.execute(
                """
                SELECT id, timestamp, predicted_condition, predicted_temp_f,
                       user_verdict, actual_condition, pressure_hpa, dew_spread_f, notes
                FROM weather_feedback
                ORDER BY timestamp DESC, id DESC LIMIT 30;
                """
            ).fetchall()

        total = len(rows)
        score_sum = 0.0
        for r in rows:
            v = str(r["user_verdict"] or "").lower()
            if v == "yes":
                score_sum += 1.0
            elif v == "partial":
                score_sum += 0.6
            else:
                score_sum += 0.15

        # Bayesian smoothed accuracy score (prior of 4 affirmations at 88%)
        accuracy_pct = int(round(((score_sum + 3.52) / (total + 4.0)) * 100.0))
        accuracy_pct = max(35, min(99, accuracy_pct))

        last_ts = str(rows[0]["timestamp"]) if rows else None
        last_pred = str(rows[0]["predicted_condition"] or "") if rows else ""
        hours_since_last: Optional[float] = None
        if last_ts:
            dt_last = parse_utc_iso(last_ts)
            if dt_last:
                hours_since_last = round(max(0.0, (now_utc - dt_last).total_seconds() / 3600.0), 2)

        # Prompt at most once every 6 hours, OR if the weather condition shifted significantly and >1h elapsed
        condition_shifted = bool(
            current_condition
            and last_pred
            and current_condition.strip().lower() != last_pred.strip().lower()
        )
        if hours_since_last is None:
            prompt_due = True
        elif hours_since_last >= 6.0:
            prompt_due = True
        elif condition_shifted and hours_since_last >= 1.5:
            prompt_due = True
        else:
            prompt_due = False

        fog_bias = float(self.get_setting("learned_dew_spread_fog_bias_f", "0.0") or "0.0")
        rain_baro_bias = float(self.get_setting("learned_baro_rain_bias_inhg", "0.0") or "0.0")

        return {
            "total_feedback": total,
            "accuracy_pct": accuracy_pct,
            "prompt_due": prompt_due,
            "hours_since_last": hours_since_last,
            "last_feedback_at": last_ts,
            "learned_biases": {
                "dew_spread_fog_bias_f": fog_bias,
                "baro_rain_bias_inhg": rain_baro_bias,
            },
            "recent": [dict(r) for r in rows[:10]],
        }

    def verify_or_correct_calendar_day(
        self,
        date_str: str,
        verdict: str = "accurate",
        condition_type: Optional[str] = None,
        clear_lightning: bool = False,
        notes: str = "",
        predicted_condition: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist human verification or weather correction for a past/present calendar date and train model weights."""
        try:
            datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be formatted as YYYY-MM-DD") from exc

        date_clean = str(date_str).strip()
        verdict_raw = str(verdict or "accurate").strip().lower()
        verdict_norm = (
            "corrected"
            if verdict_raw in ("corrected", "correct", "no", "wrong", "override")
            else "accurate"
        )

        cond_key = str(condition_type or "").strip().lower().replace("-", "_").replace(" ", "_")
        if cond_key in ("clear", "fair"):
            cond_key = "sunny"
        elif cond_key in ("mostly_cloudy", "overcast"):
            cond_key = "cloudy"
        elif cond_key in ("showers", "drizzle"):
            cond_key = "rain"
        elif cond_key in ("thunderstorm", "tstorm", "severe"):
            cond_key = "storm"

        cond_label = CANONICAL_WEATHER_CONDITIONS.get(cond_key) if cond_key else None
        if verdict_norm == "corrected" and not cond_label:
            raise ValueError(
                f"Valid condition_type required ({', '.join(CANONICAL_WEATHER_CONDITIONS.keys())})"
            )

        exclude_lightning = bool(clear_lightning) or (bool(cond_key) and cond_key != "storm")
        now_iso = format_utc_z(datetime.now(timezone.utc))
        today_local = datetime.now().strftime("%Y-%m-%d")
        demoted_strikes = 0

        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database not initialized")

            ds_row = self._conn.execute(
                """
                SELECT temp_max_f, temp_mean_c, humidity_mean, pressure_min_hpa, pressure_max_hpa
                FROM daily_summaries
                WHERE date_str = ?;
                """,
                (date_clean,),
            ).fetchone()

            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO daily_weather_overrides (
                        date_str, verified_status, corrected_condition_type,
                        corrected_condition_label, exclude_false_lightning, notes, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date_str) DO UPDATE SET
                        verified_status = excluded.verified_status,
                        corrected_condition_type = excluded.corrected_condition_type,
                        corrected_condition_label = excluded.corrected_condition_label,
                        exclude_false_lightning = excluded.exclude_false_lightning,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        date_clean,
                        verdict_norm,
                        cond_key or None,
                        cond_label,
                        1 if exclude_lightning else 0,
                        str(notes or "").strip()[:300],
                        now_iso,
                    ),
                )

                if exclude_lightning:
                    cur_lt = self._conn.execute(
                        """
                        UPDATE lightning_strikes
                        SET confirmed = 0
                        WHERE DATE(timestamp, 'localtime') = ?
                          AND (confirmed = 1 OR confirmed IS NULL);
                        """,
                        (date_clean,),
                    )
                    demoted_strikes = int(cur_lt.rowcount or 0)
                    if date_clean == today_local:
                        self._conn.execute(
                            """
                            UPDATE sensor_readings
                            SET storm_dist_mi = NULL, rfi_detected = 0
                            WHERE DATE(timestamp, 'localtime') = ?
                              AND (storm_dist_mi IS NOT NULL OR rfi_detected = 1);
                            """,
                            (date_clean,),
                        )
                elif cond_key == "storm":
                    self._conn.execute(
                        """
                        UPDATE lightning_strikes
                        SET confirmed = 1
                        WHERE DATE(timestamp, 'localtime') = ? AND confirmed = 0;
                        """,
                        (date_clean,),
                    )

        pred_temp_f = (
            round(float(ds_row["temp_max_f"]), 1)
            if ds_row and ds_row["temp_max_f"] is not None
            else None
        )
        pressure_hpa: Optional[float] = None
        if ds_row:
            p_vals = [float(x) for x in (ds_row["pressure_min_hpa"], ds_row["pressure_max_hpa"]) if x is not None]
            if p_vals:
                pressure_hpa = round(sum(p_vals) / len(p_vals), 2)

        dew_spread_f: Optional[float] = None
        if ds_row and ds_row["temp_mean_c"] is not None and ds_row["humidity_mean"] is not None:
            dp_c = compute_dew_point_c(float(ds_row["temp_mean_c"]), float(ds_row["humidity_mean"]))
            if dp_c is not None:
                dew_spread_f = round((float(ds_row["temp_mean_c"]) - float(dp_c)) * 9.0 / 5.0, 1)

        pred_label = str(predicted_condition or cond_label or "Observed")
        actual_label = cond_label or pred_label
        fb_verdict = "yes" if verdict_norm == "accurate" else "no"
        note_prefix = f"Calendar {date_clean}"
        if demoted_strikes > 0:
            note_prefix += f" (cleared {demoted_strikes} false EMI pulse(s))"
        if notes:
            note_prefix += f": {str(notes).strip()}"

        feedback_summary = self.record_weather_feedback(
            verdict=fb_verdict,
            predicted_condition=pred_label,
            predicted_temp_f=pred_temp_f,
            actual_condition=actual_label,
            pressure_hpa=pressure_hpa,
            dew_spread_f=dew_spread_f,
            notes=note_prefix[:300],
        )

        if verdict_norm == "corrected" and demoted_strikes > 0 and cond_key in ("sunny", "partly_cloudy", "cloudy"):
            try:
                self.set_setting("last_calendar_emi_purge_date", date_clean)
            except Exception:
                pass

        return {
            "ok": True,
            "date": date_clean,
            "verified_status": verdict_norm,
            "condition_type": cond_key or None,
            "condition_label": cond_label,
            "exclude_false_lightning": exclude_lightning,
            "demoted_strikes": demoted_strikes,
            "updated_at": now_iso,
            "feedback_summary": feedback_summary,
        }

    def get_weather_overview(self) -> dict[str, Any]:
        """Synthesize current outdoor/indoor conditions, barometer (`inHg`), compass wind, and nowcast."""
        now_utc = datetime.now(timezone.utc)
        coords = self.get_location_coordinates()

        # 1. Auto-heal trusted sensors on the fly if needed
        self._auto_heal_trusted_sensors()
        sensors = self.get_all_sensors()

        cutoff_30m = now_utc - timedelta(minutes=30)
        cutoff_24h = now_utc - timedelta(hours=24)

        trusted_outdoor = [
            s for s in sensors if s.get("is_trusted") and s.get("location_type") == "outdoor"
        ]
        trusted_indoor = [
            s for s in sensors if s.get("is_trusted") and s.get("location_type") == "indoor"
        ]

        outdoor_sensors: list[dict[str, Any]] = []
        indoor_sensors: list[dict[str, Any]] = []

        with self._lock:
            # Resolve outdoor sensors with 30m fresh -> 24h fallback
            for s in trusted_outdoor:
                s_key = s.get("sensor_key") or self.make_sensor_key(s["model"], s["sensor_id"])
                latest = s.get("latest_reading")
                r_ts = parse_utc_iso(latest.get("timestamp") or latest.get("reading_timestamp")) if latest else None

                effective_r = None
                is_fresh = False
                if latest and r_ts and r_ts >= cutoff_30m:
                    effective_r = latest
                    is_fresh = True
                elif self._conn is not None:
                    fb_row = self._conn.execute(
                        """
                        SELECT * FROM sensor_readings
                        WHERE (sensor_key = ? OR (model = ? AND sensor_id = ?))
                          AND timestamp >= ?
                        ORDER BY timestamp DESC, id DESC LIMIT 1;
                        """,
                        (s_key, s["model"], s["sensor_id"], format_utc_z(cutoff_24h)),
                    ).fetchone()
                    if fb_row:
                        effective_r = dict(fb_row)
                        fb_ts = parse_utc_iso(effective_r.get("timestamp"))
                        is_fresh = bool(fb_ts and fb_ts >= cutoff_30m)
                    elif latest:
                        effective_r = latest

                if effective_r:
                    s_copy = dict(s)
                    s_copy["effective_reading"] = effective_r
                    s_copy["is_fresh_30m"] = is_fresh
                    outdoor_sensors.append(s_copy)

            # Resolve indoor sensors with 30m fresh -> 24h fallback
            for s in trusted_indoor:
                s_key = s.get("sensor_key") or self.make_sensor_key(s["model"], s["sensor_id"])
                latest = s.get("latest_reading")
                r_ts = parse_utc_iso(latest.get("timestamp") or latest.get("reading_timestamp")) if latest else None

                effective_r = None
                is_fresh = False
                if latest and r_ts and r_ts >= cutoff_30m:
                    effective_r = latest
                    is_fresh = True
                elif self._conn is not None:
                    fb_row = self._conn.execute(
                        """
                        SELECT * FROM sensor_readings
                        WHERE (sensor_key = ? OR (model = ? AND sensor_id = ?))
                          AND timestamp >= ?
                        ORDER BY timestamp DESC, id DESC LIMIT 1;
                        """,
                        (s_key, s["model"], s["sensor_id"], format_utc_z(cutoff_24h)),
                    ).fetchone()
                    if fb_row:
                        effective_r = dict(fb_row)
                        fb_ts = parse_utc_iso(effective_r.get("timestamp"))
                        is_fresh = bool(fb_ts and fb_ts >= cutoff_30m)
                    elif latest:
                        effective_r = latest

                if effective_r:
                    s_copy = dict(s)
                    s_copy["effective_reading"] = effective_r
                    s_copy["is_fresh_30m"] = is_fresh
                    indoor_sensors.append(s_copy)

        out_temps_c: list[float] = []
        out_rh: list[float] = []
        out_press: list[float] = []
        out_wind_mps: list[float] = []
        out_gust_mps: list[float] = []
        out_wind_dirs: list[float] = []
        out_uv: list[float] = []
        out_lux: list[float] = []

        for s in outdoor_sensors:
            r = s.get("effective_reading") or s.get("latest_reading")
            if not r:
                continue
            hidden = set(s.get("hidden_metrics") or [])
            if "temperature" not in hidden and r.get("temp_c") is not None:
                tc = float(r["temp_c"])
                if -31.67 <= tc <= 54.44:
                    out_temps_c.append(tc)
            if "humidity" not in hidden and r.get("humidity") is not None:
                out_rh.append(float(r["humidity"]))
            if "pressure" not in hidden and r.get("pressure_hpa") is not None:
                p_val = float(r["pressure_hpa"])
                if MIN_PLAUSIBLE_PRESSURE_HPA <= p_val <= MAX_PLAUSIBLE_PRESSURE_HPA:
                    out_press.append(p_val)
            if "wind" not in hidden:
                if r.get("wind_avg_mps") is not None:
                    out_wind_mps.append(float(r["wind_avg_mps"]))
                if r.get("wind_gust_mps") is not None:
                    out_gust_mps.append(float(r["wind_gust_mps"]))
                if r.get("wind_dir_deg") is not None:
                    out_wind_dirs.append(float(r["wind_dir_deg"]))
            if "uv_light" not in hidden:
                if r.get("uv_index") is not None:
                    out_uv.append(float(r["uv_index"]))
                if r.get("lux") is not None:
                    out_lux.append(float(r["lux"]))

        mean_temp_c = round(sum(out_temps_c) / len(out_temps_c), 2) if out_temps_c else None
        mean_temp_f = round((mean_temp_c * 9.0 / 5.0) + 32.0, 1) if mean_temp_c is not None else None
        min_temp_f = (
            round((min(out_temps_c) * 9.0 / 5.0) + 32.0, 1) if out_temps_c else None
        )
        max_temp_f = (
            round((max(out_temps_c) * 9.0 / 5.0) + 32.0, 1) if out_temps_c else None
        )
        mean_rh = round(sum(out_rh) / len(out_rh), 1) if out_rh else None
        rf_mean_press = round(sum(out_press) / len(out_press), 1) if out_press else None

        dp_c = compute_dew_point_c(mean_temp_c, mean_rh)
        dp_f = round((dp_c * 9.0 / 5.0) + 32.0, 1) if dp_c is not None else None

        wind_avg_mps = round(sum(out_wind_mps) / len(out_wind_mps), 2) if out_wind_mps else None
        wind_avg_mph = round(wind_avg_mps * 2.23693629, 1) if wind_avg_mps is not None else None
        wind_gust_mps = round(max(out_gust_mps), 2) if out_gust_mps else None
        wind_gust_mph = round(wind_gust_mps * 2.23693629, 1) if wind_gust_mps is not None else None

        wind_dir_deg: Optional[float] = None
        if out_wind_dirs:
            sin_sum = sum(math.sin(math.radians(d)) for d in out_wind_dirs)
            cos_sum = sum(math.cos(math.radians(d)) for d in out_wind_dirs)
            wind_dir_deg = round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0, 0)
        wind_cardinal = _deg_to_cardinal(wind_dir_deg)

        feels_like_f = compute_feels_like_f(mean_temp_f, mean_rh, wind_avg_mph)
        feels_like_c = (
            round((feels_like_f - 32.0) * 5.0 / 9.0, 1) if feels_like_f is not None else None
        )

        uv_val = round(max(out_uv), 1) if out_uv else None
        lux_val = round(max(out_lux), 0) if out_lux else None

        # Aggregate Indoor Summary
        in_temps_c: list[float] = []
        in_rh: list[float] = []
        for s in indoor_sensors:
            r = s.get("effective_reading") or s.get("latest_reading")
            if not r:
                continue
            hidden = set(s.get("hidden_metrics") or [])
            if "temperature" not in hidden and r.get("temp_c") is not None:
                in_temps_c.append(float(r["temp_c"]))
            if "humidity" not in hidden and r.get("humidity") is not None:
                in_rh.append(float(r["humidity"]))

        in_temp_c = round(sum(in_temps_c) / len(in_temps_c), 2) if in_temps_c else None
        in_temp_f = round((in_temp_c * 9.0 / 5.0) + 32.0, 1) if in_temp_c is not None else None
        in_humidity = round(sum(in_rh) / len(in_rh), 1) if in_rh else None

        rain_24h_mm = 0.0
        rain_30m_mm = 0.0
        rain_today_mm = 0.0
        rain_week_mm = 0.0
        rain_month_mm = 0.0
        rain_year_mm = 0.0
        rain_rate_mm_h = 0.0
        temp_delta_1h_f: Optional[float] = None
        temp_delta_3h_f: Optional[float] = None
        rh_delta_1h: Optional[float] = None
        rh_delta_3h: Optional[float] = None
        wind_dir_1h_ago: Optional[str] = None
        in_temp_delta_1h_f: Optional[float] = None

        # Today's & 30d/All-Time Extremes + Timestamps + Wind 15m variance + 24h Sparkline
        today_out_low_f: Optional[float] = None
        today_out_low_ts: Optional[str] = None
        today_out_high_f: Optional[float] = None
        today_out_high_ts: Optional[str] = None
        today_in_low_f: Optional[float] = None
        today_in_low_ts: Optional[str] = None
        today_in_high_f: Optional[float] = None
        today_in_high_ts: Optional[str] = None
        today_rh_low: Optional[float] = None
        today_rh_high: Optional[float] = None
        today_lux_peak: Optional[float] = lux_val
        today_uv_peak: Optional[float] = uv_val
        today_peak_gust_mph: Optional[float] = None
        today_baro_low_inhg: Optional[float] = None
        today_baro_high_inhg: Optional[float] = None
        wind_10m_avg_mph: Optional[float] = wind_avg_mph
        wind_15m_bearings: list[float] = list(out_wind_dirs)
        sparkline_24h_f: list[float] = []
        sparkline_dew_24h_f: list[float] = []
        sparkline_in_24h_f: list[float] = []

        now_local = datetime.now().astimezone()
        midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_today = format_utc_z(midnight_local)
        cutoff_week = format_utc_z(now_local - timedelta(days=7))
        cutoff_month = format_utc_z(midnight_local.replace(day=1))
        cutoff_year = format_utc_z(midnight_local.replace(month=1, day=1))

        all_outdoor_keys = [s["sensor_key"] for s in outdoor_sensors]
        all_indoor_keys = [s["sensor_key"] for s in indoor_sensors]
        outdoor_keys = [
            s["sensor_key"]
            for s in outdoor_sensors
            if "temperature" not in set(s.get("hidden_metrics") or [])
        ]
        indoor_temp_keys = [
            s["sensor_key"]
            for s in indoor_sensors
            if "temperature" not in set(s.get("hidden_metrics") or [])
        ]
        rain_keys = [
            s["sensor_key"]
            for s in outdoor_sensors
            if "rain" not in set(s.get("hidden_metrics") or [])
        ]
        wind_keys = [
            s["sensor_key"]
            for s in outdoor_sensors
            if "wind" not in set(s.get("hidden_metrics") or [])
        ]

        def _query_rain_delta(keys: list[str], since_iso: str) -> float:
            if not keys or self._conn is None:
                return 0.0
            ph_r = ",".join("?" for _ in keys)
            r_rows = self._conn.execute(
                f"""
                SELECT COALESCE(sensor_key, model || '_' || sensor_id) AS skey,
                       MAX(rain_cumulative_mm) - MIN(rain_cumulative_mm) AS delta_mm
                FROM sensor_readings
                WHERE timestamp >= ?
                  AND rain_cumulative_mm IS NOT NULL
                  AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_r})
                GROUP BY skey;
                """,
                [since_iso, *keys],
            ).fetchall()
            vals = [float(x["delta_mm"] or 0.0) for x in r_rows if (x["delta_mm"] or 0.0) >= 0.0]
            return max(vals) if vals else 0.0

        with self._lock:
            if self._conn is not None:
                if rain_keys:
                    cutoff_24h = format_utc_z(now_utc - timedelta(hours=24))
                    cutoff_30m = format_utc_z(now_utc - timedelta(minutes=30))
                    rain_24h_mm = _query_rain_delta(rain_keys, cutoff_24h)
                    rain_30m_mm = _query_rain_delta(rain_keys, cutoff_30m)
                    rain_today_mm = _query_rain_delta(rain_keys, cutoff_today)
                    rain_week_mm = _query_rain_delta(rain_keys, cutoff_week)
                    rain_month_mm = _query_rain_delta(rain_keys, cutoff_month)
                    rain_year_mm = _query_rain_delta(rain_keys, cutoff_year)

                    # Latest rain_rate_mm_h or derived from 30m rain
                    ph_rk = ",".join("?" for _ in rain_keys)
                    rr_row = self._conn.execute(
                        f"""
                        SELECT MAX(rain_rate_mm_h) AS max_rate
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND rain_rate_mm_h IS NOT NULL
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_rk});
                        """,
                        [cutoff_30m, *rain_keys],
                    ).fetchone()
                    if rr_row and rr_row["max_rate"] is not None and float(rr_row["max_rate"]) > 0.0:
                        rain_rate_mm_h = float(rr_row["max_rate"])
                    elif rain_30m_mm > 0.0:
                        rain_rate_mm_h = rain_30m_mm * 2.0

                # Outdoor Today High/Low & timestamps + 24h Sparkline
                if outdoor_keys:
                    ph_ok = ",".join("?" for _ in outdoor_keys)
                    row_min = self._conn.execute(
                        f"""
                        SELECT temp_c, timestamp FROM sensor_readings
                        WHERE timestamp >= ? AND temp_c BETWEEN -31.67 AND 54.44
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ok})
                        ORDER BY temp_c ASC, timestamp ASC LIMIT 1;
                        """,
                        [cutoff_today, *outdoor_keys],
                    ).fetchone()
                    if row_min and row_min["temp_c"] is not None:
                        today_out_low_f = round((float(row_min["temp_c"]) * 9.0 / 5.0) + 32.0, 1)
                        today_out_low_ts = str(row_min["timestamp"])

                    row_max = self._conn.execute(
                        f"""
                        SELECT temp_c, timestamp FROM sensor_readings
                        WHERE timestamp >= ? AND temp_c BETWEEN -31.67 AND 54.44
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ok})
                        ORDER BY temp_c DESC, timestamp DESC LIMIT 1;
                        """,
                        [cutoff_today, *outdoor_keys],
                    ).fetchone()
                    if row_max and row_max["temp_c"] is not None:
                        today_out_high_f = round((float(row_max["temp_c"]) * 9.0 / 5.0) + 32.0, 1)
                        today_out_high_ts = str(row_max["timestamp"])

                # If local outdoor sensor extremes for today are not yet populated, fallback to reference weather for today
                if today_out_low_f is None or today_out_high_f is None:
                    ref_ext = self._conn.execute(
                        """
                        SELECT MIN(temp_c) AS min_tc, MAX(temp_c) AS max_tc,
                               MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts
                        FROM reference_weather_history
                        WHERE timestamp >= ? AND temp_c IS NOT NULL;
                        """,
                        (cutoff_today,),
                    ).fetchone()
                    if ref_ext:
                        if today_out_low_f is None and ref_ext["min_tc"] is not None:
                            today_out_low_f = round((float(ref_ext["min_tc"]) * 9.0 / 5.0) + 32.0, 1)
                            today_out_low_ts = str(ref_ext["min_ts"])
                        if today_out_high_f is None and ref_ext["max_tc"] is not None:
                            today_out_high_f = round((float(ref_ext["max_tc"]) * 9.0 / 5.0) + 32.0, 1)
                            today_out_high_ts = str(ref_ext["max_ts"])

                # 24h Hourly Sparkline for Outdoor Hero Card (Temperature & Dew Point)
                cutoff_spark = format_utc_z(now_utc - timedelta(hours=24))
                if outdoor_keys:
                    ph_ok = ",".join("?" for _ in outdoor_keys)
                    spark_rows = self._conn.execute(
                        f"""
                        SELECT SUBSTR(timestamp, 1, 13) AS hr, AVG(temp_c) AS avg_tc, AVG(humidity) AS avg_rh
                        FROM sensor_readings
                        WHERE timestamp >= ? AND temp_c BETWEEN -31.67 AND 54.44
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ok})
                        GROUP BY SUBSTR(timestamp, 1, 13)
                        ORDER BY hr ASC;
                        """,
                        [cutoff_spark, *outdoor_keys],
                    ).fetchall()
                    sparkline_24h_f = [
                        round((float(sr["avg_tc"]) * 9.0 / 5.0) + 32.0, 1)
                        for sr in spark_rows
                        if sr["avg_tc"] is not None
                    ]
                    sparkline_dew_24h_f = []
                    for sr in spark_rows:
                        if sr["avg_tc"] is not None and sr["avg_rh"] is not None:
                            dpc = compute_dew_point_c(float(sr["avg_tc"]), float(sr["avg_rh"]))
                            if dpc is not None:
                                sparkline_dew_24h_f.append(round((dpc * 9.0 / 5.0) + 32.0, 1))
                            else:
                                sparkline_dew_24h_f.append(round((float(sr["avg_tc"]) * 9.0 / 5.0) + 24.0, 1))

                if not sparkline_24h_f:
                    ref_sparks = self._conn.execute(
                        """
                        SELECT SUBSTR(timestamp, 1, 13) AS hr, AVG(temp_c) AS avg_tc, AVG(humidity) AS avg_rh
                        FROM reference_weather_history
                        WHERE timestamp >= ? AND temp_c IS NOT NULL
                        GROUP BY SUBSTR(timestamp, 1, 13)
                        ORDER BY hr ASC;
                        """,
                        (cutoff_spark,),
                    ).fetchall()
                    sparkline_24h_f = [
                        round((float(sr["avg_tc"]) * 9.0 / 5.0) + 32.0, 1)
                        for sr in ref_sparks
                        if sr["avg_tc"] is not None
                    ]
                    sparkline_dew_24h_f = []
                    for sr in ref_sparks:
                        if sr["avg_tc"] is not None and sr["avg_rh"] is not None:
                            dpc = compute_dew_point_c(float(sr["avg_tc"]), float(sr["avg_rh"]))
                            if dpc is not None:
                                sparkline_dew_24h_f.append(round((dpc * 9.0 / 5.0) + 32.0, 1))
                            else:
                                sparkline_dew_24h_f.append(round((float(sr["avg_tc"]) * 9.0 / 5.0) + 24.0, 1))

                if all_outdoor_keys:
                    ph_aok = ",".join("?" for _ in all_outdoor_keys)
                    row_rh_ext = self._conn.execute(
                        f"""
                        SELECT MIN(humidity) AS min_rh, MAX(humidity) AS max_rh,
                               MAX(lux) AS max_lux, MAX(uv_index) AS max_uv
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_aok});
                        """,
                        [cutoff_today, *all_outdoor_keys],
                    ).fetchone()
                    if row_rh_ext:
                        if row_rh_ext["min_rh"] is not None:
                            today_rh_low = round(float(row_rh_ext["min_rh"]), 0)
                        if row_rh_ext["max_rh"] is not None:
                            today_rh_high = round(float(row_rh_ext["max_rh"]), 0)
                        if row_rh_ext["max_lux"] is not None:
                            today_lux_peak = round(float(row_rh_ext["max_lux"]), 0)
                        if row_rh_ext["max_uv"] is not None:
                            today_uv_peak = round(float(row_rh_ext["max_uv"]), 1)

                if wind_keys:
                    ph_wk = ",".join("?" for _ in wind_keys)
                    row_w_today = self._conn.execute(
                        f"""
                        SELECT MAX(COALESCE(wind_gust_mps, wind_avg_mps)) AS max_w
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_wk});
                        """,
                        [cutoff_today, *wind_keys],
                    ).fetchone()
                    if row_w_today and row_w_today["max_w"] is not None:
                        today_peak_gust_mph = round(float(row_w_today["max_w"]) * 2.23693629, 1)

                    cutoff_10m = format_utc_z(now_utc - timedelta(minutes=10))
                    row_w_10m = self._conn.execute(
                        f"""
                        SELECT AVG(wind_avg_mps) AS avg_w
                        FROM sensor_readings
                        WHERE timestamp >= ? AND wind_avg_mps IS NOT NULL
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_wk});
                        """,
                        [cutoff_10m, *wind_keys],
                    ).fetchone()
                    if row_w_10m and row_w_10m["avg_w"] is not None:
                        wind_10m_avg_mph = round(float(row_w_10m["avg_w"]) * 2.23693629, 1)

                    cutoff_15m = format_utc_z(now_utc - timedelta(minutes=15))
                    rows_w_15m = self._conn.execute(
                        f"""
                        SELECT wind_dir_deg FROM sensor_readings
                        WHERE timestamp >= ? AND wind_dir_deg IS NOT NULL
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_wk})
                        ORDER BY timestamp DESC LIMIT 40;
                        """,
                        [cutoff_15m, *wind_keys],
                    ).fetchall()
                    if rows_w_15m:
                        wind_15m_bearings = [float(r["wind_dir_deg"]) for r in rows_w_15m]

                # Indoor Today High/Low & 1h delta + 24h Sparkline
                if indoor_temp_keys:
                    ph_ik = ",".join("?" for _ in indoor_temp_keys)
                    in_min = self._conn.execute(
                        f"""
                        SELECT temp_c, timestamp FROM sensor_readings
                        WHERE timestamp >= ? AND temp_c BETWEEN -31.67 AND 54.44
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ik})
                        ORDER BY temp_c ASC, timestamp ASC LIMIT 1;
                        """,
                        [cutoff_today, *indoor_temp_keys],
                    ).fetchone()
                    if in_min and in_min["temp_c"] is not None:
                        today_in_low_f = round((float(in_min["temp_c"]) * 9.0 / 5.0) + 32.0, 1)
                        today_in_low_ts = str(in_min["timestamp"])

                    in_max = self._conn.execute(
                        f"""
                        SELECT temp_c, timestamp FROM sensor_readings
                        WHERE timestamp >= ? AND temp_c BETWEEN -31.67 AND 54.44
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ik})
                        ORDER BY temp_c DESC, timestamp DESC LIMIT 1;
                        """,
                        [cutoff_today, *indoor_temp_keys],
                    ).fetchone()
                    if in_max and in_max["temp_c"] is not None:
                        today_in_high_f = round((float(in_max["temp_c"]) * 9.0 / 5.0) + 32.0, 1)
                        today_in_high_ts = str(in_max["timestamp"])

                    if in_temp_f is not None:
                        t_1h_s = format_utc_z(now_utc - timedelta(minutes=75))
                        t_1h_e = format_utc_z(now_utc - timedelta(minutes=30))
                        in_1h = self._conn.execute(
                            f"""
                            SELECT AVG(temp_c) AS avg_tc FROM sensor_readings
                            WHERE timestamp BETWEEN ? AND ?
                              AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ik});
                            """,
                            [t_1h_s, t_1h_e, *indoor_temp_keys],
                        ).fetchone()
                        if in_1h and in_1h["avg_tc"] is not None:
                            past_in_tf = (float(in_1h["avg_tc"]) * 9.0 / 5.0) + 32.0
                            in_temp_delta_1h_f = round(in_temp_f - past_in_tf, 1)

                    cutoff_spark = format_utc_z(now_utc - timedelta(hours=24))
                    in_spark_rows = self._conn.execute(
                        f"""
                        SELECT SUBSTR(timestamp, 1, 13) AS hr, AVG(temp_c) AS avg_tc
                        FROM sensor_readings
                        WHERE timestamp >= ? AND temp_c BETWEEN -31.67 AND 54.44
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({ph_ik})
                        GROUP BY SUBSTR(timestamp, 1, 13)
                        ORDER BY hr ASC;
                        """,
                        [cutoff_spark, *indoor_temp_keys],
                    ).fetchall()
                    sparkline_in_24h_f = [
                        round((float(sr["avg_tc"]) * 9.0 / 5.0) + 32.0, 1)
                        for sr in in_spark_rows
                        if sr["avg_tc"] is not None
                    ]

                # Today's Barometric High/Low
                row_baro_td = self._conn.execute(
                    """
                    SELECT MIN(pressure_hpa) AS min_p, MAX(pressure_hpa) AS max_p
                    FROM reference_weather_history
                    WHERE timestamp >= ? AND pressure_hpa BETWEEN 870.0 AND 1085.0;
                    """,
                    (cutoff_today,),
                ).fetchone()
                if row_baro_td:
                    if row_baro_td["min_p"] is not None:
                        today_baro_low_inhg = round(float(row_baro_td["min_p"]) * HPA_TO_INHG, 2)
                    if row_baro_td["max_p"] is not None:
                        today_baro_high_inhg = round(float(row_baro_td["max_p"]) * HPA_TO_INHG, 2)

                if outdoor_keys and mean_temp_f is not None:
                    placeholders = ",".join("?" for _ in outdoor_keys)
                    t_1h_start = format_utc_z(now_utc - timedelta(minutes=75))
                    t_1h_end = format_utc_z(now_utc - timedelta(minutes=30))
                    row_1h = self._conn.execute(
                        f"""
                        SELECT AVG(temp_c) AS avg_tc, AVG(humidity) AS avg_rh, AVG(wind_dir_deg) AS avg_wdir
                        FROM sensor_readings
                        WHERE timestamp BETWEEN ? AND ?
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({placeholders});
                        """,
                        [t_1h_start, t_1h_end, *outdoor_keys],
                    ).fetchone()

                    if row_1h and row_1h["avg_tc"] is not None:
                        past_tf = (float(row_1h["avg_tc"]) * 9.0 / 5.0) + 32.0
                        temp_delta_1h_f = round(mean_temp_f - past_tf, 1)
                    if row_1h and row_1h["avg_rh"] is not None and mean_rh is not None:
                        rh_delta_1h = round(mean_rh - float(row_1h["avg_rh"]), 1)
                    if row_1h and row_1h["avg_wdir"] is not None:
                        wind_dir_1h_ago = _deg_to_cardinal(float(row_1h["avg_wdir"]))

                    t_3h_start = format_utc_z(now_utc - timedelta(minutes=210))
                    t_3h_end = format_utc_z(now_utc - timedelta(minutes=150))
                    row_3h = self._conn.execute(
                        f"""
                        SELECT AVG(temp_c) AS avg_tc, AVG(humidity) AS avg_rh
                        FROM sensor_readings
                        WHERE timestamp BETWEEN ? AND ?
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({placeholders});
                        """,
                        [t_3h_start, t_3h_end, *outdoor_keys],
                    ).fetchone()
                    if row_3h and row_3h["avg_tc"] is not None:
                        past_3h_tf = (float(row_3h["avg_tc"]) * 9.0 / 5.0) + 32.0
                        temp_delta_3h_f = round(mean_temp_f - past_3h_tf, 1)
                    if row_3h and row_3h["avg_rh"] is not None and mean_rh is not None:
                        rh_delta_3h = round(mean_rh - float(row_3h["avg_rh"]), 1)

                    if temp_delta_3h_f is None or rh_delta_3h is None:
                        ref_3h = self._conn.execute(
                            """
                            SELECT AVG(temp_c) AS avg_tc, AVG(humidity) AS avg_rh FROM reference_weather_history
                            WHERE timestamp BETWEEN ? AND ?;
                            """,
                            (t_3h_start, t_3h_end),
                        ).fetchone()
                        if ref_3h:
                            if temp_delta_3h_f is None and ref_3h["avg_tc"] is not None:
                                past_3h_tf = (float(ref_3h["avg_tc"]) * 9.0 / 5.0) + 32.0
                                temp_delta_3h_f = round(mean_temp_f - past_3h_tf, 1)
                            if rh_delta_3h is None and ref_3h["avg_rh"] is not None and mean_rh is not None:
                                rh_delta_3h = round(mean_rh - float(ref_3h["avg_rh"]), 1)

        rain_24h_in = round(rain_24h_mm / 25.4, 2)
        rain_today_in = round(rain_today_mm / 25.4, 2)
        rain_week_in = round(rain_week_mm / 25.4, 2)
        rain_month_in = round(rain_month_mm / 25.4, 2)
        rain_year_in = round(rain_year_mm / 25.4, 2)
        rain_rate_in_h = round(rain_rate_mm_h / 25.4, 2)

        dew_spread_f = (
            round(mean_temp_f - dp_f, 1)
            if (mean_temp_f is not None and dp_f is not None)
            else None
        )

        baro = self._compute_barometric_telemetry_and_forecast(
            rf_press_hpa=rf_mean_press,
            dew_spread_f=dew_spread_f,
            wind_cardinal=wind_cardinal,
            rain_30m_mm=rain_30m_mm,
        )

        def _trend_arrow(delta_val: Optional[float], threshold: float = 0.5) -> tuple[str, str]:
            if delta_val is None:
                return "→", "Steady"
            if delta_val >= threshold:
                return "↗", "Rising"
            if delta_val <= -threshold:
                return "↘", "Falling"
            return "→", "Steady"

        out_arrow, out_trend_word = _trend_arrow(temp_delta_1h_f, 0.5)
        in_arrow, in_trend_word = _trend_arrow(in_temp_delta_1h_f, 0.3)

        local_hour = datetime.now().hour
        is_night = (
            (lux_val is not None and lux_val <= 15.0)
            or (lux_val is None and (local_hour >= 19 or local_hour < 6))
        )

        fog_bias_f = float(self.get_setting("learned_dew_spread_fog_bias_f", "0.0") or "0.0")
        fog_dew_spread_thresh = max(1.0, min(6.5, 3.0 + fog_bias_f))

        condition_code = "partly-cloudy-day"
        condition_label = "Partly Cloudy"

        if rain_30m_mm >= 2.0:
            condition_code = "heavy-rain"
            condition_label = "Heavy Rain"
        elif rain_30m_mm > 0.0:
            condition_code = "rain"
            condition_label = "Light Rain / Showers"
        elif (wind_avg_mph is not None and wind_avg_mph >= 12.0) or (
            wind_gust_mph is not None and wind_gust_mph >= 18.0
        ):
            condition_code = "windy"
            condition_label = "Windy & Gusty"
        elif (
            mean_rh is not None
            and mean_rh >= 90.0
            and dew_spread_f is not None
            and dew_spread_f <= fog_dew_spread_thresh
        ):
            condition_code = "fog"
            condition_label = "Fog / Mist Likely"
        elif is_night:
            if mean_rh is not None and mean_rh >= 82.0:
                condition_code = "cloudy-night"
                condition_label = "Humid / Mostly Cloudy Night"
            else:
                condition_code = "clear-night"
                condition_label = "Clear Night"
        else:
            if (lux_val is not None and lux_val >= 20000) or (uv_val is not None and uv_val >= 3.0):
                condition_code = "clear-day"
                condition_label = "Sunny & Clear"
            elif mean_rh is not None and mean_rh >= 80.0:
                condition_code = "cloudy"
                condition_label = "Overcast / High Humidity"
            else:
                condition_code = "partly-cloudy-day"
                condition_label = "Partly Cloudy"

        trend_badges: list[dict[str, str]] = []
        nowcast_clauses: list[str] = []

        # Slot 1: Barometer (always emitted in fixed slot 1)
        if baro.get("pressure_inhg") is not None:
            b_tone = "neutral"
            d_inhg = baro.get("delta_3h_inhg") or 0.0
            if d_inhg <= -0.03:
                b_tone = "cool"
            elif d_inhg >= 0.03:
                b_tone = "info"
            trend_badges.append(
                {
                    "slot": "barometer",
                    "label": f"{baro.get('trend_arrow', '→')} Baro {baro['pressure_inhg']:.2f} inHg ({baro['trend_label'].split(' ')[0]})",
                    "tone": b_tone,
                }
            )
        else:
            trend_badges.append(
                {
                    "slot": "barometer",
                    "label": "→ Baro Steady",
                    "tone": "neutral",
                }
            )

        # Slot 2: Temp Trend (always emitted in fixed slot 2)
        if temp_delta_1h_f is not None:
            if temp_delta_1h_f <= -1.5:
                trend_badges.append(
                    {"slot": "temp_trend", "label": f"↘ Temp {temp_delta_1h_f:+.1f}°F/hr", "tone": "cool"}
                )
                nowcast_clauses.append(f"Cooling quickly ({temp_delta_1h_f:+.1f}°F over the past hour)")
            elif temp_delta_1h_f >= 1.5:
                trend_badges.append(
                    {"slot": "temp_trend", "label": f"↗ Temp {temp_delta_1h_f:+.1f}°F/hr", "tone": "warm"}
                )
                nowcast_clauses.append(f"Warming steadily ({temp_delta_1h_f:+.1f}°F over the past hour)")
            else:
                trend_badges.append(
                    {"slot": "temp_trend", "label": f"→ Temp Steady ({temp_delta_1h_f:+.1f}°F/hr)", "tone": "neutral"}
                )
        else:
            trend_badges.append(
                {"slot": "temp_trend", "label": f"→ Temp {out_trend_word}", "tone": "neutral"}
            )

        # Slot 3: Wind (always emitted in fixed slot 3 — never hidden when steady!)
        if wind_dir_1h_ago and wind_cardinal and wind_dir_1h_ago != wind_cardinal:
            trend_badges.append(
                {"slot": "wind", "label": f"Wind Shift {wind_dir_1h_ago} -> {wind_cardinal}", "tone": "warm"}
            )
        elif wind_cardinal:
            trend_badges.append(
                {"slot": "wind", "label": f"Wind Steady ({wind_cardinal})", "tone": "neutral"}
            )
        else:
            trend_badges.append(
                {"slot": "wind", "label": "Wind Calm / Steady", "tone": "neutral"}
            )

        # Slot 4: Dew Spread (always emitted in fixed slot 4)
        if dew_spread_f is not None:
            trend_badges.append(
                {
                    "slot": "dew_spread",
                    "label": f"Dew Spread {dew_spread_f:.1f}°F",
                    "tone": "info" if dew_spread_f <= 4.0 else "neutral",
                }
            )
        else:
            trend_badges.append(
                {"slot": "dew_spread", "label": "Dew Spread --°F", "tone": "neutral"}
            )

        baro_forecast = baro.get("forecast_24h") or ""
        if rain_30m_mm > 0.0:
            nowcast_summary = (
                f"Active rain detected ({rain_30m_mm / 25.4:.2f} in over last 30 min). "
                f"{baro_forecast}"
            )
        elif dew_spread_f is not None and dew_spread_f <= 3.5 and (mean_rh or 0) >= 85.0:
            nowcast_summary = (
                f"High humidity ({mean_rh:.0f}% RH) with a narrow ~{dew_spread_f:.1f}°F dew spread "
                f"— mist or surface dew likely. {baro_forecast}"
            )
        elif nowcast_clauses:
            nowcast_summary = f"{'; '.join(nowcast_clauses)}. {baro_forecast}"
        elif mean_temp_f is not None:
            ds_str = f"~{dew_spread_f:.1f}°F" if dew_spread_f is not None else "--°F"
            nowcast_summary = (
                f"Steady temperature across {len(outdoor_sensors)} sensors, with a {ds_str} dew spread. {baro_forecast}"
            )
        else:
            nowcast_summary = (
                "No Outdoor trusted sensors configured yet. Open '3. Sensors Manager' and set your station(s) "
                f"to Outdoor (Trusted) to track local weather. {baro_forecast}"
            )

        outdoor_dict: dict[str, Any] = {
            "sensor_count": len(outdoor_sensors),
            "temp_f": mean_temp_f,
            "temp_c": mean_temp_c,
            "temp_min_f": min_temp_f,
            "temp_max_f": max_temp_f,
            "today_low_f": today_out_low_f if today_out_low_f is not None else min_temp_f,
            "today_low_ts": today_out_low_ts,
            "today_high_f": today_out_high_f if today_out_high_f is not None else max_temp_f,
            "today_high_ts": today_out_high_ts,
            "temp_extremes": {
                "low": today_out_low_f if today_out_low_f is not None else min_temp_f,
                "high": today_out_high_f if today_out_high_f is not None else max_temp_f,
                "low_time": today_out_low_ts,
                "high_time": today_out_high_ts,
            },
            "trend_arrow": out_arrow,
            "trend_word": out_trend_word,
            "sparkline_24h_f": sparkline_24h_f,
            "sparkline_dew_24h_f": sparkline_dew_24h_f,
            "humidity": mean_rh,
            "today_rh_low": today_rh_low,
            "today_rh_high": today_rh_high,
            "dew_point_f": dp_f,
            "dew_point_c": dp_c,
            "dew_spread_f": dew_spread_f,
            "feels_like_f": feels_like_f,
            "feels_like_c": feels_like_c,
            "pressure_hpa": baro.get("pressure_hpa"),
            "pressure_inhg": baro.get("pressure_inhg"),
            "today_baro_low_inhg": today_baro_low_inhg,
            "today_baro_high_inhg": today_baro_high_inhg,
            "wind_avg_mph": wind_avg_mph,
            "wind_avg_mps": wind_avg_mps,
            "wind_10m_avg_mph": wind_10m_avg_mph,
            "wind_gust_mph": wind_gust_mph,
            "wind_gust_mps": wind_gust_mps,
            "today_peak_gust_mph": today_peak_gust_mph if today_peak_gust_mph is not None else wind_gust_mph,
            "wind_dir_deg": wind_dir_deg,
            "wind_cardinal": wind_cardinal,
            "wind_15m_bearings": wind_15m_bearings,
            "rain_24h_in": rain_24h_in,
            "rain_24h_mm": round(rain_24h_mm, 1),
            "rain_today_in": rain_today_in,
            "rain_today_mm": round(rain_today_mm, 1),
            "rain_rate_in_h": rain_rate_in_h,
            "rain_rate_mm_h": round(rain_rate_mm_h, 1),
            "rain_week_in": rain_week_in,
            "rain_month_in": rain_month_in,
            "rain_year_in": rain_year_in,
            "uv_index": uv_val,
            "today_uv_peak": today_uv_peak,
            "lux": lux_val,
            "today_lux_peak": today_lux_peak,
            "temp_delta_1h_f": temp_delta_1h_f,
            "temp_delta_3h_f": temp_delta_3h_f,
            "rh_delta_1h": rh_delta_1h,
            "rh_delta_3h": rh_delta_3h,
        }

        feedback_summary = self.get_weather_feedback_summary(current_condition=condition_label)
        learned_model = self.get_learned_weather_model()
        lightning_telemetry = self.get_lightning_telemetry()

        nowcast_trajectory = ClimatologyEngine.compute_short_term_nowcast(
            outdoor=outdoor_dict,
            baro=baro,
            extremes_today={
                "temp_low_f": outdoor_dict["today_low_f"],
                "temp_high_f": outdoor_dict["today_high_f"],
            },
            learned_model=learned_model,
            feedback_summary=feedback_summary,
        )

        return {
            "generated_at": format_utc_z(now_utc),
            "station_name": self.get_setting("station_name", DEFAULT_SYSTEM_SETTINGS["station_name"]),
            "city_state_label": self.get_setting("city_state_label", ""),
            "coordinates": coords,
            "units": {
                "temp_unit": self.get_setting("temp_unit", "F"),
                "wind_unit": self.get_setting("wind_unit", "mph"),
                "pressure_unit": self.get_setting("pressure_unit", "inHg"),
                "rain_unit": self.get_setting("rain_unit", "in"),
            },
            "condition": {
                "code": condition_code,
                "label": condition_label,
                "is_night": is_night,
                "nowcast_summary": nowcast_summary,
                "trend_badges": trend_badges,
                "trajectory": nowcast_trajectory,
                "confidence_pct": nowcast_trajectory.get("confidence_pct", 85),
                "precip_prob_pct": nowcast_trajectory.get("precip_probability_pct", 10),
            },
            "nowcast": nowcast_trajectory,
            "reaffirmation": feedback_summary,
            "lightning": lightning_telemetry,
            "active_alerts": self.get_active_weather_alerts(),
            "barometer": baro,
            "outdoor": outdoor_dict,
            "indoor": {
                "sensor_count": len(indoor_sensors),
                "temp_f": in_temp_f,
                "temp_c": in_temp_c,
                "humidity": in_humidity,
                "today_low_f": today_in_low_f,
                "today_low_ts": today_in_low_ts,
                "today_high_f": today_in_high_f,
                "today_high_ts": today_in_high_ts,
                "trend_arrow": in_arrow,
                "trend_word": in_trend_word,
                "temp_delta_1h_f": in_temp_delta_1h_f,
                "sparkline_24h_f": sparkline_in_24h_f,
            },
            "rain_records": {
                "today_in": rain_today_in,
                "rate_in_h": rain_rate_in_h,
                "week_in": rain_week_in,
                "month_in": rain_month_in,
                "year_in": rain_year_in,
            },
        }

    def get_db_stats(self) -> dict[str, Any]:
        """Return total reading count, database file size in MB, active sensor count, and timestamps."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")

            row_readings = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_readings,
                    MIN(timestamp) AS earliest_timestamp,
                    MAX(timestamp) AS latest_timestamp
                FROM sensor_readings;
                """
            ).fetchone()

            row_sensors = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_sensors,
                    SUM(CASE WHEN location_type IN ('outdoor', 'indoor') AND is_trusted = 1 THEN 1 ELSE 0 END) AS active_sensors
                FROM sensors;
                """
            ).fetchone()

        total_bytes = 0
        if self.db_path.name != ":memory:":
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{self.db_path}{suffix}")
                if candidate.is_file():
                    try:
                        total_bytes += candidate.stat().st_size
                    except OSError:
                        pass

        total_readings = int(row_readings["total_readings"] or 0) if row_readings else 0
        total_sensors = int(row_sensors["total_sensors"] or 0) if row_sensors else 0
        active_sensors = int(row_sensors["active_sensors"] or 0) if row_sensors else 0

        earliest_dt = parse_utc_iso(row_readings["earliest_timestamp"]) if row_readings else None
        latest_dt = parse_utc_iso(row_readings["latest_timestamp"]) if row_readings else None

        return {
            "database_path": str(self.db_path),
            "database_size_bytes": total_bytes,
            "database_size_mb": round(total_bytes / (1024.0 * 1024.0), 3),
            "total_readings": total_readings,
            "total_sensors": total_sensors,
            "active_sensors": active_sensors,
            "earliest_timestamp": format_utc_z(earliest_dt) if earliest_dt else None,
            "latest_timestamp": format_utc_z(latest_dt) if latest_dt else None,
        }

    def count_readings(self) -> int:
        """Return the total number of stored weather readings."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            cursor = self._conn.execute("SELECT COUNT(*) FROM sensor_readings;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def checkpoint_wal(self) -> None:
        """Flush and truncate the SQLite Write-Ahead Log (`PRAGMA wal_checkpoint(TRUNCATE)`)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        """Flush WAL checkpoints and close the underlying SQLite connection gracefully."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except sqlite3.Error:
                    pass
                try:
                    self._conn.close()
                    logger.debug("Closed SQLite database connection (%s)", self.db_path)
                finally:
                    self._conn = None

    def __enter__(self) -> WeatherDatabase:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

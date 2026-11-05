"""SQLite WAL database engine, outlier expunger, barometric forecaster, and charting for SkyScry (v0.0.8)."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

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
    {"temperature", "humidity", "wind", "rain", "uv_light", "pressure"}
)

HPA_TO_INHG = 0.029529983071445

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    return row_dict


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
        self._configure_connection()
        self._initialize_schema()
        logger.debug("Initialized SQLite WAL database at %s", self.db_path)

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

        # Run startup TPMS, barometric outlier, & appliance auto-purge
        self.expunge_skewed_history()

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
                        max_allowed_dev = 15.0 if (pkt_cnt <= 2 and low_snr) else 22.0
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
                        uv_index, lux, rssi, snr, noise, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    reading_params,
                )
                return int(cursor.lastrowid or 0)

    def get_location_coordinates(
        self,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
    ) -> dict[str, float]:
        """Return configured station coordinates from `system_settings` or neutral `0.0, 0.0` defaults."""
        with self._lock:
            if self._conn is None:
                return {"latitude": default_lat, "longitude": default_lon}
            rows = self._conn.execute(
                "SELECT key, value FROM system_settings WHERE key IN ('latitude', 'longitude');"
            ).fetchall()
            kv = {r["key"]: r["value"] for r in rows}
            try:
                lat = float(kv.get("latitude", default_lat))
                lon = float(kv.get("longitude", default_lon))
            except ValueError:
                lat, lon = default_lat, default_lon
            return {"latitude": lat, "longitude": lon}

    def set_location_coordinates(self, latitude: float, longitude: float) -> dict[str, float]:
        """Persist station coordinates in `system_settings`."""
        lat = max(-90.0, min(90.0, float(latitude)))
        lon = max(-180.0, min(180.0, float(longitude)))
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            with self._conn:
                self._conn.execute(
                    "INSERT INTO system_settings (key, value) VALUES ('latitude', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                    (f"{lat:.4f}",),
                )
                self._conn.execute(
                    "INSERT INTO system_settings (key, value) VALUES ('longitude', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                    (f"{lon:.4f}",),
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
        """Return time-series readings filtered by optional `sensor_key` and UTC time window."""
        safe_hours = max(0.1, min(float(hours), 24.0 * 365.0))
        safe_limit = max(1, min(int(limit), 5000))
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

    def _compute_barometric_telemetry_and_forecast(
        self,
        rf_press_hpa: Optional[float],
        dew_spread_f: Optional[float],
        wind_cardinal: Optional[str],
        rain_30m_mm: float,
    ) -> dict[str, Any]:
        """Resolve Barometric Pressure (`inHg` & `hPa`), 3-hour tendency, and 24-hour AcuRite-style forecast."""
        now_utc = datetime.now(timezone.utc)
        if rf_press_hpa is not None and not (MIN_PLAUSIBLE_PRESSURE_HPA <= rf_press_hpa <= MAX_PLAUSIBLE_PRESSURE_HPA):
            rf_press_hpa = None

        source = "rf_sensor" if rf_press_hpa is not None else "regional_reference"
        current_hpa = rf_press_hpa
        past_3h_hpa: Optional[float] = None

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

        if current_hpa is None:
            return {
                "pressure_hpa": None,
                "pressure_inhg": None,
                "delta_3h_hpa": None,
                "delta_3h_inhg": None,
                "trend_label": "Awaiting Sync",
                "source": "none",
                "forecast_24h": "Syncing regional barometric pressure history...",
            }

        current_inhg = round(current_hpa * HPA_TO_INHG, 2)
        delta_3h_hpa = round(current_hpa - past_3h_hpa, 1) if past_3h_hpa is not None else None
        delta_3h_inhg = (
            round((current_hpa - past_3h_hpa) * HPA_TO_INHG, 2) if past_3h_hpa is not None else None
        )

        if delta_3h_inhg is None:
            trend_label = "Steady"
        elif delta_3h_inhg >= 0.06:
            trend_label = f"Rising Rapidly ({delta_3h_inhg:+.2f} inHg/3h)"
        elif delta_3h_inhg >= 0.02:
            trend_label = f"Rising ({delta_3h_inhg:+.2f} inHg/3h)"
        elif delta_3h_inhg <= -0.06:
            trend_label = f"Falling Rapidly ({delta_3h_inhg:+.2f} inHg/3h)"
        elif delta_3h_inhg <= -0.02:
            trend_label = f"Falling ({delta_3h_inhg:+.2f} inHg/3h)"
        else:
            trend_label = f"Steady ({delta_3h_inhg:+.2f} inHg/3h)"

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
            "trend_label": trend_label,
            "source": source,
            "forecast_24h": forecast_24h,
        }

    def get_weather_overview(self) -> dict[str, Any]:
        """Synthesize current outdoor/indoor conditions, barometer (`inHg`), compass wind, and nowcast."""
        now_utc = datetime.now(timezone.utc)
        sensors = self.get_all_sensors()
        coords = self.get_location_coordinates()

        fresh_cutoff = now_utc - timedelta(minutes=60)
        outdoor_sensors: list[dict[str, Any]] = []
        indoor_sensors: list[dict[str, Any]] = []

        # Strict Opt-In Trust Model: Only explicitly trusted 'outdoor' or 'indoor' stations participate
        for s in sensors:
            if not s["is_trusted"] or s["location_type"] == "ignore":
                continue
            last_dt = parse_utc_iso(s["last_seen"])
            if last_dt is None or last_dt < fresh_cutoff:
                continue
            if s["location_type"] == "indoor":
                indoor_sensors.append(s)
            elif s["location_type"] == "outdoor":
                outdoor_sensors.append(s)

        out_temps_c: list[float] = []
        out_rh: list[float] = []
        out_press: list[float] = []
        out_wind_mps: list[float] = []
        out_gust_mps: list[float] = []
        out_wind_dirs: list[float] = []
        out_uv: list[float] = []
        out_lux: list[float] = []

        for s in outdoor_sensors:
            r = s.get("latest_reading")
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
            r = s.get("latest_reading")
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
        temp_delta_1h_f: Optional[float] = None
        temp_delta_3h_f: Optional[float] = None
        rh_delta_1h: Optional[float] = None
        wind_dir_1h_ago: Optional[str] = None

        outdoor_keys = [
            s["sensor_key"]
            for s in outdoor_sensors
            if "temperature" not in set(s.get("hidden_metrics") or [])
        ]
        rain_keys = [
            s["sensor_key"]
            for s in outdoor_sensors
            if "rain" not in set(s.get("hidden_metrics") or [])
        ]

        with self._lock:
            if self._conn is not None:
                if rain_keys:
                    placeholders = ",".join("?" for _ in rain_keys)
                    cutoff_24h = format_utc_z(now_utc - timedelta(hours=24))
                    cutoff_30m = format_utc_z(now_utc - timedelta(minutes=30))

                    rain_rows_24h = self._conn.execute(
                        f"""
                        SELECT COALESCE(sensor_key, model || '_' || sensor_id) AS skey,
                               MAX(rain_cumulative_mm) - MIN(rain_cumulative_mm) AS delta_mm
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND rain_cumulative_mm IS NOT NULL
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({placeholders})
                        GROUP BY skey;
                        """,
                        [cutoff_24h, *rain_keys],
                    ).fetchall()
                    deltas_24h = [
                        float(r["delta_mm"] or 0.0)
                        for r in rain_rows_24h
                        if (r["delta_mm"] or 0.0) >= 0.0
                    ]
                    if deltas_24h:
                        rain_24h_mm = max(deltas_24h)

                    rain_rows_30m = self._conn.execute(
                        f"""
                        SELECT COALESCE(sensor_key, model || '_' || sensor_id) AS skey,
                               MAX(rain_cumulative_mm) - MIN(rain_cumulative_mm) AS delta_mm
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND rain_cumulative_mm IS NOT NULL
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({placeholders})
                        GROUP BY skey;
                        """,
                        [cutoff_30m, *rain_keys],
                    ).fetchall()
                    deltas_30m = [
                        float(r["delta_mm"] or 0.0)
                        for r in rain_rows_30m
                        if (r["delta_mm"] or 0.0) >= 0.0
                    ]
                    if deltas_30m:
                        rain_30m_mm = max(deltas_30m)

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
                        SELECT AVG(temp_c) AS avg_tc
                        FROM sensor_readings
                        WHERE timestamp BETWEEN ? AND ?
                          AND COALESCE(sensor_key, model || '_' || sensor_id) IN ({placeholders});
                        """,
                        [t_3h_start, t_3h_end, *outdoor_keys],
                    ).fetchone()
                    if row_3h and row_3h["avg_tc"] is not None:
                        past_3h_tf = (float(row_3h["avg_tc"]) * 9.0 / 5.0) + 32.0
                        temp_delta_3h_f = round(mean_temp_f - past_3h_tf, 1)

                    if temp_delta_3h_f is None:
                        ref_3h = self._conn.execute(
                            """
                            SELECT AVG(temp_c) AS avg_tc FROM reference_weather_history
                            WHERE timestamp BETWEEN ? AND ? AND temp_c IS NOT NULL;
                            """,
                            (t_3h_start, t_3h_end),
                        ).fetchone()
                        if ref_3h and ref_3h["avg_tc"] is not None:
                            past_3h_tf = (float(ref_3h["avg_tc"]) * 9.0 / 5.0) + 32.0
                            temp_delta_3h_f = round(mean_temp_f - past_3h_tf, 1)

        rain_24h_in = round(rain_24h_mm / 25.4, 2)
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

        local_hour = datetime.now().hour
        is_night = (
            (lux_val is not None and lux_val <= 15.0)
            or (lux_val is None and (local_hour >= 19 or local_hour < 6))
        )

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
            and mean_rh >= 92.0
            and dew_spread_f is not None
            and dew_spread_f <= 3.0
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

        if baro.get("pressure_inhg") is not None:
            b_tone = "neutral"
            d_inhg = baro.get("delta_3h_inhg") or 0.0
            if d_inhg <= -0.03:
                b_tone = "cool"
            elif d_inhg >= 0.03:
                b_tone = "info"
            trend_badges.append(
                {
                    "label": f"Baro {baro['pressure_inhg']:.2f} inHg ({baro['trend_label'].split(' ')[0]})",
                    "tone": b_tone,
                }
            )

        if temp_delta_1h_f is not None:
            if temp_delta_1h_f <= -1.5:
                trend_badges.append(
                    {"label": f"Falling {temp_delta_1h_f:+.1f}F/hr", "tone": "cool"}
                )
                nowcast_clauses.append(f"Rapid cooling ({temp_delta_1h_f:+.1f}F over the past hour)")
            elif temp_delta_1h_f >= 1.5:
                trend_badges.append(
                    {"label": f"Rising {temp_delta_1h_f:+.1f}F/hr", "tone": "warm"}
                )
                nowcast_clauses.append(f"Warming trend ({temp_delta_1h_f:+.1f}F over the past hour)")
            else:
                trend_badges.append(
                    {"label": f"Temp Steady ({temp_delta_1h_f:+.1f}F/hr)", "tone": "neutral"}
                )

        if rh_delta_1h is not None:
            if rh_delta_1h >= 4.0:
                trend_badges.append({"label": f"Humidity Rising {rh_delta_1h:+.0f}%/hr", "tone": "info"})
            elif rh_delta_1h <= -4.0:
                trend_badges.append({"label": f"Humidity Falling {rh_delta_1h:+.0f}%/hr", "tone": "neutral"})

        if wind_dir_1h_ago and wind_cardinal and wind_dir_1h_ago != wind_cardinal:
            trend_badges.append(
                {"label": f"Wind Shift {wind_dir_1h_ago} -> {wind_cardinal}", "tone": "warn"}
            )

        if dew_spread_f is not None:
            trend_badges.append(
                {"label": f"Dew Spread {dew_spread_f:.1f}F", "tone": "info" if dew_spread_f <= 4.0 else "neutral"}
            )

        baro_forecast = baro.get("forecast_24h") or ""
        if rain_30m_mm > 0.0:
            nowcast_summary = (
                f"Active precipitation detected ({rain_30m_mm / 25.4:.2f} in over last 30 min). "
                f"{baro_forecast}"
            )
        elif dew_spread_f is not None and dew_spread_f <= 3.5 and (mean_rh or 0) >= 85.0:
            nowcast_summary = (
                f"Air is nearing saturation (Dew point spread narrowed to {dew_spread_f:.1f}F at {mean_rh:.0f}% RH) "
                f"— high likelihood of surface condensation or mist. {baro_forecast}"
            )
        elif nowcast_clauses:
            nowcast_summary = f"{'; '.join(nowcast_clauses)}. {baro_forecast}"
        elif mean_temp_f is not None:
            nowcast_summary = (
                f"Steady microclimate across {len(outdoor_sensors)} active outdoor station(s) "
                f"(Dew spread {dew_spread_f if dew_spread_f is not None else '--'}F). {baro_forecast}"
            )
        else:
            nowcast_summary = (
                "No Outdoor trusted sensors configured yet. Open '3. Sensors Manager' and set your station(s) "
                f"to Outdoor (Trusted) to synthesize local microclimate telemetry. {baro_forecast}"
            )

        return {
            "generated_at": format_utc_z(now_utc),
            "coordinates": coords,
            "condition": {
                "code": condition_code,
                "label": condition_label,
                "is_night": is_night,
                "nowcast_summary": nowcast_summary,
                "trend_badges": trend_badges,
            },
            "barometer": baro,
            "outdoor": {
                "sensor_count": len(outdoor_sensors),
                "temp_f": mean_temp_f,
                "temp_c": mean_temp_c,
                "temp_min_f": min_temp_f,
                "temp_max_f": max_temp_f,
                "humidity": mean_rh,
                "dew_point_f": dp_f,
                "dew_point_c": dp_c,
                "dew_spread_f": dew_spread_f,
                "feels_like_f": feels_like_f,
                "feels_like_c": feels_like_c,
                "pressure_hpa": baro.get("pressure_hpa"),
                "pressure_inhg": baro.get("pressure_inhg"),
                "wind_avg_mph": wind_avg_mph,
                "wind_avg_mps": wind_avg_mps,
                "wind_gust_mph": wind_gust_mph,
                "wind_gust_mps": wind_gust_mps,
                "wind_dir_deg": wind_dir_deg,
                "wind_cardinal": wind_cardinal,
                "rain_24h_in": rain_24h_in,
                "rain_24h_mm": round(rain_24h_mm, 1),
                "uv_index": uv_val,
                "lux": lux_val,
                "temp_delta_1h_f": temp_delta_1h_f,
                "temp_delta_3h_f": temp_delta_3h_f,
                "rh_delta_1h": rh_delta_1h,
            },
            "indoor": {
                "sensor_count": len(indoor_sensors),
                "temp_f": in_temp_f,
                "temp_c": in_temp_c,
                "humidity": in_humidity,
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

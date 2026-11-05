"""SQLite WAL database engine, weather ensemble modeling, and time-bucketed charting for SkyScry."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
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

logger = logging.getLogger(__name__)

VALID_LOCATION_TYPES: frozenset[str] = frozenset({"indoor", "outdoor", "ignore", "unassigned"})
VALID_HIDDEN_METRICS: frozenset[str] = frozenset(
    {"temperature", "humidity", "wind", "rain", "uv_light", "pressure"}
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sensors (
    sensor_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    sensor_id TEXT NOT NULL,
    channel TEXT,
    alias TEXT,
    location_type TEXT DEFAULT 'unassigned',
    is_trusted INTEGER DEFAULT 1,
    hidden_metrics TEXT DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    battery_ok INTEGER,
    last_rssi REAL,
    last_snr REAL,
    last_noise REAL,
    packet_count INTEGER DEFAULT 0,
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
    """Attach computed imperial, dew point, compass, and RF quality fields to a reading dict."""
    temp_c = row_dict.get("temp_c")
    humidity = row_dict.get("humidity")
    wind_avg_mps = row_dict.get("wind_avg_mps")
    wind_gust_mps = row_dict.get("wind_gust_mps")
    wind_dir_deg = row_dict.get("wind_dir_deg")
    rain_mm = row_dict.get("rain_cumulative_mm")
    rain_rate_mm = row_dict.get("rain_rate_mm_h")
    battery_raw = row_dict.get("battery_ok")
    snr = row_dict.get("snr")
    rssi = row_dict.get("rssi")

    # Normalize timestamp string to strict Z suffix for unambiguous browser parsing
    parsed_ts = parse_utc_iso(row_dict.get("timestamp"))
    if parsed_ts is not None:
        row_dict["timestamp"] = format_utc_z(parsed_ts)

    temp_f = round((float(temp_c) * 9.0 / 5.0) + 32.0, 2) if temp_c is not None else None
    dp_c = compute_dew_point_c(float(temp_c) if temp_c is not None else None, float(humidity) if humidity is not None else None)
    dp_f = round((dp_c * 9.0 / 5.0) + 32.0, 2) if dp_c is not None else None

    row_dict["temp_f"] = temp_f
    row_dict["dew_point_c"] = dp_c
    row_dict["dew_point_f"] = dp_f
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
    """Thread-safe SQLite WAL database manager with schema migrations and weather synthesis."""

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

    def _initialize_schema(self) -> None:
        """Create tables, run non-destructive ALTER TABLE migrations, and repair any local-time timestamps."""
        with self._lock:
            if self._conn is None:
                return
            with self._conn:
                self._conn.executescript(SCHEMA_SQL)

                # Safe column migrations for existing databases
                sensor_migrations = [
                    "ALTER TABLE sensors ADD COLUMN hidden_metrics TEXT DEFAULT '[]'",
                    "ALTER TABLE sensors ADD COLUMN last_snr REAL",
                    "ALTER TABLE sensors ADD COLUMN last_noise REAL",
                    "ALTER TABLE sensors ADD COLUMN last_rssi REAL",
                    "ALTER TABLE sensors ADD COLUMN packet_count INTEGER DEFAULT 0",
                    "ALTER TABLE sensors ADD COLUMN battery_changed_at TEXT",
                    "ALTER TABLE sensors ADD COLUMN battery_low_at TEXT",
                    "ALTER TABLE sensors ADD COLUMN estimated_battery_days INTEGER",
                ]
                for stmt in sensor_migrations:
                    try:
                        self._conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass

                reading_migrations = [
                    "ALTER TABLE sensor_readings ADD COLUMN snr REAL",
                    "ALTER TABLE sensor_readings ADD COLUMN noise REAL",
                    "ALTER TABLE sensor_readings ADD COLUMN rssi REAL",
                ]
                for stmt in reading_migrations:
                    try:
                        self._conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass

                # Backfill `sensors` from `sensor_readings` if needed
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO sensors (
                        sensor_key, model, sensor_id, channel, alias,
                        location_type, is_trusted, hidden_metrics, first_seen, last_seen,
                        battery_ok, last_rssi, last_snr, last_noise, packet_count
                    )
                    SELECT
                        model || '_' || sensor_id AS sensor_key,
                        model,
                        sensor_id,
                        MAX(channel),
                        NULL,
                        'unassigned',
                        1,
                        '[]',
                        MIN(timestamp),
                        MAX(timestamp),
                        (SELECT sr2.battery_ok FROM sensor_readings sr2
                         WHERE sr2.model = sr.model AND sr2.sensor_id = sr.sensor_id
                         ORDER BY sr2.timestamp DESC, sr2.id DESC LIMIT 1),
                        (SELECT sr2.rssi FROM sensor_readings sr2
                         WHERE sr2.model = sr.model AND sr2.sensor_id = sr.sensor_id
                         ORDER BY sr2.timestamp DESC, sr2.id DESC LIMIT 1),
                        (SELECT sr2.snr FROM sensor_readings sr2
                         WHERE sr2.model = sr.model AND sr2.sensor_id = sr.sensor_id
                         ORDER BY sr2.timestamp DESC, sr2.id DESC LIMIT 1),
                        (SELECT sr2.noise FROM sensor_readings sr2
                         WHERE sr2.model = sr.model AND sr2.sensor_id = sr.sensor_id
                         ORDER BY sr2.timestamp DESC, sr2.id DESC LIMIT 1),
                        COUNT(*)
                    FROM sensor_readings sr
                    GROUP BY model, sensor_id;
                    """
                )

                # Ensure packet_count and battery_changed_at are populated for existing sensors
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET packet_count = (
                        SELECT COUNT(*) FROM sensor_readings sr
                        WHERE sr.model = sensors.model AND sr.sensor_id = sensors.sensor_id
                    )
                    WHERE packet_count IS NULL OR packet_count = 0;
                    """
                )
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET battery_changed_at = first_seen
                    WHERE battery_changed_at IS NULL AND battery_ok = 1;
                    """
                )

                self._repair_shifted_timestamps()

    def _repair_shifted_timestamps(self) -> None:
        """Repair historical rows where naive local `rtl_433` time was stored with `+00:00` prior to `-M time:utc`."""
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
            # If the row was recorded before `-M time:utc`, its naive local time was tagged +00:00,
            # making a freshly recorded packet appear ~5-8 hours in the past (or future).
            # Convert its wall-clock fields from local timezone to true UTC 'Z'.
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
            self._conn.execute(
                """
                UPDATE sensors
                SET
                    first_seen = COALESCE(
                        (SELECT MIN(sr.timestamp) FROM sensor_readings sr
                         WHERE sr.model = sensors.model AND sr.sensor_id = sensors.sensor_id),
                        sensors.first_seen
                    ),
                    last_seen = COALESCE(
                        (SELECT MAX(sr.timestamp) FROM sensor_readings sr
                         WHERE sr.model = sensors.model AND sr.sensor_id = sensors.sensor_id),
                        sensors.last_seen
                    ),
                    battery_changed_at = COALESCE(
                        (SELECT MIN(sr.timestamp) FROM sensor_readings sr
                         WHERE sr.model = sensors.model AND sr.sensor_id = sensors.sensor_id),
                        sensors.battery_changed_at
                    );
                """
            )
            logger.info("Normalized %d historical SQLite timestamps to strict UTC (Z).", len(updates))

    @staticmethod
    def make_sensor_key(model: str, sensor_id: str) -> str:
        """Format the canonical `{model}_{sensor_id}` primary key."""
        return f"{model}_{sensor_id}"

    def insert_reading(self, reading: NormalizedReading) -> int:
        """Persist a NormalizedReading and update device registry, RF metrics, and battery lifecycle."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Cannot insert into a closed WeatherDatabase connection.")

            utc_ts = format_utc_z(reading.timestamp)
            battery_int: Optional[int] = None
            if reading.battery_ok is not None:
                battery_int = 1 if reading.battery_ok else 0

            sensor_key = self.make_sensor_key(reading.model, reading.sensor_id)

            existing = self._conn.execute(
                """
                SELECT battery_ok, last_seen, first_seen, battery_changed_at, battery_low_at, estimated_battery_days, packet_count
                FROM sensors WHERE sensor_key = ?;
                """,
                (sensor_key,),
            ).fetchone()

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
                            last_rssi, last_snr, last_noise, packet_count,
                            battery_changed_at, battery_low_at, estimated_battery_days
                        ) VALUES (?, ?, ?, ?, NULL, 'unassigned', 1, '[]', ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL);
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
                    # Battery just transitioned from OK -> LOW
                    batt_low_at = utc_ts
                    start_ref = parse_utc_iso(batt_changed_at or existing["first_seen"])
                    now_dt = parse_utc_iso(utc_ts)
                    if start_ref and now_dt:
                        cycle_days = max(1, int((now_dt - start_ref).total_seconds() // 86400))
                        est_days = cycle_days
                elif battery_int == 1 and prev_batt is not None and int(prev_batt) == 0:
                    # Battery replaced: transitioned from LOW (0) -> OK (1)
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

            reading_params = (
                utc_ts,
                reading.sensor_id,
                reading.model,
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
                        timestamp, sensor_id, model, channel, battery_ok,
                        temp_c, humidity, pressure_hpa, wind_avg_mps, wind_gust_mps,
                        wind_dir_deg, rain_cumulative_mm, rain_rate_mm_h,
                        uv_index, lux, rssi, snr, noise, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    reading_params,
                )
                return int(cursor.lastrowid or 0)

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
        """Return all registered sensors sorted deterministically by display name ASC, never by last_seen."""
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
                    "location_type": raw.get("location_type") or "unassigned",
                    "is_trusted": bool(int(raw.get("is_trusted", 1)) != 0),
                    "hidden_metrics": hidden_metrics,
                    "first_seen": format_utc_z(first_seen_dt) if first_seen_dt else raw["first_seen"],
                    "last_seen": format_utc_z(last_seen_dt) if last_seen_dt else raw["last_seen"],
                    "battery_ok": battery_bool,
                    "battery_changed_at": format_utc_z(batt_changed_dt) if batt_changed_dt else None,
                    "battery_low_at": raw.get("battery_low_at"),
                    "days_on_current_battery": days_on_current_battery,
                    "estimated_battery_days": raw.get("estimated_battery_days"),
                    "packet_count": int(raw.get("packet_count") or 0),
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
    ) -> Optional[dict[str, Any]]:
        """Update user-defined metadata, hidden_metrics, or battery replacement timestamp for a sensor."""
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

            resolved_location: str = existing["location_type"] or "unassigned"
            if location_type is not None:
                loc_candidate = str(location_type).strip().lower()
                if loc_candidate not in VALID_LOCATION_TYPES:
                    raise ValueError(
                        f"Invalid location_type '{location_type}'. "
                        f"Must be one of: {', '.join(sorted(VALID_LOCATION_TYPES))}."
                    )
                resolved_location = loc_candidate

            resolved_trusted: int = int(existing["is_trusted"])
            if is_trusted is not None:
                resolved_trusted = 1 if bool(is_trusted) else 0

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
            where_clauses.append("(sr.model || '_' || sr.sensor_id) = ?")
            params.append(sensor_key.strip())

        params.append(safe_limit)
        where_sql = " AND ".join(where_clauses)

        query = f"""
        SELECT
            sr.id,
            sr.timestamp,
            sr.sensor_id,
            sr.model,
            (sr.model || '_' || sr.sensor_id) AS sensor_key,
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
        LEFT JOIN sensors s ON s.sensor_key = (sr.model || '_' || sr.sensor_id)
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
                bool(int(item["is_trusted"]) != 0) if item.get("is_trusted") is not None else True
            )
            item["hidden_metrics"] = _parse_hidden_metrics(item.get("hidden_metrics"))
            results.append(item)
        return results

    def get_bucketed_chart_series(
        self,
        hours: float = 24.0,
        bucket_minutes: Optional[int] = None,
        location_filter: str = "outdoor",
    ) -> dict[str, Any]:
        """Compute time-bucketed, smoothed chart series per sensor plus an Outdoor Average ensemble series."""
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

        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            rows = self._conn.execute(
                """
                SELECT
                    timestamp,
                    (model || '_' || sensor_id) AS sensor_key,
                    temp_c,
                    humidity,
                    wind_avg_mps,
                    wind_gust_mps,
                    rain_cumulative_mm
                FROM sensor_readings
                WHERE timestamp >= ?
                ORDER BY timestamp ASC;
                """,
                (cutoff_iso,),
            ).fetchall()

        # Filter active sensors based on location_filter
        loc_filter_norm = (location_filter or "all").strip().lower()
        eligible_sensors: dict[str, dict[str, Any]] = {}
        for skey, smeta in all_sensors.items():
            loc = smeta["location_type"]
            if loc == "ignore":
                continue
            if loc_filter_norm == "outdoor" and loc not in ("outdoor", "unassigned"):
                continue
            if loc_filter_norm == "indoor" and loc != "indoor":
                continue
            eligible_sensors[skey] = smeta

        # Group readings into epoch buckets: bucket_epoch -> sensor_key -> metric lists
        buckets: dict[int, dict[str, dict[str, list[float]]]] = {}
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
                s_bucket["temp_f"].append((float(row["temp_c"]) * 9.0 / 5.0) + 32.0)
            if "humidity" not in hidden and row["humidity"] is not None:
                s_bucket["humidity"].append(float(row["humidity"]))
            if "wind" not in hidden and row["wind_avg_mps"] is not None:
                s_bucket["wind_avg_mph"].append(float(row["wind_avg_mps"]) * 2.23693629)
            if "wind" not in hidden and row["wind_gust_mps"] is not None:
                s_bucket["wind_gust_mph"].append(float(row["wind_gust_mps"]) * 2.23693629)
            if "rain" not in hidden and row["rain_cumulative_mm"] is not None:
                s_bucket["rain_in"].append(float(row["rain_cumulative_mm"]) / 25.4)

        sorted_epochs = sorted(all_bucket_epochs)
        labels_iso = [
            format_utc_z(datetime.fromtimestamp(ep, tz=timezone.utc)) for ep in sorted_epochs
        ]

        # Build per-sensor series in deterministic sensor order
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

        # Compute Outdoor Ensemble Average line across trusted outdoor/unassigned sensors
        ensemble_temp_f: list[Optional[float]] = []
        ensemble_humidity: list[Optional[float]] = []

        for idx in range(len(sorted_epochs)):
            t_vals = [
                s["temp_f"][idx]
                for s in sensor_series
                if s["is_trusted"]
                and s["location_type"] in ("outdoor", "unassigned")
                and s["temp_f"][idx] is not None
            ]
            h_vals = [
                s["humidity"][idx]
                for s in sensor_series
                if s["is_trusted"]
                and s["location_type"] in ("outdoor", "unassigned")
                and s["humidity"][idx] is not None
            ]
            ensemble_temp_f.append(round(sum(t_vals) / len(t_vals), 2) if t_vals else None)
            ensemble_humidity.append(round(sum(h_vals) / len(h_vals), 1) if h_vals else None)

        return {
            "hours": safe_hours,
            "bucket_minutes": resolved_bucket_min,
            "location_filter": loc_filter_norm,
            "timestamps": labels_iso,
            "series": sensor_series,
            "ensemble": {
                "temp_f": ensemble_temp_f,
                "humidity": ensemble_humidity,
            },
        }

    def get_weather_overview(self) -> dict[str, Any]:
        """Synthesize current outdoor/indoor conditions, animated icon state, 1h/3h trends, and local nowcast."""
        now_utc = datetime.now(timezone.utc)
        sensors = self.get_all_sensors()

        # Active sensors seen within the last 60 minutes
        fresh_cutoff = now_utc - timedelta(minutes=60)

        outdoor_sensors: list[dict[str, Any]] = []
        indoor_sensors: list[dict[str, Any]] = []

        # If the user hasn't explicitly classified any sensor as 'outdoor' yet, treat 'unassigned' as outdoor
        has_explicit_outdoor = any(
            s["location_type"] == "outdoor" and s["is_trusted"] for s in sensors
        )

        for s in sensors:
            if not s["is_trusted"] or s["location_type"] == "ignore":
                continue
            last_dt = parse_utc_iso(s["last_seen"])
            if last_dt is None or last_dt < fresh_cutoff:
                continue
            if s["location_type"] == "indoor":
                indoor_sensors.append(s)
            elif s["location_type"] == "outdoor" or (
                not has_explicit_outdoor and s["location_type"] == "unassigned"
            ):
                outdoor_sensors.append(s)

        # Fallback: if no explicit outdoor sensors are fresh, include unassigned sensors alongside outdoor
        if not outdoor_sensors:
            for s in sensors:
                if s["is_trusted"] and s["location_type"] == "unassigned":
                    outdoor_sensors.append(s)

        # Aggregate Outdoor Metrics (strictly respecting each sensor's hidden_metrics)
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
                out_temps_c.append(float(r["temp_c"]))
            if "humidity" not in hidden and r.get("humidity") is not None:
                out_rh.append(float(r["humidity"]))
            if "pressure" not in hidden and r.get("pressure_hpa") is not None:
                out_press.append(float(r["pressure_hpa"]))
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
        mean_press = round(sum(out_press) / len(out_press), 1) if out_press else None

        dp_c = compute_dew_point_c(mean_temp_c, mean_rh)
        dp_f = round((dp_c * 9.0 / 5.0) + 32.0, 1) if dp_c is not None else None

        wind_avg_mps = round(sum(out_wind_mps) / len(out_wind_mps), 2) if out_wind_mps else None
        wind_avg_mph = round(wind_avg_mps * 2.23693629, 1) if wind_avg_mps is not None else None
        wind_gust_mps = round(max(out_gust_mps), 2) if out_gust_mps else None
        wind_gust_mph = round(wind_gust_mps * 2.23693629, 1) if wind_gust_mps is not None else None

        # Circular mean for wind direction
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

        # Compute 24h & 30m rainfall deltas + 1h & 3h temperature/humidity trends from SQLite
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
                        SELECT (model || '_' || sensor_id) AS skey,
                               MAX(rain_cumulative_mm) - MIN(rain_cumulative_mm) AS delta_mm
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND rain_cumulative_mm IS NOT NULL
                          AND (model || '_' || sensor_id) IN ({placeholders})
                        GROUP BY skey;
                        """,
                        [cutoff_24h, *rain_keys],
                    ).fetchall()
                    deltas_24h = [float(r["delta_mm"] or 0.0) for r in rain_rows_24h if (r["delta_mm"] or 0.0) >= 0.0]
                    if deltas_24h:
                        rain_24h_mm = max(deltas_24h)

                    rain_rows_30m = self._conn.execute(
                        f"""
                        SELECT (model || '_' || sensor_id) AS skey,
                               MAX(rain_cumulative_mm) - MIN(rain_cumulative_mm) AS delta_mm
                        FROM sensor_readings
                        WHERE timestamp >= ?
                          AND rain_cumulative_mm IS NOT NULL
                          AND (model || '_' || sensor_id) IN ({placeholders})
                        GROUP BY skey;
                        """,
                        [cutoff_30m, *rain_keys],
                    ).fetchall()
                    deltas_30m = [float(r["delta_mm"] or 0.0) for r in rain_rows_30m if (r["delta_mm"] or 0.0) >= 0.0]
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
                          AND (model || '_' || sensor_id) IN ({placeholders});
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
                          AND (model || '_' || sensor_id) IN ({placeholders});
                        """,
                        [t_3h_start, t_3h_end, *outdoor_keys],
                    ).fetchone()
                    if row_3h and row_3h["avg_tc"] is not None:
                        past_3h_tf = (float(row_3h["avg_tc"]) * 9.0 / 5.0) + 32.0
                        temp_delta_3h_f = round(mean_temp_f - past_3h_tf, 1)

        rain_24h_in = round(rain_24h_mm / 25.4, 2)
        dew_spread_f = (
            round(mean_temp_f - dp_f, 1)
            if (mean_temp_f is not None and dp_f is not None)
            else None
        )

        # Infer current condition + animated SVG icon key
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

        # Synthesize Trend Badges and Plain-English Nowcast
        trend_badges: list[dict[str, str]] = []
        nowcast_clauses: list[str] = []

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

        # Build plain-English Nowcast summary
        if rain_30m_mm > 0.0:
            nowcast_summary = (
                f"Active precipitation detected ({rain_30m_mm / 25.4:.2f} in over last 30 min). "
                "Expect wet surface conditions and elevated relative humidity."
            )
        elif dew_spread_f is not None and dew_spread_f <= 3.5 and (mean_rh or 0) >= 85.0:
            nowcast_summary = (
                f"Air is nearing saturation (Dew point spread narrowed to {dew_spread_f:.1f}F at {mean_rh:.0f}% RH) "
                "— high likelihood of surface condensation, mist, or localized fog formation."
            )
        elif (wind_gust_mph or 0) >= 16.0:
            nowcast_summary = (
                f"Gusty boundary winds ({wind_gust_mph:.1f} mph from {wind_cardinal or 'variable'}) "
                "indicate active atmospheric mixing or a passing pressure gradient."
            )
        elif nowcast_clauses:
            nowcast_summary = (
                f"{'; '.join(nowcast_clauses)}. Dew point spread is {dew_spread_f if dew_spread_f is not None else '--'}F "
                "with stable barometric/microclimate telemetry."
            )
        elif mean_temp_f is not None:
            nowcast_summary = (
                f"Steady microclimate conditions across {len(outdoor_sensors)} active outdoor station(s). "
                f"Dew point spread is {dew_spread_f if dew_spread_f is not None else '--'}F with no immediate precipitation signatures."
            )
        else:
            nowcast_summary = "Awaiting outdoor telemetry bursts to synthesize local microclimate nowcast."

        return {
            "generated_at": format_utc_z(now_utc),
            "condition": {
                "code": condition_code,
                "label": condition_label,
                "is_night": is_night,
                "nowcast_summary": nowcast_summary,
                "trend_badges": trend_badges,
            },
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
                "pressure_hpa": mean_press,
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
                    SUM(CASE WHEN location_type != 'ignore' THEN 1 ELSE 0 END) AS active_sensors
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

    def close(self) -> None:
        """Flush WAL checkpoints and close the underlying SQLite connection gracefully."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
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

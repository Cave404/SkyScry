"""SQLite WAL database engine for SkyScry time-series telemetry and device registry."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

from src.models import COMPASS_16_POINTS, NormalizedReading

logger = logging.getLogger(__name__)

VALID_LOCATION_TYPES: frozenset[str] = frozenset({"indoor", "outdoor", "ignore", "unassigned"})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sensors (
    sensor_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    sensor_id TEXT NOT NULL,
    channel TEXT,
    alias TEXT,
    location_type TEXT DEFAULT 'unassigned',
    is_trusted INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    battery_ok INTEGER,
    last_rssi REAL
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
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_time
    ON sensor_readings (sensor_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_model_sensor_time
    ON sensor_readings (model, sensor_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp
    ON sensor_readings (timestamp);
"""

UPSERT_SENSOR_SQL = """
INSERT INTO sensors (
    sensor_key,
    model,
    sensor_id,
    channel,
    alias,
    location_type,
    is_trusted,
    first_seen,
    last_seen,
    battery_ok,
    last_rssi
) VALUES (?, ?, ?, ?, NULL, 'unassigned', 1, ?, ?, ?, ?)
ON CONFLICT(sensor_key) DO UPDATE SET
    last_seen = excluded.last_seen,
    channel = COALESCE(excluded.channel, sensors.channel),
    battery_ok = COALESCE(excluded.battery_ok, sensors.battery_ok),
    last_rssi = COALESCE(excluded.last_rssi, sensors.last_rssi);
"""

INSERT_READING_SQL = """
INSERT INTO sensor_readings (
    timestamp,
    sensor_id,
    model,
    channel,
    battery_ok,
    temp_c,
    humidity,
    pressure_hpa,
    wind_avg_mps,
    wind_gust_mps,
    wind_dir_deg,
    rain_cumulative_mm,
    rain_rate_mm_h,
    uv_index,
    lux,
    rssi,
    raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def _deg_to_cardinal(deg: Optional[float]) -> Optional[str]:
    if deg is None:
        return None
    normalized_deg = float(deg) % 360.0
    index = int((normalized_deg + 11.25) // 22.5) % 16
    return COMPASS_16_POINTS[index]


def _enrich_reading_dict(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach computed imperial and compass fields to a serialized reading dictionary."""
    temp_c = row_dict.get("temp_c")
    wind_avg_mps = row_dict.get("wind_avg_mps")
    wind_gust_mps = row_dict.get("wind_gust_mps")
    wind_dir_deg = row_dict.get("wind_dir_deg")
    rain_mm = row_dict.get("rain_cumulative_mm")
    battery_raw = row_dict.get("battery_ok")

    row_dict["temp_f"] = round((float(temp_c) * 9.0 / 5.0) + 32.0, 2) if temp_c is not None else None
    row_dict["wind_avg_mph"] = (
        round(float(wind_avg_mps) * 2.23693629, 2) if wind_avg_mps is not None else None
    )
    row_dict["wind_gust_mph"] = (
        round(float(wind_gust_mps) * 2.23693629, 2) if wind_gust_mps is not None else None
    )
    row_dict["rain_cumulative_in"] = (
        round(float(rain_mm) / 25.4, 3) if rain_mm is not None else None
    )
    row_dict["wind_cardinal"] = _deg_to_cardinal(wind_dir_deg)
    if battery_raw is not None:
        row_dict["battery_ok"] = bool(int(battery_raw) != 0)
    return row_dict


class WeatherDatabase:
    """Thread-safe SQLite WAL database manager for time-series readings and device metadata."""

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
        """Create the sensors and sensor_readings tables and backfill any existing readings."""
        with self._lock:
            if self._conn is None:
                return
            with self._conn:
                self._conn.executescript(SCHEMA_SQL)
                # Backfill `sensors` from `sensor_readings` if upgrading from v0.1.0
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO sensors (
                        sensor_key, model, sensor_id, channel, alias,
                        location_type, is_trusted, first_seen, last_seen, battery_ok, last_rssi
                    )
                    SELECT
                        model || '_' || sensor_id AS sensor_key,
                        model,
                        sensor_id,
                        MAX(channel),
                        NULL,
                        'unassigned',
                        1,
                        MIN(timestamp),
                        MAX(timestamp),
                        (SELECT sr2.battery_ok FROM sensor_readings sr2
                         WHERE sr2.model = sr.model AND sr2.sensor_id = sr.sensor_id
                         ORDER BY sr2.timestamp DESC, sr2.id DESC LIMIT 1),
                        (SELECT sr2.rssi FROM sensor_readings sr2
                         WHERE sr2.model = sr.model AND sr2.sensor_id = sr.sensor_id
                         ORDER BY sr2.timestamp DESC, sr2.id DESC LIMIT 1)
                    FROM sensor_readings sr
                    GROUP BY model, sensor_id;
                    """
                )

    @staticmethod
    def make_sensor_key(model: str, sensor_id: str) -> str:
        """Format the canonical `{model}_{sensor_id}` primary key."""
        return f"{model}_{sensor_id}"

    def insert_reading(self, reading: NormalizedReading) -> int:
        """Persist a NormalizedReading and auto-upsert the device in the `sensors` registry."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Cannot insert into a closed WeatherDatabase connection.")

            utc_ts = reading.timestamp.astimezone(timezone.utc).isoformat()
            battery_int: Optional[int] = None
            if reading.battery_ok is not None:
                battery_int = 1 if reading.battery_ok else 0

            sensor_key = self.make_sensor_key(reading.model, reading.sensor_id)

            sensor_params = (
                sensor_key,
                reading.model,
                reading.sensor_id,
                reading.channel,
                utc_ts,
                utc_ts,
                battery_int,
                reading.rssi,
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
                reading.raw_json,
            )

            with self._conn:
                self._conn.execute(UPSERT_SENSOR_SQL, sensor_params)
                cursor = self._conn.execute(INSERT_READING_SQL, reading_params)
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
        """Return all registered sensors ordered by last_seen DESC with their latest reading attached."""
        query = """
        SELECT
            s.sensor_key,
            s.model,
            s.sensor_id,
            s.channel,
            s.alias,
            s.location_type,
            s.is_trusted,
            s.first_seen,
            s.last_seen,
            s.battery_ok,
            s.last_rssi,
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
            r.rssi
        FROM sensors s
        LEFT JOIN sensor_readings r ON r.id = (
            SELECT sr.id
            FROM sensor_readings sr
            WHERE sr.model = s.model AND sr.sensor_id = s.sensor_id
            ORDER BY sr.timestamp DESC, sr.id DESC
            LIMIT 1
        )
        ORDER BY s.last_seen DESC;
        """
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            cursor = self._conn.execute(query)
            rows = cursor.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            raw = dict(row)
            alias = str(raw["alias"]).strip() if raw.get("alias") else None
            display_name = alias if alias else f"{raw['model']} #{raw['sensor_id']}"
            battery_val = raw.get("battery_ok")
            battery_bool = bool(int(battery_val) != 0) if battery_val is not None else None

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
                        "rssi": raw["rssi"],
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
                    "first_seen": raw["first_seen"],
                    "last_seen": raw["last_seen"],
                    "battery_ok": battery_bool,
                    "last_rssi": raw["last_rssi"],
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
    ) -> Optional[dict[str, Any]]:
        """Update user-defined metadata (`alias`, `location_type`, `is_trusted`) for a sensor."""
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

            with self._conn:
                self._conn.execute(
                    """
                    UPDATE sensors
                    SET alias = ?, location_type = ?, is_trusted = ?
                    WHERE sensor_key = ?;
                    """,
                    (cleaned_alias, resolved_location, resolved_trusted, sensor_key),
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
        """Return time-series readings filtered by optional `sensor_key` and time window."""
        safe_hours = max(0.1, min(float(hours), 24.0 * 365.0))
        safe_limit = max(1, min(int(limit), 5000))
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=safe_hours)).isoformat()

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
            sr.rssi
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
            results.append(item)
        return results

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

        return {
            "database_path": str(self.db_path),
            "database_size_bytes": total_bytes,
            "database_size_mb": round(total_bytes / (1024.0 * 1024.0), 3),
            "total_readings": total_readings,
            "total_sensors": total_sensors,
            "active_sensors": active_sensors,
            "earliest_timestamp": row_readings["earliest_timestamp"] if row_readings else None,
            "latest_timestamp": row_readings["latest_timestamp"] if row_readings else None,
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

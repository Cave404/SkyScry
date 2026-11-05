"""SQLite WAL database engine for SkyScry weather telemetry persistence."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timezone
from pathlib import Path
from types import TracebackType
from typing import Optional

from src.models import NormalizedReading

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
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

CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp
    ON sensor_readings (timestamp);
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


class WeatherDatabase:
    """SQLite database manager configured with Write-Ahead Logging (WAL) for concurrent access."""

    def __init__(self, db_path: str | Path = "data/skyscry.db") -> None:
        self.db_path = Path(db_path)
        if self.db_path.name != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

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
        """Enable WAL journal mode and NORMAL synchronization for high-throughput concurrent I/O."""
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
        """Create the sensor_readings table and composite indexes if they do not exist."""
        if self._conn is None:
            return
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)

    def insert_reading(self, reading: NormalizedReading) -> int:
        """Persist a single NormalizedReading into sensor_readings and return the row ID."""
        if self._conn is None:
            raise RuntimeError("Cannot insert into a closed WeatherDatabase connection.")

        utc_ts = reading.timestamp.astimezone(timezone.utc).isoformat()
        battery_int: Optional[int] = None
        if reading.battery_ok is not None:
            battery_int = 1 if reading.battery_ok else 0

        params = (
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
            cursor = self._conn.execute(INSERT_READING_SQL, params)
            return int(cursor.lastrowid or 0)

    def count_readings(self) -> int:
        """Return the total number of stored weather readings."""
        if self._conn is None:
            raise RuntimeError("Database connection is closed.")
        cursor = self._conn.execute("SELECT COUNT(*) FROM sensor_readings;")
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Flush WAL checkpoints and close the underlying SQLite connection gracefully."""
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


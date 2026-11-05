"""Unified data model for normalized 433.92 MHz weather telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

COMPASS_16_POINTS: tuple[str, ...] = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


@dataclass(slots=True)
class NormalizedReading:
    """Standardized SI weather observation with computed imperial/compass properties."""

    # Metadata
    timestamp: datetime
    sensor_id: str
    model: str
    channel: Optional[str] = None
    battery_ok: Optional[bool] = None

    # Standardized SI Metrics
    temp_c: Optional[float] = None
    humidity: Optional[float] = None
    pressure_hpa: Optional[float] = None
    wind_avg_mps: Optional[float] = None
    wind_gust_mps: Optional[float] = None
    wind_dir_deg: Optional[float] = None
    rain_cumulative_mm: Optional[float] = None
    rain_rate_mm_h: Optional[float] = None
    uv_index: Optional[float] = None
    lux: Optional[float] = None
    rssi: Optional[float] = None
    raw_json: str = "{}"

    @property
    def temp_f(self) -> Optional[float]:
        """Temperature in degrees Fahrenheit."""
        if self.temp_c is None:
            return None
        return round((self.temp_c * 9.0 / 5.0) + 32.0, 2)

    @property
    def wind_avg_mph(self) -> Optional[float]:
        """Average wind speed in miles per hour (1 m/s = 2.23693629 mph)."""
        if self.wind_avg_mps is None:
            return None
        return round(self.wind_avg_mps * 2.23693629, 2)

    @property
    def wind_gust_mph(self) -> Optional[float]:
        """Wind gust speed in miles per hour (1 m/s = 2.23693629 mph)."""
        if self.wind_gust_mps is None:
            return None
        return round(self.wind_gust_mps * 2.23693629, 2)

    @property
    def rain_cumulative_in(self) -> Optional[float]:
        """Cumulative rainfall in inches (1 inch = 25.4 mm)."""
        if self.rain_cumulative_mm is None:
            return None
        return round(self.rain_cumulative_mm / 25.4, 3)

    @property
    def wind_cardinal(self) -> Optional[str]:
        """16-point compass cardinal direction (e.g., 'N', 'WSW', 'NNW') from wind_dir_deg."""
        if self.wind_dir_deg is None:
            return None
        normalized_deg = self.wind_dir_deg % 360.0
        index = int((normalized_deg + 11.25) // 22.5) % 16
        return COMPASS_16_POINTS[index]


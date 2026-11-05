"""Unified data model for normalized 433.92 MHz weather telemetry and RF signal quality."""

from __future__ import annotations

import math
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


def compute_signal_quality(snr: Optional[float], rssi: Optional[float]) -> str:
    """Classify 433.92 MHz RF signal quality from `rtl_433 -M level` SNR and RSSI (dB)."""
    if snr is not None:
        if snr >= 18.0:
            return "Excellent"
        if snr >= 11.0:
            return "Good"
        if snr >= 6.0:
            return "Fair"
        return "Weak"
    if rssi is not None:
        if rssi >= -10.0:
            return "Excellent"
        if rssi >= -20.0:
            return "Good"
        if rssi >= -30.0:
            return "Fair"
        return "Weak"
    return "Unknown"


def compute_signal_bars(snr: Optional[float], rssi: Optional[float]) -> int:
    """Return 0-4 signal strength bars based on SNR / RSSI."""
    rating = compute_signal_quality(snr, rssi)
    return {
        "Excellent": 4,
        "Good": 3,
        "Fair": 2,
        "Weak": 1,
    }.get(rating, 0)


def compute_dew_point_c(temp_c: Optional[float], humidity: Optional[float]) -> Optional[float]:
    """Calculate Dew Point in Celsius using the Magnus-Tetens approximation."""
    if temp_c is None or humidity is None or humidity <= 0.0:
        return None
    rh = max(1.0, min(100.0, float(humidity)))
    a = 17.625
    b = 243.04
    gamma = math.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    dp_c = (b * gamma) / (a - gamma)
    return round(dp_c, 2)


def compute_feels_like_f(
    temp_f: Optional[float],
    humidity: Optional[float],
    wind_mph: Optional[float],
) -> Optional[float]:
    """Calculate Apparent Temperature ('Feels Like' °F) using NWS Wind Chill & Rothfusz Heat Index."""
    if temp_f is None:
        return None
    t = float(temp_f)
    w = float(wind_mph) if wind_mph is not None else 0.0
    rh = float(humidity) if humidity is not None else 50.0

    # Wind Chill applies when Temp <= 50 F and Wind > 3 mph
    if t <= 50.0 and w > 3.0:
        wc = 35.74 + (0.6215 * t) - (35.75 * (w**0.16)) + (0.4275 * t * (w**0.16))
        return round(wc, 1)

    # Heat Index applies when Temp >= 80 F and RH >= 40%
    if t >= 80.0 and rh >= 40.0:
        hi = (
            -42.379
            + 2.04901523 * t
            + 10.14333127 * rh
            - 0.22475541 * t * rh
            - 0.00683783 * t * t
            - 0.05481717 * rh * rh
            + 0.00122874 * t * t * rh
            + 0.00085282 * t * rh * rh
            - 0.00000199 * t * t * rh * rh
        )
        return round(hi, 1)

    return round(t, 1)


@dataclass(slots=True)
class NormalizedReading:
    """Standardized SI weather observation with computed imperial/compass & RF properties."""

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

    # RF Signal Level Metrics (-M level)
    rssi: Optional[float] = None
    snr: Optional[float] = None
    noise: Optional[float] = None
    raw_json: str = "{}"

    @property
    def sensor_key(self) -> str:
        """Canonical composite key (`{model}_{sensor_id}`) for the device registry."""
        return f"{self.model}_{self.sensor_id}"

    @property
    def temp_f(self) -> Optional[float]:
        """Temperature in degrees Fahrenheit."""
        if self.temp_c is None:
            return None
        return round((self.temp_c * 9.0 / 5.0) + 32.0, 2)

    @property
    def pressure_inhg(self) -> Optional[float]:
        """Barometric pressure in inches of mercury (1 hPa = 0.029529983071445 inHg)."""
        if self.pressure_hpa is None:
            return None
        return round(self.pressure_hpa * 0.029529983071445, 2)

    @property
    def dew_point_c(self) -> Optional[float]:
        """Dew point in degrees Celsius."""
        return compute_dew_point_c(self.temp_c, self.humidity)

    @property
    def dew_point_f(self) -> Optional[float]:
        """Dew point in degrees Fahrenheit."""
        dp_c = self.dew_point_c
        if dp_c is None:
            return None
        return round((dp_c * 9.0 / 5.0) + 32.0, 2)

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

    @property
    def signal_quality(self) -> str:
        """Human-readable RF signal rating ('Excellent', 'Good', 'Fair', 'Weak', 'Unknown')."""
        return compute_signal_quality(self.snr, self.rssi)

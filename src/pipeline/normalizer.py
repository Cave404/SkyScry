"""Raw rtl_433 JSON parsing, SI unit normalization, and sliding-window burst deduplication."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.models import NormalizedReading

logger = logging.getLogger(__name__)

# Recognized meteorological keys in rtl_433 JSON output.
# Payloads lacking all of these (e.g., TPMS tire sensors, car keyfobs, security contacts) are ignored.
WEATHER_TELEMETRY_KEYS: frozenset[str] = frozenset(
    {
        "temperature_C",
        "temperature_F",
        "temp_C",
        "temp_F",
        "humidity",
        "pressure_hPa",
        "pressure_kPa",
        "pressure_inHg",
        "wind_avg_m_s",
        "wind_avg_km_h",
        "wind_avg_mi_h",
        "wind_speed_m_s",
        "wind_speed_km_h",
        "wind_speed_mi_h",
        "wind_max_m_s",
        "wind_max_km_h",
        "wind_max_mi_h",
        "wind_gust_m_s",
        "wind_gust_km_h",
        "wind_gust_mi_h",
        "wind_dir_deg",
        "rain_mm",
        "rain_in",
        "rain_rate_mm_h",
        "rain_rate_in_h",
        "uv",
        "uvi",
        "uv_index",
        "light_lux",
        "lux",
    }
)


def _to_float(value: Any) -> Optional[float]:
    """Safely convert a raw JSON field to float, returning None on invalid or missing values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class BurstDeduplicator:
    """Suppresses redundant 433.92 MHz RF burst frames within a sliding time window.

    433 MHz transmitters typically broadcast 3 identical frames per transmission burst
    within ~100-300ms. Keying on `(model, sensor_id, msg_type)` filters duplicates while
    allowing distinct message subtypes from multi-frame stations through immediately.
    """

    def __init__(
        self,
        window_seconds: float = 4.0,
        prune_interval_seconds: float = 300.0,
    ) -> None:
        self.window_seconds = max(0.0, float(window_seconds))
        self.prune_interval_seconds = max(10.0, float(prune_interval_seconds))
        self._last_seen: dict[tuple[str, str, str], float] = {}
        self._last_prune: float = time.monotonic()

    def is_duplicate(
        self,
        model: str,
        sensor_id: str,
        msg_type: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Return True if a frame with the same key was seen within `window_seconds`."""
        if self.window_seconds <= 0.0:
            return False

        current_time = time.monotonic() if now is None else float(now)

        if current_time - self._last_prune >= self.prune_interval_seconds:
            self.prune(current_time)

        key = (str(model), str(sensor_id), str(msg_type or ""))
        previous_time = self._last_seen.get(key)
        if previous_time is not None and (current_time - previous_time) < self.window_seconds:
            return True

        self._last_seen[key] = current_time
        return False

    def prune(self, now: Optional[float] = None) -> int:
        """Remove stale deduplication entries older than the active sliding window."""
        current_time = time.monotonic() if now is None else float(now)
        cutoff = current_time - max(self.window_seconds * 2.0, 60.0)
        stale_keys = [k for k, ts in self._last_seen.items() if ts < cutoff]
        for k in stale_keys:
            del self._last_seen[k]
        self._last_prune = current_time
        return len(stale_keys)


class PacketNormalizer:
    """Validates, deduplicates, and normalizes raw `rtl_433 -F json` lines into SI units."""

    def __init__(
        self,
        deduplicator: Optional[BurstDeduplicator] = None,
        dedup_window_seconds: float = 4.0,
    ) -> None:
        self.deduplicator = deduplicator or BurstDeduplicator(window_seconds=dedup_window_seconds)

    @staticmethod
    def has_weather_telemetry(payload: dict[str, Any]) -> bool:
        """Return True if the JSON payload contains at least one recognized weather metric."""
        return any(
            key in payload and payload[key] is not None for key in WEATHER_TELEMETRY_KEYS
        )

    @staticmethod
    def _extract_sensor_id(payload: dict[str, Any]) -> str:
        """Extract and cast the sensor ID to string with safe channel/unknown fallbacks."""
        for key in ("id", "sensor_id", "device", "station_id"):
            val = payload.get(key)
            if val is not None and str(val).strip() != "":
                return str(val).strip()
        channel = payload.get("channel")
        if channel is not None and str(channel).strip() != "":
            return f"ch-{str(channel).strip()}"
        return "unknown"

    @staticmethod
    def _extract_msg_type(payload: dict[str, Any]) -> str:
        """Extract optional subtype/message_type used to distinguish multi-part bursts."""
        for key in ("subtype", "msg_type", "message_type", "type"):
            val = payload.get(key)
            if val is not None and str(val).strip() != "":
                return str(val).strip()
        return ""

    @staticmethod
    def _parse_timestamp(raw_time: Any) -> datetime:
        """Parse rtl_433 timestamp string into a timezone-aware UTC datetime."""
        if isinstance(raw_time, str) and raw_time.strip():
            cleaned = raw_time.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(cleaned)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_battery(raw_battery: Any) -> Optional[bool]:
        """Normalize battery_ok (1 -> True, 0 -> False, None -> None)."""
        if raw_battery is None:
            return None
        if isinstance(raw_battery, bool):
            return raw_battery
        if isinstance(raw_battery, (int, float)):
            return bool(int(raw_battery) != 0)
        if isinstance(raw_battery, str):
            lowered = raw_battery.strip().lower()
            if lowered in ("1", "true", "ok", "good", "yes"):
                return True
            if lowered in ("0", "false", "low", "bad", "no"):
                return False
        return None

    @staticmethod
    def _normalize_temp_c(payload: dict[str, Any]) -> Optional[float]:
        """Extract temperature in Celsius, converting from Fahrenheit if needed."""
        temp_c = _to_float(payload.get("temperature_C", payload.get("temp_C")))
        if temp_c is not None:
            return round(temp_c, 2)

        temp_f = _to_float(payload.get("temperature_F", payload.get("temp_F")))
        if temp_f is not None:
            return round((temp_f - 32.0) * 5.0 / 9.0, 2)

        return None

    @staticmethod
    def _normalize_wind_avg_mps(payload: dict[str, Any]) -> Optional[float]:
        """Extract average wind speed in meters per second (m/s)."""
        mps = _to_float(payload.get("wind_avg_m_s", payload.get("wind_speed_m_s")))
        if mps is not None:
            return round(mps, 2)

        kmh = _to_float(payload.get("wind_avg_km_h", payload.get("wind_speed_km_h")))
        if kmh is not None:
            return round(kmh / 3.6, 2)

        mph = _to_float(payload.get("wind_avg_mi_h", payload.get("wind_speed_mi_h")))
        if mph is not None:
            return round(mph * 0.44704, 2)

        return None

    @staticmethod
    def _normalize_wind_gust_mps(payload: dict[str, Any]) -> Optional[float]:
        """Extract wind gust speed in meters per second (m/s)."""
        mps = _to_float(payload.get("wind_max_m_s", payload.get("wind_gust_m_s")))
        if mps is not None:
            return round(mps, 2)

        kmh = _to_float(payload.get("wind_max_km_h", payload.get("wind_gust_km_h")))
        if kmh is not None:
            return round(kmh / 3.6, 2)

        mph = _to_float(payload.get("wind_max_mi_h", payload.get("wind_gust_mi_h")))
        if mph is not None:
            return round(mph * 0.44704, 2)

        return None

    @staticmethod
    def _normalize_pressure_hpa(payload: dict[str, Any]) -> Optional[float]:
        """Extract barometric pressure in hectopascals (hPa)."""
        hpa = _to_float(payload.get("pressure_hPa"))
        if hpa is not None:
            return round(hpa, 2)

        kpa = _to_float(payload.get("pressure_kPa"))
        if kpa is not None:
            return round(kpa * 10.0, 2)

        inhg = _to_float(payload.get("pressure_inHg"))
        if inhg is not None:
            return round(inhg * 33.8638866667, 2)

        return None

    @staticmethod
    def _normalize_rain_mm(payload: dict[str, Any]) -> Optional[float]:
        """Extract cumulative rainfall in millimeters (mm)."""
        rain_mm = _to_float(payload.get("rain_mm"))
        if rain_mm is not None:
            return round(rain_mm, 2)

        rain_in = _to_float(payload.get("rain_in"))
        if rain_in is not None:
            return round(rain_in * 25.4, 2)

        return None

    @staticmethod
    def _normalize_rain_rate_mm_h(payload: dict[str, Any]) -> Optional[float]:
        """Extract rainfall rate in millimeters per hour (mm/h)."""
        rate_mm = _to_float(payload.get("rain_rate_mm_h"))
        if rate_mm is not None:
            return round(rate_mm, 2)

        rate_in = _to_float(payload.get("rain_rate_in_h"))
        if rate_in is not None:
            return round(rate_in * 25.4, 2)

        return None

    def process_line(self, raw_line: str) -> Optional[NormalizedReading]:
        """Parse a single line from rtl_433 stdout and return a NormalizedReading if valid & unique."""
        if not raw_line:
            return None
        stripped = raw_line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            return None

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("Ignored malformed JSON line: %s", stripped[:120])
            return None

        if not isinstance(payload, dict):
            return None

        model = str(payload.get("model") or "").strip()
        if not model:
            return None

        if not self.has_weather_telemetry(payload):
            logger.debug("Ignored non-weather RF payload from model=%s", model)
            return None

        sensor_id = self._extract_sensor_id(payload)
        msg_type = self._extract_msg_type(payload)

        if self.deduplicator.is_duplicate(model=model, sensor_id=sensor_id, msg_type=msg_type):
            logger.debug(
                "Suppressed duplicate burst frame for model=%s id=%s subtype=%s",
                model,
                sensor_id,
                msg_type or "default",
            )
            return None

        channel_raw = payload.get("channel")
        channel = str(channel_raw).strip() if channel_raw is not None and str(channel_raw).strip() != "" else None

        wind_dir = _to_float(payload.get("wind_dir_deg"))
        if wind_dir is not None:
            wind_dir = round(wind_dir % 360.0, 1)

        canonical_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        return NormalizedReading(
            timestamp=self._parse_timestamp(payload.get("time")),
            sensor_id=sensor_id,
            model=model,
            channel=channel,
            battery_ok=self._normalize_battery(payload.get("battery_ok")),
            temp_c=self._normalize_temp_c(payload),
            humidity=_to_float(payload.get("humidity")),
            pressure_hpa=self._normalize_pressure_hpa(payload),
            wind_avg_mps=self._normalize_wind_avg_mps(payload),
            wind_gust_mps=self._normalize_wind_gust_mps(payload),
            wind_dir_deg=wind_dir,
            rain_cumulative_mm=self._normalize_rain_mm(payload),
            rain_rate_mm_h=self._normalize_rain_rate_mm_h(payload),
            uv_index=_to_float(payload.get("uv", payload.get("uvi", payload.get("uv_index")))),
            lux=_to_float(payload.get("light_lux", payload.get("lux"))),
            rssi=_to_float(payload.get("rssi", payload.get("snr"))),
            raw_json=canonical_json,
        )


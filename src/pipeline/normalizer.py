"""Raw rtl_433 JSON parsing, SI unit normalization, TPMS/appliance filtering, and burst deduplication."""

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
# NOTE: `pressure_kPa` and `pressure_PSI` are intentionally excluded because rtl_433 TPMS tire sensors
# emit inflation pressure in kPa/PSI (~200-300 kPa), which is NOT atmospheric barometric pressure.
WEATHER_TELEMETRY_KEYS: frozenset[str] = frozenset(
    {
        "temperature_C",
        "temperature_F",
        "temp_C",
        "temp_F",
        "humidity",
        "pressure_hPa",
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
        "strike_count",
        "strikes_total",
        "strike_cnt",
        "storm_dist",
        "strike_distance",
        "storm_dist_km",
        "strike_distance_km",
    }
)

# Vehicle TPMS, soil moisture probes, and kitchen/fridge/meat-probe protocols that broadcast on 433.92 MHz.
# Any packet matching these prefixes is hard-dropped at the normalizer gate and purged from SQLite.
NON_WEATHER_MODEL_PREFIXES: tuple[str, ...] = (
    # Vehicle Tire Pressure Monitoring Systems (TPMS)
    "ford",
    "citroen",
    "renault",
    "toyota",
    "nissan",
    "schrader",
    "jansite",
    "pmv-107j",
    "abarth",
    "hyundai",
    "peugeot",
    "truck",
    "tpms",
    "kia",
    "subaru",
    "mazda",
    "chevrolet",
    "vdo",
    "pacific",
    # Soil moisture / irrigation probes
    "rainpoint",
    "ecowitt-wh51",
    # Kitchen / Fridge / Freezer / Meat / Pool thermometers
    "acurite-986",
    "acurite-00275rm",
    "acurite-515",
    "maverick",
    "thermopro",
    "inkbird",
    "govee",
    "oregon-sl109h",
)

# Backwards-compatible alias for db imports
APPLIANCE_PROBE_PREFIXES: tuple[str, ...] = NON_WEATHER_MODEL_PREFIXES

# Hard physical meteorological bounds for ambient surface weather stations
MIN_PLAUSIBLE_TEMP_F = -25.0
MAX_PLAUSIBLE_TEMP_F = 130.0
MIN_PLAUSIBLE_HUMIDITY = 1.0
MAX_PLAUSIBLE_HUMIDITY = 100.0
MAX_PLAUSIBLE_WIND_MPH = 130.0
MIN_PLAUSIBLE_PRESSURE_HPA = 870.0   # 25.69 inHg
MAX_PLAUSIBLE_PRESSURE_HPA = 1085.0  # 32.04 inHg


def is_appliance_model(model: str) -> bool:
    """Return True if the sensor model belongs to a vehicle TPMS, soil probe, or kitchen/fridge probe."""
    lower = (model or "").strip().lower()
    if not lower:
        return False
    return any(lower.startswith(prefix) for prefix in NON_WEATHER_MODEL_PREFIXES)


def is_tpms_or_non_weather_payload(payload: dict[str, Any]) -> bool:
    """Return True if a raw rtl_433 JSON payload is a vehicle TPMS, soil probe, or appliance packet."""
    model = str(payload.get("model") or "").strip()
    if is_appliance_model(model):
        return True

    pkt_type = str(payload.get("type") or "").strip().upper()
    if pkt_type == "TPMS":
        return True

    # Tire pressure keys emitted by rtl_433 TPMS decoders
    if any(
        k in payload
        for k in (
            "pressure_kPa",
            "pressure_PSI",
            "tire_pressure_psi",
            "wheel",
            "moisture",
        )
    ):
        return True

    # TPMS status flags paired with missing humidity/wind/rain
    if "flags" in payload and "humidity" not in payload and "wind_avg_m_s" not in payload and "rain_mm" not in payload:
        return True

    return False


def check_physical_bounds(reading: NormalizedReading) -> Optional[str]:
    """Return a rejection reason string if any metric falls outside physical meteorological bounds."""
    if reading.temp_f is not None:
        if reading.temp_f < MIN_PLAUSIBLE_TEMP_F or reading.temp_f > MAX_PLAUSIBLE_TEMP_F:
            return (
                f"temperature {reading.temp_f:.1f}F ({reading.temp_c:.1f}C) outside physical "
                f"ambient bounds [{MIN_PLAUSIBLE_TEMP_F:.0f}F, {MAX_PLAUSIBLE_TEMP_F:.0f}F]"
            )

    if reading.humidity is not None:
        if reading.humidity < MIN_PLAUSIBLE_HUMIDITY or reading.humidity > MAX_PLAUSIBLE_HUMIDITY:
            return (
                f"humidity {reading.humidity:.1f}% outside physical bounds "
                f"[{MIN_PLAUSIBLE_HUMIDITY:.0f}%, {MAX_PLAUSIBLE_HUMIDITY:.0f}%]"
            )

    if reading.pressure_hpa is not None:
        if reading.pressure_hpa < MIN_PLAUSIBLE_PRESSURE_HPA or reading.pressure_hpa > MAX_PLAUSIBLE_PRESSURE_HPA:
            return (
                f"pressure {reading.pressure_hpa:.1f} hPa outside physical barometric bounds "
                f"[{MIN_PLAUSIBLE_PRESSURE_HPA:.0f}, {MAX_PLAUSIBLE_PRESSURE_HPA:.0f} hPa]"
            )

    if reading.wind_avg_mph is not None and reading.wind_avg_mph > MAX_PLAUSIBLE_WIND_MPH:
        return f"wind_avg {reading.wind_avg_mph:.1f} mph exceeds physical limit ({MAX_PLAUSIBLE_WIND_MPH:.0f} mph)"

    if reading.wind_gust_mph is not None and reading.wind_gust_mph > MAX_PLAUSIBLE_WIND_MPH:
        return f"wind_gust {reading.wind_gust_mph:.1f} mph exceeds physical limit ({MAX_PLAUSIBLE_WIND_MPH:.0f} mph)"

    return None


def _to_float(value: Any) -> Optional[float]:
    """Safely convert a raw JSON field to float, returning None on invalid or missing values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class BurstDeduplicator:
    """Suppresses redundant 433.92 MHz RF burst frames within a sliding time window."""

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
        self._last_cadence_mono: dict[tuple[str, str], float] = {}

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
        """Parse rtl_433 timestamp string into a strict timezone-aware UTC datetime."""
        now_utc = datetime.now(timezone.utc)
        if isinstance(raw_time, str) and raw_time.strip():
            cleaned = raw_time.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(cleaned)
                if dt.tzinfo is not None:
                    dt_utc = dt.astimezone(timezone.utc)
                else:
                    candidate_utc = dt.replace(tzinfo=timezone.utc)
                    if abs((now_utc - candidate_utc).total_seconds()) <= 120.0:
                        dt_utc = candidate_utc
                    else:
                        dt_utc = dt.astimezone().astimezone(timezone.utc)

                if abs((now_utc - dt_utc).total_seconds()) <= 300.0:
                    return dt_utc.replace(microsecond=0)
            except ValueError:
                pass
        return now_utc.replace(microsecond=0)

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
        """Extract atmospheric barometric pressure in hectopascals (hPa), clamped to [870.0, 1085.0] hPa.

        Never maps `pressure_kPa` or `pressure_PSI` from vehicle TPMS tire sensors.
        """
        hpa = _to_float(payload.get("pressure_hPa"))
        if hpa is None:
            inhg = _to_float(payload.get("pressure_inHg"))
            if inhg is not None:
                hpa = inhg * 33.8638866667

        if hpa is None:
            return None

        if MIN_PLAUSIBLE_PRESSURE_HPA <= hpa <= MAX_PLAUSIBLE_PRESSURE_HPA:
            return round(hpa, 2)

        logger.debug(
            "Discarded out-of-range barometric pressure value %.2f hPa (valid: %.0f-%.0f hPa)",
            hpa,
            MIN_PLAUSIBLE_PRESSURE_HPA,
            MAX_PLAUSIBLE_PRESSURE_HPA,
        )
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

    @staticmethod
    def _normalize_strike_count(payload: dict[str, Any]) -> Optional[int]:
        """Extract cumulative lightning strike counter (AcuRite 06045M or compatible)."""
        for key in ("strike_count", "strikes_total", "strike_cnt"):
            val = _to_float(payload.get(key))
            if val is not None and val >= 0:
                return int(round(val))
        return None

    @staticmethod
    def _normalize_storm_dist_mi(payload: dict[str, Any]) -> Optional[float]:
        """Extract lightning storm distance in miles, clamped to valid [1.0, 25.0] miles when active.

        AcuRite 06045M sends `31` (0x1f) when no storm is active / out-of-range.
        """
        dist_mi: Optional[float] = None
        for key in ("storm_dist", "strike_distance", "storm_dist_mi"):
            val = _to_float(payload.get(key))
            if val is not None:
                dist_mi = val
                break

        if dist_mi is None:
            for key_km in ("storm_dist_km", "strike_distance_km"):
                val_km = _to_float(payload.get(key_km))
                if val_km is not None:
                    if int(round(val_km)) == 31 or val_km <= 0:
                        return None
                    dist_mi = val_km / 1.609344
                    break

        if dist_mi is None:
            return None

        # 31 (0x1f) or 0 is AcuRite 06045M sentinel for out-of-range / no active storm
        if int(round(dist_mi)) == 31 or dist_mi <= 0.0:
            return None

        return round(max(1.0, min(25.0, float(dist_mi))), 1)

    @staticmethod
    def _normalize_rfi_detected(payload: dict[str, Any], is_lightning_sensor: bool = False) -> Optional[int]:
        """Extract RFI interference indicator (0/1) for AcuRite 06045M and lightning sensors."""
        for key in ("rfi", "interference", "rfi_detected", "exception"):
            if key in payload and payload[key] is not None:
                raw_v = payload[key]
                if isinstance(raw_v, bool):
                    return 1 if raw_v else 0
                num_v = _to_float(raw_v)
                if num_v is not None:
                    return 1 if int(round(num_v)) != 0 else 0
                if isinstance(raw_v, str) and raw_v.strip().lower() in ("1", "true", "yes", "rfi", "interference"):
                    return 1
                return 0
        if is_lightning_sensor:
            return 0
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

        # Hard-drop vehicle TPMS, soil moisture, and appliance/kitchen probes at the normalizer gate
        if is_tpms_or_non_weather_payload(payload):
            logger.debug("Hard-dropped TPMS/soil/appliance RF packet from model=%s", model)
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
        channel = (
            str(channel_raw).strip()
            if channel_raw is not None and str(channel_raw).strip() != ""
            else None
        )

        wind_dir = _to_float(payload.get("wind_dir_deg"))
        if wind_dir is not None:
            wind_dir = round(wind_dir % 360.0, 1)

        canonical_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        rssi_val = _to_float(payload.get("rssi"))
        snr_val = _to_float(payload.get("snr"))
        noise_val = _to_float(payload.get("noise"))

        strike_count = self._normalize_strike_count(payload)
        storm_dist_mi = self._normalize_storm_dist_mi(payload)
        is_lightning_sensor = (
            "6045" in model.lower()
            or "lightning" in model.lower()
            or strike_count is not None
            or storm_dist_mi is not None
        )
        rfi_detected = self._normalize_rfi_detected(payload, is_lightning_sensor=is_lightning_sensor)

        active_storm_mode: Optional[bool] = None
        if is_lightning_sensor:
            now_mono = time.monotonic()
            cadence_key = (model, sensor_id)
            prev_mono = self._last_cadence_mono.get(cadence_key)
            self._last_cadence_mono[cadence_key] = now_mono
            rapid_cadence = prev_mono is not None and 4.0 <= (now_mono - prev_mono) <= 14.0
            raw_active = payload.get("active", payload.get("storm_mode"))
            flag_active = bool(raw_active) if isinstance(raw_active, (bool, int)) else False
            active_storm_mode = bool(rapid_cadence or flag_active or (storm_dist_mi is not None))

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
            strike_count=strike_count,
            storm_dist_mi=storm_dist_mi,
            rfi_detected=rfi_detected,
            active_storm_mode=active_storm_mode,
            rssi=round(rssi_val, 2) if rssi_val is not None else None,
            snr=round(snr_val, 2) if snr_val is not None else None,
            noise=round(noise_val, 2) if noise_val is not None else None,
            raw_json=canonical_json,
        )

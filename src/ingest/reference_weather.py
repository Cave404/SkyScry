"""Regional barometric pressure and 48-hour historical weather backfill via Open-Meteo API."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.db import WeatherDatabase

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class ReferenceWeatherService:
    """Fetches no-key regional barometric pressure (`inHg`/`hPa`) and 48h history from Open-Meteo."""

    def __init__(
        self,
        db: WeatherDatabase,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
    ) -> None:
        self.db = db
        self.default_lat = float(default_lat)
        self.default_lon = float(default_lon)

    def get_coordinates(self) -> tuple[float, float]:
        """Resolve stored latitude/longitude from SQLite settings or fall back to config defaults."""
        coords = self.db.get_location_coordinates(
            default_lat=self.default_lat,
            default_lon=self.default_lon,
        )
        return coords["latitude"], coords["longitude"]

    def sync_regional_weather(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        past_hours: int = 48,
    ) -> dict[str, Any]:
        """Pull past 48h 15-min / hourly barometric pressure & weather observations from Open-Meteo."""
        lat, lon = self.get_coordinates()
        if latitude is not None and longitude is not None:
            lat = float(latitude)
            lon = float(longitude)
            self.db.set_location_coordinates(lat, lon)

        if abs(lat) < 0.001 and abs(lon) < 0.001:
            logger.debug("Skipping Open-Meteo regional sync: station coordinates are not configured (0.0, 0.0).")
            return {
                "synced_records": 0,
                "latitude": lat,
                "longitude": lon,
                "latest_pressure_hpa": None,
                "latest_pressure_inhg": None,
            }

        query_params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current": "temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation",
            "hourly": "temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation",
            "minutely_15": "temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation",
            "past_days": "30",
            "forecast_days": "1",
            "past_minutely_15": str(max(16, min(int(past_hours) * 4, 384))),
            "forecast_minutely_15": "4",
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }

        url = f"{OPEN_METEO_URL}?{urllib.parse.urlencode(query_params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SkyScry/0.0.12 (https://github.com/Cave404/SkyScry)"},
        )

        with urllib.request.urlopen(req, timeout=12.0) as resp:
            raw_body = resp.read().decode("utf-8")
            payload = json.loads(raw_body)

        now_epoch = int(datetime.now(timezone.utc).timestamp())
        records_map: dict[str, dict[str, Any]] = {}

        def _extract_series(blk: dict[str, Any]) -> None:
            times = blk.get("time") or []
            temps = blk.get("temperature_2m") or []
            rhs = blk.get("relative_humidity_2m") or []
            p_msl = blk.get("pressure_msl") or []
            p_surf = blk.get("surface_pressure") or []
            w_spd = blk.get("wind_speed_10m") or []
            w_dir = blk.get("wind_direction_10m") or []
            w_gst = blk.get("wind_gusts_10m") or []
            precip = blk.get("precipitation") or []

            for idx, t_str in enumerate(times):
                if not t_str:
                    continue
                cleaned = str(t_str).strip()
                if not cleaned.endswith("Z") and "+" not in cleaned:
                    cleaned += ":00Z" if len(cleaned) == 16 else "Z"
                try:
                    dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00")).astimezone(timezone.utc)
                except ValueError:
                    continue

                if int(dt.timestamp()) > now_epoch + 300:
                    continue

                iso_z = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                press_val = (
                    p_msl[idx]
                    if idx < len(p_msl) and p_msl[idx] is not None
                    else (p_surf[idx] if idx < len(p_surf) else None)
                )
                records_map[iso_z] = {
                    "timestamp": iso_z,
                    "pressure_hpa": float(press_val) if press_val is not None else None,
                    "surface_pressure_hpa": (
                        float(p_surf[idx]) if idx < len(p_surf) and p_surf[idx] is not None else None
                    ),
                    "temp_c": float(temps[idx]) if idx < len(temps) and temps[idx] is not None else None,
                    "humidity": float(rhs[idx]) if idx < len(rhs) and rhs[idx] is not None else None,
                    "wind_avg_mps": (
                        float(w_spd[idx]) if idx < len(w_spd) and w_spd[idx] is not None else None
                    ),
                    "wind_gust_mps": (
                        float(w_gst[idx]) if idx < len(w_gst) and w_gst[idx] is not None else None
                    ),
                    "wind_dir_deg": (
                        float(w_dir[idx]) if idx < len(w_dir) and w_dir[idx] is not None else None
                    ),
                    "rain_mm": (
                        float(precip[idx]) if idx < len(precip) and precip[idx] is not None else 0.0
                    ),
                }

        # 1. First extract 30 days of hourly observations
        if isinstance(payload.get("hourly"), dict):
            _extract_series(payload["hourly"])

        # 2. Layer recent high-res 15-minute observations over hourly data
        if isinstance(payload.get("minutely_15"), dict):
            _extract_series(payload["minutely_15"])

        records = sorted(records_map.values(), key=lambda r: str(r["timestamp"]))

        # Also capture exact `current` block if provided
        curr = payload.get("current") or {}
        if curr.get("time"):
            dt_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            iso_now = dt_now.strftime("%Y-%m-%dT%H:%M:%SZ")
            c_press = (
                curr.get("pressure_msl")
                if curr.get("pressure_msl") is not None
                else curr.get("surface_pressure")
            )
            records.append(
                {
                    "timestamp": iso_now,
                    "pressure_hpa": float(c_press) if c_press is not None else None,
                    "surface_pressure_hpa": (
                        float(curr["surface_pressure"])
                        if curr.get("surface_pressure") is not None
                        else None
                    ),
                    "temp_c": (
                        float(curr["temperature_2m"])
                        if curr.get("temperature_2m") is not None
                        else None
                    ),
                    "humidity": (
                        float(curr["relative_humidity_2m"])
                        if curr.get("relative_humidity_2m") is not None
                        else None
                    ),
                    "wind_avg_mps": (
                        float(curr["wind_speed_10m"])
                        if curr.get("wind_speed_10m") is not None
                        else None
                    ),
                    "wind_gust_mps": (
                        float(curr["wind_gusts_10m"])
                        if curr.get("wind_gusts_10m") is not None
                        else None
                    ),
                    "wind_dir_deg": (
                        float(curr["wind_direction_10m"])
                        if curr.get("wind_direction_10m") is not None
                        else None
                    ),
                    "rain_mm": (
                        float(curr["precipitation"])
                        if curr.get("precipitation") is not None
                        else 0.0
                    ),
                }
            )

        upserted = self.db.upsert_reference_history(records)
        try:
            self.db.sync_daily_summaries(days_back=35)
        except Exception as exc:
            logger.debug("Automatic daily summaries sync warning after reference ingest: %s", exc)

        latest_press = records[-1]["pressure_hpa"] if records else None
        latest_inhg = round(latest_press * 0.029529983071445, 2) if latest_press else None
        logger.info(
            "Synced %d regional reference/barometric records from Open-Meteo (%.4f, %.4f) — Baro: %s inHg (%s hPa)",
            upserted,
            lat,
            lon,
            latest_inhg,
            latest_press,
        )
        return {
            "synced_records": upserted,
            "latitude": lat,
            "longitude": lon,
            "latest_pressure_hpa": latest_press,
            "latest_pressure_inhg": latest_inhg,
        }


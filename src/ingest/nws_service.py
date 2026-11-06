"""US National Weather Service (api.weather.gov) baseline corroboration, active alerts, and radar metadata service for SkyScry (v0.0.12)."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from src.models import COMPASS_16_POINTS, compute_dew_point_c

if TYPE_CHECKING:
    from src.db import WeatherDatabase

logger = logging.getLogger(__name__)

NWS_BASE_URL = "https://api.weather.gov"
NWS_USER_AGENT = "(SkyScry Weather Telemetry, github.com/Cave404/SkyScry)"
NWS_CACHE_TTL_SECONDS = 900.0  # 15 minutes
HPA_TO_INHG = 0.029529983071445


def _deg_to_cardinal(deg: Optional[float]) -> Optional[str]:
    if deg is None:
        return None
    normalized_deg = float(deg) % 360.0
    index = int((normalized_deg + 11.25) // 22.5) % 16
    return COMPASS_16_POINTS[index]


def _extract_val(prop: Any) -> Optional[float]:
    """Safely extract numeric value from an NWS QuantitativeValue dict `{'value': ..., 'unitCode': ...}`."""
    if not isinstance(prop, dict):
        return None
    val = prop.get("value")
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class NWSWeatherService:
    """Integrates official US National Weather Service (`api.weather.gov`) observations, active alerts, and 7-day forecast."""

    def __init__(
        self,
        db: WeatherDatabase,
        default_lat: float = 0.0,
        default_lon: float = 0.0,
        cache_ttl_seconds: float = NWS_CACHE_TTL_SECONDS,
    ) -> None:
        self.db = db
        self.default_lat = float(default_lat)
        self.default_lon = float(default_lon)
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._lock = threading.Lock()
        self._cache: Optional[dict[str, Any]] = None
        self._cache_time: float = 0.0
        self._cache_coords: tuple[float, float] = (0.0, 0.0)

    def _http_get_json(self, url: str, timeout: float = 10.0) -> dict[str, Any]:
        """Perform a compliant GET request against `api.weather.gov` with GeoJSON Accept and User-Agent headers."""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": NWS_USER_AGENT,
                "Accept": "application/geo+json, application/json;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw_body)
            return data if isinstance(data, dict) else {}

    def get_coordinates(self) -> tuple[float, float]:
        """Resolve station coordinates from `system_settings` or fall back to defaults."""
        coords = self.db.get_location_coordinates(
            default_lat=self.default_lat,
            default_lon=self.default_lon,
            auto_resolve=True,
        )
        return float(coords.get("latitude", 0.0)), float(coords.get("longitude", 0.0))

    def invalidate_cache(self) -> None:
        """Clear cached NWS response when coordinates or settings are updated."""
        with self._lock:
            self._cache = None
            self._cache_time = 0.0

    def _parse_observation(
        self,
        obs_payload: dict[str, Any],
        station_id: str,
        station_name: str,
    ) -> dict[str, Any]:
        """Normalize an NWS `/stations/{id}/observations/latest` GeoJSON payload."""
        props = obs_payload.get("properties") if isinstance(obs_payload.get("properties"), dict) else {}

        temp_c = _extract_val(props.get("temperature"))
        temp_f = round((temp_c * 9.0 / 5.0) + 32.0, 1) if temp_c is not None else None

        dp_c = _extract_val(props.get("dewpoint"))
        if dp_c is None and temp_c is not None:
            rh_raw = _extract_val(props.get("relativeHumidity"))
            dp_c = compute_dew_point_c(temp_c, rh_raw)
        dp_f = round((dp_c * 9.0 / 5.0) + 32.0, 1) if dp_c is not None else None

        rh_val = _extract_val(props.get("relativeHumidity"))
        humidity = round(rh_val, 1) if rh_val is not None else None

        # Pressure in Pa -> hPa & inHg
        baro_pa = _extract_val(props.get("seaLevelPressure"))
        if baro_pa is None:
            baro_pa = _extract_val(props.get("barometricPressure"))
        pressure_hpa: Optional[float] = None
        pressure_inhg: Optional[float] = None
        if baro_pa is not None:
            hpa_candidate = baro_pa / 100.0 if baro_pa > 2000.0 else baro_pa
            if 870.0 <= hpa_candidate <= 1085.0:
                pressure_hpa = round(hpa_candidate, 1)
                pressure_inhg = round(hpa_candidate * HPA_TO_INHG, 2)

        # Wind speed (unitCode may be wmoUnit:km_h-1 or wmoUnit:m_s-1)
        wind_prop = props.get("windSpeed") if isinstance(props.get("windSpeed"), dict) else {}
        wind_raw = _extract_val(wind_prop)
        wind_unit = str(wind_prop.get("unitCode") or "").lower()
        wind_mph: Optional[float] = None
        wind_mps: Optional[float] = None
        if wind_raw is not None:
            if "km_h" in wind_unit or "km/h" in wind_unit:
                wind_mps = round(wind_raw / 3.6, 2)
                wind_mph = round(wind_raw * 0.621371, 1)
            elif "mi_h" in wind_unit or "mph" in wind_unit:
                wind_mph = round(wind_raw, 1)
                wind_mps = round(wind_raw * 0.44704, 2)
            else:
                wind_mps = round(wind_raw, 2)
                wind_mph = round(wind_raw * 2.23693629, 1)

        gust_prop = props.get("windGust") if isinstance(props.get("windGust"), dict) else {}
        gust_raw = _extract_val(gust_prop)
        gust_unit = str(gust_prop.get("unitCode") or "").lower()
        wind_gust_mph: Optional[float] = None
        if gust_raw is not None:
            if "km_h" in gust_unit or "km/h" in gust_unit:
                wind_gust_mph = round(gust_raw * 0.621371, 1)
            elif "mi_h" in gust_unit or "mph" in gust_unit:
                wind_gust_mph = round(gust_raw, 1)
            else:
                wind_gust_mph = round(gust_raw * 2.23693629, 1)

        wind_dir = _extract_val(props.get("windDirection"))
        wind_cardinal = _deg_to_cardinal(wind_dir)

        vis_m = _extract_val(props.get("visibility"))
        vis_miles = round(vis_m / 1609.344, 1) if vis_m is not None else None

        condition_text = str(props.get("textDescription") or "Observed").strip() or "Observed"
        obs_timestamp = str(props.get("timestamp") or "").strip() or None

        return {
            "station_id": station_id,
            "station_name": station_name or station_id,
            "timestamp": obs_timestamp,
            "condition": condition_text,
            "text_description": condition_text,
            "temp_c": round(temp_c, 1) if temp_c is not None else None,
            "temp_f": temp_f,
            "dew_point_c": round(dp_c, 1) if dp_c is not None else None,
            "dew_point_f": dp_f,
            "humidity": humidity,
            "pressure_hpa": pressure_hpa,
            "pressure_inhg": pressure_inhg,
            "wind_mph": wind_mph,
            "wind_speed_mph": wind_mph,
            "wind_mps": wind_mps,
            "wind_gust_mph": wind_gust_mph,
            "wind_dir_deg": round(wind_dir, 0) if wind_dir is not None else None,
            "wind_cardinal": wind_cardinal,
            "visibility_mi": vis_miles,
        }

    def _fetch_open_meteo_forecast(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """Fallback 7-day daily forecast generated from Open-Meteo if NWS gridpoint is unavailable."""
        try:
            params = {
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max,windspeed_10m_max,winddirection_10m_dominant",
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
                "timezone": "auto",
            }
            url = f"https://api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "SkyScry/0.0.12 (https://github.com/Cave404/SkyScry)"},
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            daily = data.get("daily") or {}
            times = daily.get("time") or []
            wcodes = daily.get("weathercode") or []
            t_max = daily.get("temperature_2m_max") or []
            t_min = daily.get("temperature_2m_min") or []
            pops = daily.get("precipitation_probability_max") or []
            winds = daily.get("windspeed_10m_max") or []
            wind_dirs = daily.get("winddirection_10m_dominant") or []

            wcode_map = {
                0: "Clear / Sunny",
                1: "Mainly Sunny",
                2: "Partly Cloudy",
                3: "Overcast",
                45: "Foggy",
                48: "Rime Fog",
                51: "Light Drizzle",
                53: "Drizzle",
                55: "Dense Drizzle",
                56: "Freezing Drizzle",
                57: "Dense Freezing Drizzle",
                61: "Slight Rain",
                63: "Moderate Rain",
                65: "Heavy Rain",
                66: "Freezing Rain",
                67: "Heavy Freezing Rain",
                71: "Slight Snow",
                73: "Moderate Snow",
                75: "Heavy Snow",
                77: "Snow Grains",
                80: "Rain Showers",
                81: "Scattered Showers",
                82: "Violent Rain Showers",
                85: "Slight Snow Showers",
                86: "Heavy Snow Showers",
                95: "Thunderstorms",
                96: "Thunderstorms w/ Hail",
                99: "Severe Thunderstorms",
            }

            periods: list[dict[str, Any]] = []
            for idx, date_str in enumerate(times[:7]):
                code = wcodes[idx] if idx < len(wcodes) and wcodes[idx] is not None else 0
                cond = wcode_map.get(int(code), "Partly Cloudy")
                hi = round(float(t_max[idx]), 1) if idx < len(t_max) and t_max[idx] is not None else None
                lo = round(float(t_min[idx]), 1) if idx < len(t_min) and t_min[idx] is not None else None
                pop = int(round(float(pops[idx]))) if idx < len(pops) and pops[idx] is not None else 0
                w_spd = round(float(winds[idx]), 1) if idx < len(winds) and winds[idx] is not None else None
                w_dir_deg = wind_dirs[idx] if idx < len(wind_dirs) and wind_dirs[idx] is not None else None
                w_card = _deg_to_cardinal(w_dir_deg) or ""

                try:
                    dt = datetime.fromisoformat(date_str)
                    day_name = dt.strftime("%A")
                    if idx == 0:
                        day_name = "Today"
                except Exception:
                    day_name = f"Day {idx + 1}"

                w_str = f"{w_spd} mph" if w_spd is not None else "--"
                det_parts = [f"{cond} with high near {hi}°F and low near {lo}°F."]
                if pop > 0:
                    det_parts.append(f"Precipitation chance {pop}%.")
                if w_spd:
                    det_parts.append(f"Winds {w_card} up to {w_spd} mph.")

                periods.append(
                    {
                        "number": idx + 1,
                        "name": day_name,
                        "is_daytime": True,
                        "temp": hi,
                        "temperature": hi,
                        "temp_unit": "F",
                        "temperature_unit": "F",
                        "wind_speed": w_str,
                        "wind_direction": w_card,
                        "short_forecast": cond,
                        "detailed_forecast": " ".join(det_parts),
                        "pop_percent": pop,
                        "pop_pct": pop,
                        "icon": "",
                    }
                )
            return periods
        except Exception as exc:
            logger.debug("Open-Meteo fallback forecast fetch warning: %s", exc)
            return []

    def fetch_nws_corroboration(
        self,
        local_overview: Optional[dict[str, Any]] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch or serve cached NWS baseline observation, active alerts, 7-day forecast, and microclimate deltas."""
        lat, lon = self.get_coordinates()
        now_mono = time.monotonic()

        if abs(lat) < 0.001 and abs(lon) < 0.001:
            return {
                "ok": True,
                "configured": False,
                "coordinates": {"latitude": lat, "longitude": lon},
                "message": "Configure station Latitude & Longitude in Settings to enable official NWS baseline corroboration and live NEXRAD radar centering.",
                "office": None,
                "observation": None,
                "corroboration": None,
                "alerts": [],
                "forecast_periods": [],
            }

        with self._lock:
            if (
                not force_refresh
                and self._cache is not None
                and (now_mono - self._cache_time) < self.cache_ttl_seconds
                and abs(self._cache_coords[0] - lat) < 0.001
                and abs(self._cache_coords[1] - lon) < 0.001
            ):
                cached_copy = dict(self._cache)
                if local_overview is not None:
                    cached_copy["corroboration"] = self._compute_corroboration_deltas(
                        local_overview,
                        cached_copy.get("observation"),
                    )
                return cached_copy

        lat_str = f"{lat:.4f}"
        lon_str = f"{lon:.4f}"
        office_id = self.db.get_setting("nws_office") or ""
        grid_x = self.db.get_setting("nws_grid_x") or ""
        grid_y = self.db.get_setting("nws_grid_y") or ""
        city_state = ""
        forecast_url = ""
        stations_url = ""

        try:
            points_data = self._http_get_json(f"{NWS_BASE_URL}/points/{lat_str},{lon_str}")
            p_props = points_data.get("properties") if isinstance(points_data.get("properties"), dict) else {}
            office_id = str(p_props.get("gridId") or p_props.get("cwa") or office_id).strip()
            gx = p_props.get("gridX")
            gy = p_props.get("gridY")
            if gx is not None:
                grid_x = str(gx)
            if gy is not None:
                grid_y = str(gy)
            forecast_url = str(p_props.get("forecast") or "").strip()
            stations_url = str(p_props.get("observationStations") or "").strip()

            rel_loc = p_props.get("relativeLocation")
            if isinstance(rel_loc, dict):
                rel_props = rel_loc.get("properties") if isinstance(rel_loc.get("properties"), dict) else {}
                city = str(rel_props.get("city") or "").strip()
                state = str(rel_props.get("state") or "").strip()
                if city and state:
                    city_state = f"{city}, {state}"

            if office_id:
                self.db.set_setting("nws_office", office_id)
            if grid_x:
                self.db.set_setting("nws_grid_x", str(grid_x))
            if grid_y:
                self.db.set_setting("nws_grid_y", str(grid_y))
        except Exception as exc:
            logger.debug("NWS /points lookup warning for (%s, %s): %s", lat_str, lon_str, exc)

        # 1. Nearest Observation Station & Latest Observation
        observation: Optional[dict[str, Any]] = None
        if stations_url:
            try:
                st_data = self._http_get_json(stations_url)
                features = st_data.get("features") if isinstance(st_data.get("features"), list) else []
                for feat in features[:3]:
                    if not isinstance(feat, dict):
                        continue
                    f_props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
                    st_id = str(f_props.get("stationIdentifier") or "").strip()
                    st_name = str(f_props.get("name") or st_id).strip()
                    if not st_id:
                        continue
                    obs_url = f"{NWS_BASE_URL}/stations/{urllib.parse.quote(st_id)}/observations/latest"
                    obs_raw = self._http_get_json(obs_url)
                    parsed = self._parse_observation(obs_raw, st_id, st_name)
                    if parsed.get("temp_f") is not None or parsed.get("pressure_inhg") is not None:
                        observation = parsed
                        break
            except Exception as exc:
                logger.debug("NWS station observation fetch warning: %s", exc)

        # 2. Active Weather Alerts for Point
        alerts_list: list[dict[str, Any]] = []
        try:
            alerts_url = f"{NWS_BASE_URL}/alerts/active?point={lat_str},{lon_str}"
            alerts_data = self._http_get_json(alerts_url)
            a_features = alerts_data.get("features") if isinstance(alerts_data.get("features"), list) else []
            for a_feat in a_features[:8]:
                if not isinstance(a_feat, dict):
                    continue
                ap = a_feat.get("properties") if isinstance(a_feat.get("properties"), dict) else {}
                alerts_list.append(
                    {
                        "id": str(ap.get("id") or ""),
                        "event": str(ap.get("event") or "Weather Advisory"),
                        "headline": str(ap.get("headline") or ap.get("event") or ""),
                        "severity": str(ap.get("severity") or "Moderate"),
                        "urgency": str(ap.get("urgency") or ""),
                        "certainty": str(ap.get("certainty") or ""),
                        "area_desc": str(ap.get("areaDesc") or ""),
                        "effective": ap.get("effective"),
                        "expires": ap.get("expires"),
                        "description": str(ap.get("description") or "")[:900],
                        "instruction": str(ap.get("instruction") or "")[:500],
                    }
                )
        except Exception as exc:
            logger.debug("NWS active alerts fetch warning: %s", exc)

        # 3. Official 7-Day Gridpoint Forecast
        forecast_periods: list[dict[str, Any]] = []
        if not forecast_url and office_id and grid_x and grid_y:
            forecast_url = f"{NWS_BASE_URL}/gridpoints/{office_id}/{grid_x},{grid_y}/forecast"

        if forecast_url:
            try:
                fc_data = self._http_get_json(forecast_url)
                fc_props = fc_data.get("properties") if isinstance(fc_data.get("properties"), dict) else {}
                raw_periods = fc_props.get("periods") if isinstance(fc_props.get("periods"), list) else []
                for p in raw_periods[:14]:
                    if not isinstance(p, dict):
                        continue
                    pop_dict = p.get("probabilityOfPrecipitation")
                    pop_val = _extract_val(pop_dict)
                    forecast_periods.append(
                        {
                            "number": p.get("number"),
                            "name": str(p.get("name") or ""),
                            "is_daytime": bool(p.get("isDaytime", True)),
                            "temp": p.get("temperature"),
                            "temperature": p.get("temperature"),
                            "temp_unit": str(p.get("temperatureUnit") or "F"),
                            "temperature_unit": str(p.get("temperatureUnit") or "F"),
                            "wind_speed": str(p.get("windSpeed") or ""),
                            "wind_direction": str(p.get("windDirection") or ""),
                            "short_forecast": str(p.get("shortForecast") or ""),
                            "detailed_forecast": str(p.get("detailedForecast") or ""),
                            "pop_percent": int(round(pop_val)) if pop_val is not None else 0,
                            "pop_pct": int(round(pop_val)) if pop_val is not None else 0,
                            "icon": str(p.get("icon") or ""),
                        }
                    )
            except Exception as exc:
                logger.debug("NWS 7-day forecast fetch warning: %s", exc)

        if not forecast_periods:
            logger.info("NWS forecast empty or unavailable; fetching 7-day fallback from Open-Meteo...")
            forecast_periods = self._fetch_open_meteo_forecast(lat, lon)

        corroboration = self._compute_corroboration_deltas(local_overview, observation)

        station_info = {
            "station_id": (observation or {}).get("station_id") or office_id or "NWS",
            "station_name": (observation or {}).get("station_name") or city_state or f"{lat_str}, {lon_str}",
            "wfo": office_id or None,
            "grid_x": grid_x or None,
            "grid_y": grid_y or None,
        }

        result: dict[str, Any] = {
            "ok": True,
            "configured": True,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "coordinates": {"latitude": lat, "longitude": lon},
            "location_label": city_state or f"{lat_str}, {lon_str}",
            "office": office_id or None,
            "grid_x": grid_x or None,
            "grid_y": grid_y or None,
            "radar_station": f"K{office_id}" if office_id and len(office_id) == 3 else None,
            "station": station_info,
            "observation": observation,
            "corroboration": corroboration,
            "alerts": alerts_list,
            "forecast_periods": forecast_periods,
            "forecast": forecast_periods,
        }

        with self._lock:
            self._cache = result
            self._cache_time = now_mono
            self._cache_coords = (lat, lon)

        return result

    @staticmethod
    def _compute_corroboration_deltas(
        local_overview: Optional[dict[str, Any]],
        nws_obs: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compare SkyScry 433 MHz Ensemble telemetry against the Official NWS Baseline station."""
        out = (local_overview or {}).get("outdoor") or {}
        baro = (local_overview or {}).get("barometer") or {}

        local_tf = out.get("temp_f")
        local_rh = out.get("humidity")
        local_baro = baro.get("pressure_inhg") or out.get("pressure_inhg")
        local_wind = out.get("wind_avg_mph")

        nws_tf = nws_obs.get("temp_f") if isinstance(nws_obs, dict) else None
        nws_rh = nws_obs.get("humidity") if isinstance(nws_obs, dict) else None
        nws_baro = nws_obs.get("pressure_inhg") if isinstance(nws_obs, dict) else None
        nws_wind = nws_obs.get("wind_mph") if isinstance(nws_obs, dict) else None
        station_id = nws_obs.get("station_id") if isinstance(nws_obs, dict) else None
        station_name = nws_obs.get("station_name") if isinstance(nws_obs, dict) else None

        temp_delta_f = (
            round(float(local_tf) - float(nws_tf), 1)
            if (local_tf is not None and nws_tf is not None)
            else None
        )
        rh_delta = (
            round(float(local_rh) - float(nws_rh), 1)
            if (local_rh is not None and nws_rh is not None)
            else None
        )
        baro_delta_inhg = (
            round(float(local_baro) - float(nws_baro), 2)
            if (local_baro is not None and nws_baro is not None)
            else None
        )
        wind_delta_mph = (
            round(float(local_wind) - float(nws_wind), 1)
            if (local_wind is not None and nws_wind is not None)
            else None
        )

        badge_text = "Awaiting local sensor & NWS station data"
        if local_tf is not None and nws_tf is not None and temp_delta_f is not None:
            st_tag = station_id or "NWS"
            badge_text = (
                f"Local Sensors: {float(local_tf):.1f}°F | "
                f"NWS Official ({st_tag}): {float(nws_tf):.1f}°F | "
                f"Microclimate Delta: {temp_delta_f:+.1f}°F"
            )
        elif nws_tf is not None:
            st_tag = station_id or "NWS"
            badge_text = f"NWS Official ({st_tag}): {float(nws_tf):.1f}°F | Local Sensors: Awaiting Outdoor Station"
        elif local_tf is not None:
            badge_text = f"Local Sensors: {float(local_tf):.1f}°F | NWS Official: Syncing..."

        return {
            "station_id": station_id,
            "station_name": station_name,
            "local_temp_f": local_tf,
            "nws_temp_f": nws_tf,
            "temp_delta_f": temp_delta_f,
            "local_humidity": local_rh,
            "local_rh": local_rh,
            "nws_humidity": nws_rh,
            "nws_rh": nws_rh,
            "humidity_delta": rh_delta,
            "humidity_delta_pct": rh_delta,
            "local_pressure_inhg": local_baro,
            "local_baro_inhg": local_baro,
            "nws_pressure_inhg": nws_baro,
            "nws_baro_inhg": nws_baro,
            "pressure_delta_inhg": baro_delta_inhg,
            "local_wind_mph": local_wind,
            "nws_wind_mph": nws_wind,
            "nws_wind_speed_mph": nws_wind,
            "wind_delta_mph": wind_delta_mph,
            "status": "Corroborated",
            "summary_badge": badge_text,
        }

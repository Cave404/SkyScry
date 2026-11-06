"""Multi-Factor Short-Term Nowcast Engine (1-24h) and Long-Term Seasonal/Climatology Insights for SkyScry (v0.0.12)."""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.db import WeatherDatabase

logger = logging.getLogger(__name__)

# Monthly Mid-Latitude Climatological Normal Mean Temperatures (°F) (1 = Jan .. 12 = Dec)
MONTHLY_NORMAL_TEMP_F: dict[int, float] = {
    1: 32.0,
    2: 36.5,
    3: 47.0,
    4: 58.0,
    5: 67.5,
    6: 76.5,
    7: 80.5,
    8: 79.0,
    9: 71.0,
    10: 59.5,
    11: 46.5,
    12: 35.5,
}


class ClimatologyEngine:
    """Computes multi-factor 12h/24h nowcast trajectories, diurnal curve estimates, degree days, and seasonal anomalies."""

    def __init__(self, db: WeatherDatabase) -> None:
        self.db = db

    @staticmethod
    def compute_short_term_nowcast(
        outdoor: dict[str, Any],
        baro: dict[str, Any],
        extremes_today: Optional[dict[str, Any]] = None,
        nws_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Generate a multi-factor confidence-scored 12h/24h Weather Trajectory.

        Factors combined:
          (a) 3-hour & 6-hour Barometric Pressure velocity (delta inHg/hr)
          (b) Dew Point depression (Temp - DewPoint) and closure rate
          (c) Wind direction backing/veering (shift to S/SE with falling pressure vs NW with rising pressure)
          (d) Solar/Lux attenuation during daylight hours
          (e) NWS official short-term PoP (Probability of Precipitation) corroboration
        """
        temp_f = outdoor.get("temp_f")
        dew_point_f = outdoor.get("dew_point_f")
        dew_spread_f = outdoor.get("dew_spread_f")
        rh = outdoor.get("humidity")
        wind_cardinal = outdoor.get("wind_cardinal")
        wind_mph = outdoor.get("wind_avg_mph") or 0.0
        lux = outdoor.get("lux")
        uv = outdoor.get("uv_index")
        rain_today_in = outdoor.get("rain_today_in") or outdoor.get("rain_24h_in") or 0.0
        rain_rate_in_h = outdoor.get("rain_rate_in_h") or 0.0

        press_inhg = baro.get("pressure_inhg")
        delta_3h_inhg = baro.get("delta_3h_inhg") or 0.0
        delta_6h_inhg = baro.get("delta_6h_inhg") or (delta_3h_inhg * 1.6)
        vel_inhg_hr = round(delta_3h_inhg / 3.0, 3)

        # Extract NWS short-term PoP if available
        nws_pop: Optional[int] = None
        nws_short: str = ""
        if isinstance(nws_data, dict):
            periods = nws_data.get("forecast_periods")
            if isinstance(periods, list) and periods:
                first_p = periods[0]
                if isinstance(first_p, dict):
                    if first_p.get("pop_percent") is not None:
                        nws_pop = int(first_p["pop_percent"])
                    nws_short = str(first_p.get("short_forecast") or "")

        southerly = wind_cardinal in ("S", "SSE", "SE", "ESE", "SSW", "SW")
        northerly_westerly = wind_cardinal in ("N", "NNW", "NW", "WNW", "NNE", "NE", "W")

        now_local = datetime.now()
        local_hour = now_local.hour
        is_daylight = 7 <= local_hour <= 18
        solar_attenuated = bool(is_daylight and lux is not None and float(lux) < 3500.0)

        # Score precipitation / unsettled vs fair / clearing probability
        precip_score = 20.0
        if rain_rate_in_h > 0.0:
            precip_score += 65.0
        if delta_3h_inhg <= -0.06:
            precip_score += 42.0
        elif delta_3h_inhg <= -0.03:
            precip_score += 26.0
        elif delta_3h_inhg >= 0.04:
            precip_score -= 22.0
        elif delta_3h_inhg >= 0.02:
            precip_score -= 12.0

        if press_inhg is not None:
            if float(press_inhg) <= 29.75:
                precip_score += 22.0
            elif float(press_inhg) >= 30.15:
                precip_score -= 18.0

        if dew_spread_f is not None:
            if float(dew_spread_f) <= 2.5:
                precip_score += 20.0
            elif float(dew_spread_f) <= 5.0:
                precip_score += 10.0
            elif float(dew_spread_f) >= 15.0:
                precip_score -= 12.0

        if southerly and delta_3h_inhg < -0.01:
            precip_score += 12.0
        elif northerly_westerly and delta_3h_inhg > 0.01:
            precip_score -= 12.0

        if solar_attenuated:
            precip_score += 10.0

        if nws_pop is not None:
            precip_score = (precip_score * 0.55) + (float(nws_pop) * 0.45)

        precip_prob = int(max(5, min(98, round(precip_score))))
        fair_prob = 100 - precip_prob

        if rain_rate_in_h > 0.0 or precip_prob >= 70:
            regime = "Active Precipitation / Stormy"
            confidence = precip_prob
            condition_prob_label = f"Condition Probability: {precip_prob}% Rain / Showers"
        elif precip_prob >= 45:
            regime = "Cloudy / Unsettled Transition"
            confidence = max(55, precip_prob + 10)
            condition_prob_label = f"Condition Probability: {precip_prob}% Unsettled / Showers"
        elif delta_3h_inhg >= 0.03:
            regime = "Clearing & High-Pressure Stabilization"
            confidence = max(72, fair_prob)
            condition_prob_label = f"Condition Probability: {confidence}% Fair / Clearing"
        else:
            regime = "Fair / Settled Microclimate"
            confidence = max(68, fair_prob)
            condition_prob_label = f"Condition Probability: {confidence}% Fair / Settled"

        # Diurnal Curve Fitting for Estimated Daytime High & Overnight Low
        est_high_f: Optional[float] = None
        est_low_f: Optional[float] = None
        if temp_f is not None:
            cur_t = float(temp_f)
            dp_floor = float(dew_point_f) if dew_point_f is not None else (cur_t - 8.0)
            # Typical diurnal amplitude modulated by humidity/cloud cover and wind
            diurnal_amp = 18.0
            if rh is not None and float(rh) > 75.0:
                diurnal_amp = 11.0
            elif rh is not None and float(rh) < 45.0:
                diurnal_amp = 23.0
            if float(wind_mph) >= 10.0:
                diurnal_amp *= 0.8

            # Phase of diurnal cycle: min around 06:00, max around 15:00
            phase_rad = ((local_hour - 6.0) / 24.0) * 2.0 * math.pi
            norm_pos = 0.5 * (1.0 - math.cos(phase_rad))  # 0 at 06:00, 1 at 18:00
            advection_adj = 2.5 if southerly else (-2.5 if northerly_westerly else 0.0)

            proj_high = cur_t + max(1.0, (1.0 - norm_pos) * diurnal_amp * 0.75) + max(0.0, advection_adj)
            proj_low = max(dp_floor - 1.5, cur_t - max(1.5, norm_pos * diurnal_amp * 0.75) + min(0.0, advection_adj))

            if isinstance(extremes_today, dict):
                td_high = extremes_today.get("temp_high_f")
                td_low = extremes_today.get("temp_low_f")
                if td_high is not None:
                    proj_high = max(proj_high, float(td_high))
                if td_low is not None and local_hour < 10:
                    proj_low = min(proj_low, float(td_low))

            est_high_f = round(proj_high, 1)
            est_low_f = round(proj_low, 1)

        if est_high_f is None:
            base_t = float(temp_f) if temp_f is not None else 68.0
            est_high_f = round(base_t + 4.5, 1)
            est_low_f = round(base_t - 6.5, 1)

        # Frost / Freeze / Fog / Heat Risk Badges
        risk_badges: list[dict[str, str]] = []
        check_low = est_low_f if est_low_f is not None else (float(temp_f) if temp_f is not None else 50.0)
        if check_low <= 28.0:
            risk_badges.append({"label": "Hard Freeze Warning Risk (<=28°F)", "tone": "cool"})
        elif check_low <= 32.0:
            risk_badges.append({"label": "Freeze Risk (<=32°F)", "tone": "cool"})
        elif check_low <= 36.5 and float(wind_mph) < 6.0:
            risk_badges.append({"label": "Radiational Frost Risk (33-36°F)", "tone": "cool"})

        if dew_spread_f is not None and float(dew_spread_f) <= 3.5 and (rh or 0) >= 86.0 and float(wind_mph) <= 7.0:
            risk_badges.append({"label": "Radiation Fog / Mist Risk (Spread <=3.5°F)", "tone": "info"})
        elif dew_spread_f is not None and float(dew_spread_f) <= 5.5 and (rh or 0) >= 80.0:
            risk_badges.append({"label": "Overnight Dew / Condensation Likely", "tone": "neutral"})

        if est_high_f is not None and est_high_f >= 92.0:
            risk_badges.append({"label": f"Heat Advisory Trajectory (~{est_high_f:.0f}°F)", "tone": "warm"})

        if not risk_badges:
            risk_badges.append({"label": "No Frost / Freeze / Fog Hazards", "tone": "info"})

        return {
            "regime": regime,
            "confidence_pct": confidence,
            "precip_probability_pct": precip_prob,
            "precip_prob_pct": precip_prob,
            "condition_probability_label": condition_prob_label,
            "baro_velocity_inhg_hr": vel_inhg_hr,
            "delta_6h_inhg": round(delta_6h_inhg, 2),
            "estimated_daytime_high_f": est_high_f,
            "estimated_overnight_low_f": est_low_f,
            "est_daytime_high_f": est_high_f,
            "est_overnight_low_f": est_low_f,
            "nws_pop_percent": nws_pop,
            "nws_short_forecast": nws_short,
            "risk_badges": risk_badges,
        }

    def get_climatology_report(self) -> dict[str, Any]:
        """Compile long-term weekly, monthly, and seasonal insights + Weather-Display Records Summary Table."""
        self.db.sync_daily_summaries()
        daily_rows = self.db.get_daily_summaries(limit=365)
        records_table = self.db.get_records_summary_table()

        now_utc = datetime.now(timezone.utc)
        current_month = now_utc.month
        seasonal_baseline_f = MONTHLY_NORMAL_TEMP_F.get(current_month, 58.0)

        last_7 = daily_rows[:7]
        last_30 = daily_rows[:30]

        def _summarize_window(rows: list[dict[str, Any]]) -> dict[str, Any]:
            if not rows:
                return {
                    "days_count": 0,
                    "mean_temp_f": None,
                    "high_temp_f": None,
                    "low_temp_f": None,
                    "diurnal_swing_avg_f": None,
                    "hdd_total": 0.0,
                    "cdd_total": 0.0,
                    "rain_total_in": 0.0,
                    "wet_days": 0,
                    "dry_days": 0,
                    "peak_wind_mph": 0.0,
                    "baro_volatility_inhg": None,
                }

            means = [float(r["temp_mean_f"]) for r in rows if r.get("temp_mean_f") is not None]
            highs = [float(r["temp_max_f"]) for r in rows if r.get("temp_max_f") is not None]
            lows = [float(r["temp_min_f"]) for r in rows if r.get("temp_min_f") is not None]
            swings = [
                float(r["temp_max_f"]) - float(r["temp_min_f"])
                for r in rows
                if r.get("temp_max_f") is not None and r.get("temp_min_f") is not None
            ]
            hdd = sum(float(r.get("hdd") or 0.0) for r in rows)
            cdd = sum(float(r.get("cdd") or 0.0) for r in rows)
            rain_in = sum(float(r.get("rain_total_in") or 0.0) for r in rows)
            wet_days = sum(1 for r in rows if float(r.get("rain_total_in") or 0.0) >= 0.01)
            dry_days = len(rows) - wet_days
            winds = [float(r.get("wind_peak_mph") or 0.0) for r in rows]
            p_ranges = [
                float(r["pressure_range_inhg"])
                for r in rows
                if r.get("pressure_range_inhg") is not None
            ]

            return {
                "days_count": len(rows),
                "mean_temp_f": round(sum(means) / len(means), 1) if means else None,
                "high_temp_f": round(max(highs), 1) if highs else None,
                "low_temp_f": round(min(lows), 1) if lows else None,
                "diurnal_swing_avg_f": round(sum(swings) / len(swings), 1) if swings else None,
                "hdd_total": round(hdd, 1),
                "cdd_total": round(cdd, 1),
                "rain_total_in": round(rain_in, 2),
                "wet_days": wet_days,
                "dry_days": dry_days,
                "peak_wind_mph": round(max(winds), 1) if winds else 0.0,
                "baro_volatility_inhg": round(sum(p_ranges) / len(p_ranges), 2) if p_ranges else None,
            }

        weekly_stats = _summarize_window(last_7)
        monthly_stats = _summarize_window(last_30)

        # Compute Wet / Dry streak from most recent days
        current_dry_streak = 0
        current_wet_streak = 0
        for r in daily_rows:
            has_rain = float(r.get("rain_total_in") or 0.0) >= 0.01
            if has_rain:
                if current_dry_streak > 0:
                    break
                current_wet_streak += 1
            else:
                if current_wet_streak > 0:
                    break
                current_dry_streak += 1

        # Seasonal Trajectory & Anomaly Estimator narrative
        mean_7d = weekly_stats.get("mean_temp_f")
        mean_30d = monthly_stats.get("mean_temp_f")
        baro_vol = weekly_stats.get("baro_volatility_inhg") or 0.12

        anomaly_7d_f: Optional[float] = None
        anomaly_30d_f: Optional[float] = None
        if mean_7d is not None:
            anomaly_7d_f = round(mean_7d - seasonal_baseline_f, 1)
        if mean_30d is not None:
            anomaly_30d_f = round(mean_30d - seasonal_baseline_f, 1)

        if anomaly_7d_f is not None:
            dir_word = "above" if anomaly_7d_f >= 0 else "below"
            stability_clause = (
                "with elevated barometric volatility — trending toward an active frontal transition period"
                if baro_vol >= 0.22
                else "with steady barometric stability — maintaining a settled seasonal regime"
            )
            moisture_clause = (
                f" ({weekly_stats['rain_total_in']:.2f} in rain across {weekly_stats['wet_days']} wet day(s) this week)."
                if weekly_stats["wet_days"] > 0
                else f" ({current_dry_streak}-day dry streak active)."
            )
            trajectory_narrative = (
                f"Rolling 7-day mean ({mean_7d:.1f}°F) is {anomaly_7d_f:+.1f}°F {dir_word} seasonal baseline "
                f"({seasonal_baseline_f:.1f}°F) {stability_clause}{moisture_clause}"
            )
        else:
            trajectory_narrative = (
                "Accumulating daily rollup summaries from trusted outdoor sensors to establish "
                "7-day and 30-day climatological anomaly trajectories."
            )

        return {
            "ok": True,
            "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seasonal_baseline_f": seasonal_baseline_f,
            "anomaly_7d_f": anomaly_7d_f,
            "anomaly_30d_f": anomaly_30d_f,
            "current_dry_streak_days": current_dry_streak,
            "current_wet_streak_days": current_wet_streak,
            "trajectory_narrative": trajectory_narrative,
            "weekly": weekly_stats,
            "monthly": monthly_stats,
            "records": records_table,
            "daily_summaries": daily_rows[:30],
        }

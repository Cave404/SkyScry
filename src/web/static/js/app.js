/**
 * SkyScry v0.0.4 — 4-View Frontend Controller
 * - View 1: Main Overview Dashboard with Animated SVG Weather Icons, Outdoor/Indoor Ensemble, & Nowcast
 * - View 2: Multi-Section Time-Bucketed Historical Charts with Outdoor Ensemble Average Line
 * - View 3: Dedicated Sensors Manager with In-Place DOM Patching (No Jitter/Flashing), RF Signal Bars, Battery Lifecycle, & Per-Sensor Metric Masking
 * - View 4: Raw SQLite Telemetry Log Explorer with CSV/JSON Export
 */

(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;

  let activeTab = "overview";
  let sensorsCache = [];
  let currentIconCode = "";
  let logsLoadedOnce = false;

  // Persistent Chart.js instances (mutated in-place, never destroyed on poll)
  const charts = {
    temp: null,
    rh: null,
    wind: null,
    rain: null,
  };

  // Header DOM
  const elSdrStatus = document.getElementById("stat-sdr-status");
  const elSensorCount = document.getElementById("stat-sensor-count");
  const elReadingCount = document.getElementById("stat-reading-count");
  const elDbSize = document.getElementById("stat-db-size");
  const elUptime = document.getElementById("stat-uptime");
  const elSyncDot = document.getElementById("stat-sync-indicator");
  const elLastSync = document.getElementById("stat-last-sync");
  const elErrorBanner = document.getElementById("sdr-error-banner");
  const elBtnRefresh = document.getElementById("btn-refresh-now");

  // View 1: Overview DOM
  const elHeroIcon = document.getElementById("hero-weather-icon");
  const elOvConditionLabel = document.getElementById("ov-condition-label");
  const elOvOutTempF = document.getElementById("ov-out-temp-f");
  const elOvOutTempC = document.getElementById("ov-out-temp-c");
  const elOvOutSpread = document.getElementById("ov-out-spread");
  const elOvTrendBadges = document.getElementById("ov-trend-badges");
  const elOvNowcastText = document.getElementById("ov-nowcast-text");
  const elOvInCount = document.getElementById("ov-in-count");
  const elOvInTemp = document.getElementById("ov-in-temp");
  const elOvInTempC = document.getElementById("ov-in-temp-c");
  const elOvInRh = document.getElementById("ov-in-rh");
  const elOvInComfort = document.getElementById("ov-in-comfort");
  const elOvDelta1hTemp = document.getElementById("ov-delta-1h-temp");
  const elOvDelta3hTemp = document.getElementById("ov-delta-3h-temp");
  const elOvDelta1hRh = document.getElementById("ov-delta-1h-rh");
  const elOvDewSpread = document.getElementById("ov-dew-spread");
  const elOvOutRh = document.getElementById("ov-out-rh");
  const elOvOutDp = document.getElementById("ov-out-dp");
  const elOvFeelsF = document.getElementById("ov-feels-f");
  const elOvFeelsC = document.getElementById("ov-feels-c");
  const elOvWindSpeed = document.getElementById("ov-wind-speed");
  const elOvWindSub = document.getElementById("ov-wind-sub");
  const elOvRain24h = document.getElementById("ov-rain-24h");
  const elOvRainMm = document.getElementById("ov-rain-mm");
  const elOvUvLux = document.getElementById("ov-uv-lux");
  const elOvLuxSub = document.getElementById("ov-lux-sub");

  // View 2: Charts DOM
  const elChartHours = document.getElementById("chart-hours-select");
  const elChartLocation = document.getElementById("chart-location-select");
  const elChartBucket = document.getElementById("chart-bucket-select");
  const elChartToggleEnsemble = document.getElementById("chart-toggle-ensemble");
  const elChartMetaTemp = document.getElementById("chart-meta-temp");
  const elChartMetaRh = document.getElementById("chart-meta-rh");

  // View 3: Sensors DOM
  const elSensorGrid = document.getElementById("sensor-grid");
  const elSensorEmpty = document.getElementById("sensor-grid-empty");
  const elToggleIgnored = document.getElementById("toggle-show-ignored");

  // View 4: Logs DOM
  const elLogSensor = document.getElementById("log-sensor-select");
  const elLogHours = document.getElementById("log-hours-select");
  const elLogLimit = document.getElementById("log-limit-select");
  const elBtnRunLogQuery = document.getElementById("btn-run-log-query");
  const elBtnExportCsv = document.getElementById("btn-export-csv");
  const elBtnExportJson = document.getElementById("btn-export-json");
  const elLogSummary = document.getElementById("log-record-summary");
  const elLogTableBody = document.getElementById("log-table-body");

  // Modal DOM
  const elModalBackdrop = document.getElementById("sensor-modal-backdrop");
  const elModalForm = document.getElementById("sensor-config-form");
  const elModalSubtitle = document.getElementById("modal-subtitle");
  const elModalSensorKey = document.getElementById("modal-sensor-key");
  const elModalAlias = document.getElementById("modal-alias-input");
  const elModalLocation = document.getElementById("modal-location-select");
  const elModalTrusted = document.getElementById("modal-trusted-checkbox");
  const elModalBattReset = document.getElementById("modal-battery-reset-checkbox");
  const elModalFeedback = document.getElementById("modal-feedback");
  const elBtnCloseModal = document.getElementById("btn-close-modal");
  const elBtnCancelModal = document.getElementById("btn-cancel-modal");
  const elBtnClearAlias = document.getElementById("btn-clear-alias");

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function parseUtcDate(isoStr) {
    if (!isoStr) return null;
    let s = String(isoStr).trim();
    if (!s.endsWith("Z") && !/[+-]\d{2}:\d{2}$/.test(s)) {
      s += "Z";
    }
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function formatRelativeSeconds(isoStr) {
    const dt = parseUtcDate(isoStr);
    if (!dt) return "never";
    const diffSec = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
    if (diffSec < 60) return `${diffSec}s ago`;
    const mins = Math.floor(diffSec / 60);
    const secs = diffSec % 60;
    if (mins < 60) return `${mins}m ${secs}s ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 48) return `${hrs}h ${mins % 60}m ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  function formatLocalShortTime(isoStr, includeDate) {
    const dt = parseUtcDate(isoStr);
    if (!dt) return "--";
    if (includeDate) {
      return dt.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    return dt.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatDuration(seconds) {
    if (typeof seconds !== "number" || isNaN(seconds)) return "--";
    const s = Math.floor(seconds);
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const secs = s % 60;
    if (hrs > 0) return `${hrs}h ${mins}m ${secs}s`;
    if (mins > 0) return `${mins}m ${secs}s`;
    return `${secs}s`;
  }

  // ==========================================================================
  // ANIMATED WEATHER CONDITION SVG LIBRARY
  // ==========================================================================
  function getAnimatedWeatherSvg(code) {
    switch (code) {
      case "clear-day":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-sun-rays" stroke="#fbbf24" stroke-width="4" stroke-linecap="round">
              <line x1="48" y1="10" x2="48" y2="20" />
              <line x1="48" y1="76" x2="48" y2="86" />
              <line x1="10" y1="48" x2="20" y2="48" />
              <line x1="76" y1="48" x2="86" y2="48" />
              <line x1="21" y1="21" x2="28" y2="28" />
              <line x1="68" y1="68" x2="75" y2="75" />
              <line x1="75" y1="21" x2="68" y2="28" />
              <line x1="28" y1="68" x2="21" y2="75" />
            </g>
            <circle cx="48" cy="48" r="18" fill="url(#sunGrad)" />
            <defs>
              <radialGradient id="sunGrad" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(44 42) scale(24)">
                <stop stop-color="#fde047" />
                <stop offset="1" stop-color="#f59e0b" />
              </radialGradient>
            </defs>
          </svg>`;

      case "clear-night":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle class="wx-star" cx="24" cy="24" r="2.2" fill="#fde047" />
            <circle class="wx-star" cx="74" cy="22" r="1.8" fill="#93c5fd" />
            <circle class="wx-star" cx="20" cy="64" r="1.6" fill="#e2e8f0" />
            <path d="M58 22C45 24 36 35 36 48C36 62 47 73 61 73C68 73 74 70 78 65C66 66 55 57 55 44C55 34 60 26 58 22Z"
                  fill="url(#moonGrad)" stroke="#93c5fd" stroke-width="1.5" />
            <defs>
              <linearGradient id="moonGrad" x1="36" y1="22" x2="78" y2="73" gradientUnits="userSpaceOnUse">
                <stop stop-color="#e0f2fe" />
                <stop offset="1" stop-color="#60a5fa" />
              </linearGradient>
            </defs>
          </svg>`;

      case "cloudy-night":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle class="wx-star" cx="22" cy="22" r="2" fill="#fde047" />
            <path d="M58 18C47 20 40 29 40 40C40 51 49 60 60 60C65 60 70 58 74 54C64 55 56 47 56 37C56 29 60 22 58 18Z"
                  fill="#93c5fd" opacity="0.85" />
            <g class="wx-cloud-body">
              <path d="M26 68H68C75 68 80 63 80 56C80 50 75 45 69 45C67 36 59 30 49 30C38 30 30 38 29 48C23 49 18 54 18 60C18 65 22 68 26 68Z"
                    fill="url(#cloudNightGrad)" stroke="#475569" stroke-width="1.5" />
            </g>
            <defs>
              <linearGradient id="cloudNightGrad" x1="20" y1="30" x2="78" y2="68" gradientUnits="userSpaceOnUse">
                <stop stop-color="#94a3b8" />
                <stop offset="1" stop-color="#475569" />
              </linearGradient>
            </defs>
          </svg>`;

      case "rain":
      case "heavy-rain":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-back">
              <path d="M32 54H72C77 54 82 50 82 44C82 39 78 35 73 35C71 27 64 22 56 22C46 22 39 29 38 37C33 38 29 42 29 47C29 51 30 54 32 54Z"
                    fill="#334155" />
            </g>
            <g class="wx-cloud-body">
              <path d="M24 62H66C73 62 78 57 78 50C78 44 73 39 67 39C65 30 57 24 47 24C36 24 28 32 27 42C21 43 16 48 16 54C16 59 20 62 24 62Z"
                    fill="#64748b" stroke="#94a3b8" stroke-width="1.2" />
            </g>
            <g stroke="#38bdf8" stroke-width="3.2" stroke-linecap="round">
              <line class="wx-drop-1" x1="32" y1="68" x2="28" y2="80" />
              <line class="wx-drop-2" x1="48" y1="68" x2="44" y2="80" />
              <line class="wx-drop-3" x1="64" y1="68" x2="60" y2="80" />
            </g>
          </svg>`;

      case "windy":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-body">
              <path d="M24 54H66C72 54 76 50 76 44C76 38 72 34 66 34C64 26 57 20 48 20C38 20 30 27 29 36C23 37 19 41 19 47C19 51 21 54 24 54Z"
                    fill="#64748b" />
            </g>
            <path class="wx-wind-line" d="M16 64H72C77 64 80 61 80 57C80 53 77 50 73 50" stroke="#38bdf8" stroke-width="3.5" stroke-linecap="round" />
            <path class="wx-wind-line" d="M22 74H62C67 74 70 77 70 81C70 84 67 87 63 87" stroke="#93c5fd" stroke-width="3.2" stroke-linecap="round" />
          </svg>`;

      case "fog":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-body">
              <path d="M24 54H68C74 54 78 50 78 44C78 38 74 34 68 34C66 26 59 20 50 20C40 20 32 27 31 36C25 37 20 41 20 47C20 51 22 54 24 54Z"
                    fill="#64748b" />
            </g>
            <g class="wx-fog-bar" stroke="#94a3b8" stroke-width="3.5" stroke-linecap="round">
              <line x1="20" y1="64" x2="76" y2="64" />
              <line x1="26" y1="73" x2="70" y2="73" />
              <line x1="18" y1="82" x2="64" y2="82" />
            </g>
          </svg>`;

      case "cloudy":
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-back">
              <path d="M34 56H74C79 56 84 52 84 46C84 41 80 37 75 37C73 29 66 24 58 24C48 24 41 31 40 39C35 40 31 44 31 49C31 53 32 56 34 56Z"
                    fill="#475569" />
            </g>
            <g class="wx-cloud-body">
              <path d="M22 68H66C73 68 78 63 78 56C78 50 73 45 67 45C65 36 57 30 47 30C36 30 28 38 27 48C21 49 16 54 16 60C16 65 19 68 22 68Z"
                    fill="#94a3b8" />
            </g>
          </svg>`;

      case "partly-cloudy-day":
      default:
        return `
          <svg viewBox="0 0 96 96" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-sun-rays" stroke="#fbbf24" stroke-width="3.5" stroke-linecap="round" style="transform-origin: 34px 34px;">
              <line x1="34" y1="10" x2="34" y2="16" />
              <line x1="34" y1="52" x2="34" y2="58" />
              <line x1="10" y1="34" x2="16" y2="34" />
              <line x1="52" y1="34" x2="58" y2="34" />
              <line x1="17" y1="17" x2="22" y2="22" />
              <line x1="46" y1="46" x2="51" y2="51" />
              <line x1="51" y1="17" x2="46" y2="22" />
              <line x1="22" y1="46" x2="17" y2="51" />
            </g>
            <circle cx="34" cy="34" r="13" fill="#fbbf24" />
            <g class="wx-cloud-body">
              <path d="M28 70H70C77 70 82 65 82 58C82 52 77 47 71 47C69 38 61 32 51 32C40 32 32 40 31 50C25 51 20 56 20 62C20 67 24 70 28 70Z"
                    fill="url(#partlyCloudGrad)" stroke="#cbd5e1" stroke-width="1.2" />
            </g>
            <defs>
              <linearGradient id="partlyCloudGrad" x1="20" y1="32" x2="80" y2="70" gradientUnits="userSpaceOnUse">
                <stop stop-color="#e2e8f0" />
                <stop offset="1" stop-color="#94a3b8" />
              </linearGradient>
            </defs>
          </svg>`;
    }
  }

  // ==========================================================================
  // API FETCHERS & VIEW UPDATERS
  // ==========================================================================
  async function fetchStatus() {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const sdrState = (data.sdr_status || "disconnected").toLowerCase();
    elSdrStatus.textContent = sdrState.toUpperCase();
    elSdrStatus.className =
      "status-pill " + (sdrState === "running" ? "status-running" : "status-disconnected");

    elSensorCount.textContent = `${data.active_sensors ?? 0} / ${data.total_sensors ?? 0}`;
    elReadingCount.textContent = Number(data.total_readings || 0).toLocaleString();
    elDbSize.textContent = `${Number(data.database_size_mb || 0).toFixed(2)} MB`;
    elUptime.textContent = formatDuration(data.uptime_seconds);

    if (data.sdr_last_error && sdrState !== "running") {
      elErrorBanner.textContent = `SDR DIAGNOSTIC: ${data.sdr_last_error}`;
      elErrorBanner.classList.remove("hidden");
    } else {
      elErrorBanner.classList.add("hidden");
    }
  }

  async function fetchOverview() {
    const res = await fetch("/api/overview");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const cond = data.condition || {};
    const out = data.outdoor || {};
    const ind = data.indoor || {};

    if (cond.code && cond.code !== currentIconCode) {
      currentIconCode = cond.code;
      elHeroIcon.innerHTML = getAnimatedWeatherSvg(cond.code);
    }

    elOvConditionLabel.textContent = cond.label || "Monitoring Active";
    elOvOutTempF.innerHTML =
      out.temp_f !== null && out.temp_f !== undefined
        ? `${Number(out.temp_f).toFixed(1)}&deg;F`
        : `--.-&deg;F`;
    elOvOutTempC.innerHTML =
      out.temp_c !== null && out.temp_c !== undefined
        ? `(${Number(out.temp_c).toFixed(1)}&deg;C)`
        : `(--.-&deg;C)`;

    if (out.temp_min_f !== null && out.temp_max_f !== null && out.sensor_count > 1) {
      elOvOutSpread.innerHTML = `Outdoor ensemble mean across ${out.sensor_count} stations | Spread: ${Number(out.temp_min_f).toFixed(1)}&deg;F &ndash; ${Number(out.temp_max_f).toFixed(1)}&deg;F`;
    } else {
      elOvOutSpread.textContent = `Outdoor ensemble across ${out.sensor_count ?? 0} active station(s)`;
    }

    const badges = Array.isArray(cond.trend_badges) ? cond.trend_badges : [];
    elOvTrendBadges.innerHTML = badges
      .map(
        (b) =>
          `<span class="trend-pill ${escapeHtml(b.tone || "neutral")}">${escapeHtml(b.label)}</span>`
      )
      .join("");

    elOvNowcastText.textContent = cond.nowcast_summary || "";

    // Indoor card
    elOvInCount.textContent = `${ind.sensor_count ?? 0} station(s)`;
    elOvInTemp.innerHTML =
      ind.temp_f !== null && ind.temp_f !== undefined
        ? `${Number(ind.temp_f).toFixed(1)}&deg;F`
        : `--.-&deg;F`;
    elOvInTempC.innerHTML =
      ind.temp_c !== null && ind.temp_c !== undefined
        ? `${Number(ind.temp_c).toFixed(1)}&deg;C`
        : `--.-&deg;C`;
    elOvInRh.textContent =
      ind.humidity !== null && ind.humidity !== undefined
        ? `${Number(ind.humidity).toFixed(0)}%`
        : `--%`;
    if (ind.humidity !== null && ind.humidity !== undefined) {
      const rh = Number(ind.humidity);
      elOvInComfort.textContent =
        rh < 30 ? "Dry Air" : rh <= 55 ? "Optimal Comfort" : "Humid Indoor";
    } else {
      elOvInComfort.textContent = "Assign 'Indoor' in Sensors tab";
    }

    // Deltas
    elOvDelta1hTemp.textContent =
      out.temp_delta_1h_f !== null && out.temp_delta_1h_f !== undefined
        ? `${out.temp_delta_1h_f >= 0 ? "+" : ""}${Number(out.temp_delta_1h_f).toFixed(1)} F/hr`
        : "Collecting...";
    elOvDelta3hTemp.textContent =
      out.temp_delta_3h_f !== null && out.temp_delta_3h_f !== undefined
        ? `${out.temp_delta_3h_f >= 0 ? "+" : ""}${Number(out.temp_delta_3h_f).toFixed(1)} F/3h`
        : "Collecting...";
    elOvDelta1hRh.textContent =
      out.rh_delta_1h !== null && out.rh_delta_1h !== undefined
        ? `${out.rh_delta_1h >= 0 ? "+" : ""}${Number(out.rh_delta_1h).toFixed(0)} %/hr`
        : "Collecting...";
    elOvDewSpread.textContent =
      out.dew_spread_f !== null && out.dew_spread_f !== undefined
        ? `${Number(out.dew_spread_f).toFixed(1)} F`
        : "--";

    // Outdoor KPI tiles
    elOvOutRh.textContent =
      out.humidity !== null && out.humidity !== undefined
        ? `${Number(out.humidity).toFixed(0)}%`
        : `--%`;
    elOvOutDp.innerHTML =
      out.dew_point_f !== null && out.dew_point_f !== undefined
        ? `Dew Point: ${Number(out.dew_point_f).toFixed(1)}&deg;F (${Number(out.dew_point_c).toFixed(1)}&deg;C)`
        : `Dew Point: --`;

    elOvFeelsF.innerHTML =
      out.feels_like_f !== null && out.feels_like_f !== undefined
        ? `${Number(out.feels_like_f).toFixed(1)}&deg;F`
        : `--.-&deg;F`;
    elOvFeelsC.innerHTML =
      out.feels_like_c !== null && out.feels_like_c !== undefined
        ? `Apparent Temp: ${Number(out.feels_like_c).toFixed(1)}&deg;C`
        : `Apparent Temp: --`;

    elOvWindSpeed.textContent =
      out.wind_avg_mph !== null && out.wind_avg_mph !== undefined
        ? `${Number(out.wind_avg_mph).toFixed(1)} mph ${out.wind_cardinal || ""}`
        : `0.0 mph`;
    const gustTxt =
      out.wind_gust_mph !== null && out.wind_gust_mph !== undefined
        ? `${Number(out.wind_gust_mph).toFixed(1)} mph`
        : `--`;
    const dirTxt =
      out.wind_dir_deg !== null && out.wind_dir_deg !== undefined
        ? `${Number(out.wind_dir_deg).toFixed(0)} deg (${out.wind_cardinal || ""})`
        : `--`;
    elOvWindSub.textContent = `Peak Gust: ${gustTxt} | Bearing: ${dirTxt}`;

    elOvRain24h.textContent = `${Number(out.rain_24h_in || 0).toFixed(2)} in`;
    elOvRainMm.textContent = `${Number(out.rain_24h_mm || 0).toFixed(1)} mm over last 24 hours`;

    elOvUvLux.textContent =
      out.uv_index !== null && out.uv_index !== undefined
        ? `UV ${Number(out.uv_index).toFixed(1)}`
        : `UV 0.0`;
    elOvLuxSub.textContent =
      out.lux !== null && out.lux !== undefined
        ? `Illuminance: ${Number(out.lux).toLocaleString()} lux`
        : `Illuminance: -- lux`;
  }

  // ==========================================================================
  // VIEW 3: IN-PLACE SENSOR GRID PATCHING (ZERO DOM FLASHING OR REORDERING)
  // ==========================================================================
  async function fetchSensors() {
    const res = await fetch("/api/sensors");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    sensorsCache = Array.isArray(data.sensors) ? data.sensors : [];
    patchSensorGridInPlace();
    populateLogSensorDropdown();
  }

  function buildSignalBarsHtml(bars, rating) {
    const tone = (rating || "unknown").toLowerCase();
    let html = `<span class="signal-bars" aria-hidden="true">`;
    for (let i = 1; i <= 4; i++) {
      const active = i <= bars ? `active ${tone}` : "";
      html += `<span class="sig-bar b${i} ${active}"></span>`;
    }
    html += `</span>`;
    return html;
  }

  function createSensorCardElement(sensorKey) {
    const article = document.createElement("article");
    article.className = "sensor-card";
    article.setAttribute("data-sensor-key", sensorKey);
    article.innerHTML = `
      <div class="sensor-card-header">
        <div>
          <div class="sensor-title" data-slot="title"></div>
          <div class="sensor-subtitle" data-slot="subtitle"></div>
        </div>
        <div class="badge-group" data-slot="badges"></div>
      </div>

      <div class="telemetry-primary" data-slot="primary-box">
        <div class="temp-Readout" data-slot="temp"></div>
        <div class="rh-readout" data-slot="rh"></div>
      </div>

      <div class="telemetry-secondary" data-slot="secondary-grid"></div>

      <div class="rf-battery-strip">
        <div class="rf-row">
          <span data-slot="rf-signal"></span>
          <span data-slot="pkt-count" class="text-muted"></span>
        </div>
        <div class="batt-lifecycle-row">
          <span data-slot="batt-days"></span>
          <span data-slot="batt-est" class="text-muted"></span>
        </div>
      </div>

      <div class="sensor-card-footer">
        <span class="last-seen-text" data-slot="last-seen"></span>
        <button class="btn btn-secondary btn-sm" type="button" data-slot="btn-config">
          Configure
        </button>
      </div>
    `;

    const btn = article.querySelector('[data-slot="btn-config"]');
    btn.addEventListener("click", () => openSensorModal(sensorKey));
    return article;
  }

  function updateSensorCardElement(cardEl, sensor) {
    const r = sensor.latest_reading || {};
    const hidden = new Set(sensor.hidden_metrics || []);
    const defaultHardwareId = `${sensor.model} #${sensor.sensor_id}`;
    const displayName =
      sensor.alias && sensor.alias.trim() !== "" ? sensor.alias.trim() : defaultHardwareId;

    cardEl.classList.toggle("untrusted", !sensor.is_trusted);

    const subtitleParts = [`KEY: ${sensor.sensor_key}`];
    if (sensor.alias) subtitleParts.unshift(defaultHardwareId);
    if (sensor.channel) subtitleParts.push(`CH ${sensor.channel}`);

    const slotTitle = cardEl.querySelector('[data-slot="title"]');
    if (slotTitle.textContent !== displayName) slotTitle.textContent = displayName;

    const subtitleText = subtitleParts.join(" | ");
    const slotSub = cardEl.querySelector('[data-slot="subtitle"]');
    if (slotSub.textContent !== subtitleText) slotSub.textContent = subtitleText;

    // Badges
    const locType = (sensor.location_type || "unassigned").toLowerCase();
    let locBadgeClass = "badge";
    if (locType === "indoor") locBadgeClass += " badge-indoor";
    else if (locType === "outdoor") locBadgeClass += " badge-outdoor";
    else if (locType === "ignore") locBadgeClass += " badge-ignore";

    let battBadge = `<span class="badge">BATT: N/A</span>`;
    if (sensor.battery_ok === true) {
      battBadge = `<span class="badge badge-batt-ok">BATT: OK</span>`;
    } else if (sensor.battery_ok === false) {
      battBadge = `<span class="badge badge-batt-low">BATT: LOW</span>`;
    }
    const trustedBadge = sensor.is_trusted ? "" : `<span class="badge">UNTRUSTED</span>`;
    const hiddenCountBadge =
      hidden.size > 0 ? `<span class="badge">${hidden.size} HIDDEN</span>` : "";

    const badgesHtml = `<span class="${locBadgeClass}">${escapeHtml(locType)}</span>${trustedBadge}${hiddenCountBadge}${battBadge}`;
    const slotBadges = cardEl.querySelector('[data-slot="badges"]');
    if (slotBadges.innerHTML !== badgesHtml) slotBadges.innerHTML = badgesHtml;

    // Primary Temp & RH (respecting hidden_metrics)
    const slotTemp = cardEl.querySelector('[data-slot="temp"]');
    let tempHtml = `<span class="text-muted">Temp Hidden</span>`;
    if (!hidden.has("temperature")) {
      tempHtml =
        r.temp_f !== null && r.temp_f !== undefined
          ? `${Number(r.temp_f).toFixed(1)}&deg;F <span class="temp-sub">(${Number(r.temp_c).toFixed(1)}&deg;C)</span>`
          : `--&deg;F`;
    }
    if (slotTemp.innerHTML !== tempHtml) slotTemp.innerHTML = tempHtml;

    const slotRh = cardEl.querySelector('[data-slot="rh"]');
    let rhText = "";
    if (!hidden.has("humidity")) {
      rhText =
        r.humidity !== null && r.humidity !== undefined
          ? `RH ${Number(r.humidity).toFixed(0)}%`
          : `RH --%`;
    } else {
      rhText = "RH Hidden";
    }
    if (slotRh.textContent !== rhText) slotRh.textContent = rhText;

    // Secondary metrics
    const secondaryItems = [];
    if (!hidden.has("temperature") && !hidden.has("humidity") && r.dew_point_f !== null && r.dew_point_f !== undefined) {
      secondaryItems.push({
        label: "Dew Point",
        val: `${Number(r.dew_point_f).toFixed(1)}F (${Number(r.dew_point_c).toFixed(1)}C)`,
      });
    }
    if (!hidden.has("wind") && r.wind_avg_mph !== null && r.wind_avg_mph !== undefined) {
      let windVal = `${Number(r.wind_avg_mph).toFixed(1)} mph`;
      if (r.wind_cardinal) windVal += ` ${escapeHtml(r.wind_cardinal)}`;
      secondaryItems.push({ label: "Wind Avg", val: windVal });
    }
    if (!hidden.has("wind") && r.wind_gust_mph !== null && r.wind_gust_mph !== undefined) {
      secondaryItems.push({
        label: "Wind Gust",
        val: `${Number(r.wind_gust_mph).toFixed(1)} mph`,
      });
    }
    if (!hidden.has("rain") && r.rain_cumulative_in !== null && r.rain_cumulative_in !== undefined) {
      secondaryItems.push({
        label: "Rain Total",
        val: `${Number(r.rain_cumulative_in).toFixed(2)} in (${Number(r.rain_cumulative_mm).toFixed(1)} mm)`,
      });
    }
    if (!hidden.has("uv_light") && (r.uv_index !== null || r.lux !== null)) {
      const parts = [];
      if (r.uv_index !== null && r.uv_index !== undefined) parts.push(`UV ${Number(r.uv_index).toFixed(1)}`);
      if (r.lux !== null && r.lux !== undefined) parts.push(`${Number(r.lux).toFixed(0)} lux`);
      secondaryItems.push({ label: "Solar / Light", val: parts.join(" | ") });
    }

    const secHtml = secondaryItems
      .map(
        (it) => `
        <div class="kv-item">
          <span class="kv-label">${escapeHtml(it.label)}</span>
          <span class="kv-val">${it.val}</span>
        </div>`
      )
      .join("");
    const slotSec = cardEl.querySelector('[data-slot="secondary-grid"]');
    if (slotSec.innerHTML !== secHtml) slotSec.innerHTML = secHtml;
    slotSec.classList.toggle("hidden", secondaryItems.length === 0);

    // RF Signal Quality & Packet Count
    const slotRf = cardEl.querySelector('[data-slot="rf-signal"]');
    const barsHtml = buildSignalBarsHtml(sensor.signal_bars || 0, sensor.signal_quality);
    const rfParts = [];
    if (sensor.last_rssi !== null && sensor.last_rssi !== undefined) {
      rfParts.push(`RSSI ${Number(sensor.last_rssi).toFixed(1)} dB`);
    }
    if (sensor.last_snr !== null && sensor.last_snr !== undefined) {
      rfParts.push(`SNR ${Number(sensor.last_snr).toFixed(1)} dB`);
    }
    if (sensor.last_noise !== null && sensor.last_noise !== undefined) {
      rfParts.push(`Noise ${Number(sensor.last_noise).toFixed(1)} dB`);
    }
    const rfLabel =
      rfParts.length > 0
        ? `${barsHtml}${rfParts.join(" | ")} (${escapeHtml(sensor.signal_quality)})`
        : `${barsHtml}RF level awaiting next burst`;
    if (slotRf.innerHTML !== rfLabel) slotRf.innerHTML = rfLabel;

    const slotPkt = cardEl.querySelector('[data-slot="pkt-count"]');
    const pktText = `${Number(sensor.packet_count || 0).toLocaleString()} pkts`;
    if (slotPkt.textContent !== pktText) slotPkt.textContent = pktText;

    // Battery lifecycle
    const slotBattDays = cardEl.querySelector('[data-slot="batt-days"]');
    const daysText =
      sensor.days_on_current_battery !== null && sensor.days_on_current_battery !== undefined
        ? `Days on current battery: ${sensor.days_on_current_battery}d`
        : `Battery tracking active`;
    if (slotBattDays.textContent !== daysText) slotBattDays.textContent = daysText;

    const slotBattEst = cardEl.querySelector('[data-slot="batt-est"]');
    const estText =
      sensor.estimated_battery_days !== null && sensor.estimated_battery_days !== undefined
        ? `Est. life: ~${sensor.estimated_battery_days}d`
        : `Cycle: monitoring`;
    if (slotBattEst.textContent !== estText) slotBattEst.textContent = estText;

    // Real-time relative seconds counter
    const slotLastSeen = cardEl.querySelector('[data-slot="last-seen"]');
    slotLastSeen.setAttribute("data-utc-ts", sensor.last_seen || "");
    slotLastSeen.textContent = `Last packet: ${formatRelativeSeconds(sensor.last_seen)}`;
  }

  function patchSensorGridInPlace() {
    const showIgnored = elToggleIgnored.checked;
    const visibleSensors = sensorsCache.filter(
      (s) => showIgnored || s.location_type !== "ignore"
    );

    if (visibleSensors.length === 0) {
      elSensorEmpty.classList.remove("hidden");
    } else {
      elSensorEmpty.classList.add("hidden");
    }

    const visibleKeys = new Set(visibleSensors.map((s) => s.sensor_key));

    // Remove cards that are no longer visible
    Array.from(elSensorGrid.children).forEach((child) => {
      const key = child.getAttribute("data-sensor-key");
      if (!visibleKeys.has(key)) {
        elSensorGrid.removeChild(child);
      }
    });

    // Insert or update cards in deterministic order without recreating existing DOM nodes
    visibleSensors.forEach((sensor, idx) => {
      let cardEl = elSensorGrid.querySelector(
        `[data-sensor-key="${CSS.escape(sensor.sensor_key)}"]`
      );
      if (!cardEl) {
        cardEl = createSensorCardElement(sensor.sensor_key);
      }
      updateSensorCardElement(cardEl, sensor);

      const currentAtIdx = elSensorGrid.children[idx];
      if (currentAtIdx !== cardEl) {
        elSensorGrid.insertBefore(cardEl, currentAtIdx || null);
      }
    });
  }

  // Tick the "Last packet: Xs ago" counters every 1 second so the UI feels live
  setInterval(() => {
    document.querySelectorAll('[data-slot="last-seen"]').forEach((el) => {
      const ts = el.getAttribute("data-utc-ts");
      if (ts) {
        el.textContent = `Last packet: ${formatRelativeSeconds(ts)}`;
      }
    });
  }, 1000);

  // ==========================================================================
  // VIEW 2: MULTI-SECTION HISTORICAL CHARTS (SMOOTHED & IN-PLACE UPDATED)
  // ==========================================================================
  const SENSOR_COLORS = [
    "#38bdf8",
    "#34d399",
    "#f59e0b",
    "#a78bfa",
    "#f472b6",
    "#fb7185",
    "#2dd4bf",
    "#facc15",
  ];

  function getOrCreateChart(key, canvasId, yUnitLabel) {
    if (charts[key]) return charts[key];
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === "undefined") return null;

    charts[key] = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels: [], datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: {
          mode: "index",
          intersect: false,
        },
        plugins: {
          legend: {
            labels: { color: "#cbd5e1", font: { size: 12 }, boxWidth: 18 },
          },
        },
        scales: {
          x: {
            ticks: { color: "#64748b", maxTicksLimit: 10 },
            grid: { color: "rgba(36, 50, 75, 0.45)" },
          },
          y: {
            title: {
              display: true,
              text: yUnitLabel,
              color: "#94a3b8",
              font: { size: 11 },
            },
            ticks: { color: "#94a3b8" },
            grid: { color: "rgba(36, 50, 75, 0.45)" },
          },
        },
      },
    });
    return charts[key];
  }

  async function fetchAndUpdateCharts() {
    if (typeof Chart === "undefined") return;
    const hours = elChartHours.value || "24";
    const location = elChartLocation.value || "outdoor";
    const bucket = elChartBucket.value || "auto";
    const showEnsemble = elChartToggleEnsemble.checked;

    const params = new URLSearchParams({
      hours,
      location,
      bucket_minutes: bucket,
    });

    const res = await fetch(`/api/charts?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const includeDate = Number(hours) > 24;
    const labels = (data.timestamps || []).map((iso) =>
      formatLocalShortTime(iso, includeDate)
    );
    const seriesList = Array.isArray(data.series) ? data.series : [];
    const ensemble = data.ensemble || {};

    elChartMetaTemp.textContent = `${data.bucket_minutes}m buckets | ${labels.length} intervals`;
    elChartMetaRh.textContent = `${data.bucket_minutes}m buckets | ${labels.length} intervals`;

    // 1. Temperature datasets
    const tempDatasets = [];
    if (showEnsemble && Array.isArray(ensemble.temp_f) && ensemble.temp_f.some((v) => v !== null)) {
      tempDatasets.push({
        label: "Outdoor Average (Ensemble Mean)",
        data: ensemble.temp_f,
        borderColor: "#ffffff",
        backgroundColor: "rgba(255, 255, 255, 0.1)",
        borderWidth: 2.8,
        borderDash: [6, 4],
        pointRadius: 0,
        pointHoverRadius: 5,
        spanGaps: true,
        cubicInterpolationMode: "monotone",
        tension: 0.35,
        order: 0,
      });
    }

    // 2. Humidity datasets
    const rhDatasets = [];
    if (showEnsemble && Array.isArray(ensemble.humidity) && ensemble.humidity.some((v) => v !== null)) {
      rhDatasets.push({
        label: "Outdoor Average (Ensemble Mean)",
        data: ensemble.humidity,
        borderColor: "#ffffff",
        backgroundColor: "rgba(255, 255, 255, 0.1)",
        borderWidth: 2.8,
        borderDash: [6, 4],
        pointRadius: 0,
        pointHoverRadius: 5,
        spanGaps: true,
        cubicInterpolationMode: "monotone",
        tension: 0.35,
        order: 0,
      });
    }

    // 3. Wind & 4. Rain datasets
    const windDatasets = [];
    const rainDatasets = [];

    seriesList.forEach((s, idx) => {
      const color = SENSOR_COLORS[idx % SENSOR_COLORS.length];

      if (s.has_temp) {
        tempDatasets.push({
          label: s.display_name,
          data: s.temp_f,
          borderColor: color,
          backgroundColor: color,
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 5,
          spanGaps: true,
          cubicInterpolationMode: "monotone",
          tension: 0.35,
          order: 1,
        });
      }

      if (s.has_humidity) {
        rhDatasets.push({
          label: s.display_name,
          data: s.humidity,
          borderColor: color,
          backgroundColor: color,
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 5,
          spanGaps: true,
          cubicInterpolationMode: "monotone",
          tension: 0.35,
          order: 1,
        });
      }

      if (s.has_wind) {
        windDatasets.push({
          label: `${s.display_name} (Avg mph)`,
          data: s.wind_avg_mph,
          borderColor: color,
          backgroundColor: color,
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 5,
          spanGaps: true,
          cubicInterpolationMode: "monotone",
          tension: 0.35,
        });
        if (Array.isArray(s.wind_gust_mph) && s.wind_gust_mph.some((v) => v !== null)) {
          windDatasets.push({
            label: `${s.display_name} (Peak Gust)`,
            data: s.wind_gust_mph,
            borderColor: "#f59e0b",
            backgroundColor: "#f59e0b",
            borderWidth: 1.8,
            borderDash: [4, 4],
            pointRadius: 2,
            pointHoverRadius: 5,
            spanGaps: true,
            cubicInterpolationMode: "monotone",
            tension: 0.35,
          });
        }
      }

      if (s.has_rain) {
        rainDatasets.push({
          label: `${s.display_name} (Rain in)`,
          data: s.rain_in,
          borderColor: "#38bdf8",
          backgroundColor: "rgba(56, 189, 248, 0.16)",
          fill: true,
          borderWidth: 2.2,
          pointRadius: 2,
          pointHoverRadius: 5,
          spanGaps: true,
          cubicInterpolationMode: "monotone",
          tension: 0.25,
        });
      }
    });

    const cTemp = getOrCreateChart("temp", "canvas-chart-temp", "Degrees F");
    if (cTemp) {
      cTemp.data.labels = labels;
      cTemp.data.datasets = tempDatasets;
      cTemp.update("none");
    }

    const cRh = getOrCreateChart("rh", "canvas-chart-rh", "Relative Humidity (%)");
    if (cRh) {
      cRh.data.labels = labels;
      cRh.data.datasets = rhDatasets;
      cRh.update("none");
    }

    const cWind = getOrCreateChart("wind", "canvas-chart-wind", "Wind Speed (mph)");
    if (cWind) {
      cWind.data.labels = labels;
      cWind.data.datasets = windDatasets;
      cWind.update("none");
    }

    const cRain = getOrCreateChart("rain", "canvas-chart-rain", "Accumulated Rain (in)");
    if (cRain) {
      cRain.data.labels = labels;
      cRain.data.datasets = rainDatasets;
      cRain.update("none");
    }
  }

  // ==========================================================================
  // VIEW 4: RAW SQLITE TELEMETRY LOG EXPLORER
  // ==========================================================================
  function populateLogSensorDropdown() {
    const currentVal = elLogSensor.value;
    const options = [`<option value="">All Registered Sensors</option>`];
    sensorsCache.forEach((s) => {
      const label = s.alias
        ? `${s.alias} (${s.model} #${s.sensor_id})`
        : `${s.model} #${s.sensor_id}`;
      const selected = s.sensor_key === currentVal ? "selected" : "";
      options.push(
        `<option value="${escapeHtml(s.sensor_key)}" ${selected}>${escapeHtml(label)}</option>`
      );
    });
    if (elLogSensor.innerHTML !== options.join("")) {
      elLogSensor.innerHTML = options.join("");
    }
  }

  async function loadLogExplorerTable() {
    const sensorKey = elLogSensor.value;
    const hours = elLogHours.value || "24";
    const limit = elLogLimit.value || "250";

    const params = new URLSearchParams({ hours, limit });
    if (sensorKey) params.set("sensor_key", sensorKey);

    const res = await fetch(`/api/readings?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const rows = Array.isArray(data.readings) ? data.readings : [];

    elLogSummary.textContent = `${rows.length} records loaded`;
    logsLoadedOnce = true;

    if (rows.length === 0) {
      elLogTableBody.innerHTML = `
        <tr>
          <td colspan="10" class="text-center text-muted">No telemetry packets found inside the selected UTC window.</td>
        </tr>`;
      return;
    }

    elLogTableBody.innerHTML = rows
      .map((r) => {
        const localTime = formatLocalShortTime(r.timestamp, true);
        const tempCell =
          r.temp_f !== null && r.temp_f !== undefined
            ? `${Number(r.temp_f).toFixed(1)}F / ${Number(r.temp_c).toFixed(1)}C`
            : "--";
        const rhDpParts = [];
        if (r.humidity !== null && r.humidity !== undefined) rhDpParts.push(`${Number(r.humidity).toFixed(0)}%`);
        if (r.dew_point_f !== null && r.dew_point_f !== undefined) rhDpParts.push(`DP ${Number(r.dew_point_f).toFixed(1)}F`);
        const rhCell = rhDpParts.length > 0 ? rhDpParts.join(" | ") : "--";

        let windCell = "--";
        if (r.wind_avg_mph !== null && r.wind_avg_mph !== undefined) {
          windCell = `${Number(r.wind_avg_mph).toFixed(1)} mph`;
          if (r.wind_cardinal) windCell += ` ${escapeHtml(r.wind_cardinal)}`;
        }
        const rainCell =
          r.rain_cumulative_in !== null && r.rain_cumulative_in !== undefined
            ? `${Number(r.rain_cumulative_in).toFixed(2)} in (${Number(r.rain_cumulative_mm).toFixed(1)} mm)`
            : "--";

        const rfParts = [];
        if (r.rssi !== null && r.rssi !== undefined) rfParts.push(`${Number(r.rssi).toFixed(1)}dB`);
        if (r.snr !== null && r.snr !== undefined) rfParts.push(`SNR ${Number(r.snr).toFixed(1)}dB`);
        const rfCell = rfParts.length > 0 ? rfParts.join(" / ") : "--";

        let battCell = "--";
        if (r.battery_ok === true) battCell = `<span class="badge badge-batt-ok">OK</span>`;
        else if (r.battery_ok === false) battCell = `<span class="badge badge-batt-low">LOW</span>`;

        return `
          <tr>
            <td class="mono">${escapeHtml(localTime)}</td>
            <td class="mono text-muted">${escapeHtml(r.timestamp)}</td>
            <td><strong>${escapeHtml(r.display_name)}</strong></td>
            <td><span class="badge">${escapeHtml(r.location_type || "unassigned")}</span></td>
            <td class="mono">${tempCell}</td>
            <td class="mono">${rhCell}</td>
            <td class="mono">${windCell}</td>
            <td class="mono">${rainCell}</td>
            <td class="mono">${rfCell}</td>
            <td>${battCell}</td>
          </tr>`;
      })
      .join("");
  }

  // ==========================================================================
  // SENSOR CONFIGURATION MODAL (WITH GRANULAR METRIC CHECKBOXES)
  // ==========================================================================
  function openSensorModal(sensorKey) {
    const sensor = sensorsCache.find((s) => s.sensor_key === sensorKey);
    if (!sensor) return;

    elModalSensorKey.value = sensor.sensor_key;
    elModalSubtitle.textContent = `${sensor.model} #${sensor.sensor_id} (Key: ${sensor.sensor_key})`;
    elModalAlias.value = sensor.alias || "";
    elModalLocation.value = sensor.location_type || "unassigned";
    elModalTrusted.checked = Boolean(sensor.is_trusted);
    elModalBattReset.checked = false;

    const hiddenSet = new Set(sensor.hidden_metrics || []);
    document.querySelectorAll('input[name="vis_metric"]').forEach((cb) => {
      cb.checked = !hiddenSet.has(cb.value);
    });

    elModalFeedback.classList.add("hidden");
    elModalBackdrop.classList.remove("hidden");
    elModalAlias.focus();
  }

  function closeSensorModal() {
    elModalBackdrop.classList.add("hidden");
  }

  async function handleSaveSensorModal(evt) {
    evt.preventDefault();
    const sensorKey = elModalSensorKey.value;
    if (!sensorKey) return;

    const hiddenMetrics = [];
    document.querySelectorAll('input[name="vis_metric"]').forEach((cb) => {
      if (!cb.checked) {
        hiddenMetrics.push(cb.value);
      }
    });

    const payload = {
      alias: elModalAlias.value.trim(),
      location_type: elModalLocation.value,
      is_trusted: elModalTrusted.checked,
      hidden_metrics: hiddenMetrics,
      mark_battery_replaced: elModalBattReset.checked,
    };

    try {
      const res = await fetch(`/api/sensors/${encodeURIComponent(sensorKey)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      closeSensorModal();
      await refreshAll(true);
    } catch (err) {
      elModalFeedback.textContent = err.message || "Failed to save sensor configuration.";
      elModalFeedback.className = "form-feedback error";
      elModalFeedback.classList.remove("hidden");
    }
  }

  // ==========================================================================
  // POLLING & EVENT BINDINGS
  // ==========================================================================
  async function refreshAll(forceActiveViewRefresh) {
    elSyncDot.classList.add("syncing");
    try {
      await Promise.all([fetchStatus(), fetchOverview(), fetchSensors()]);
      if (activeTab === "charts" || forceActiveViewRefresh) {
        await fetchAndUpdateCharts();
      }
      if ((activeTab === "logs" && !logsLoadedOnce) || forceActiveViewRefresh) {
        await loadLogExplorerTable();
      }
      const now = new Date();
      elLastSync.textContent = now.toTimeString().split(" ")[0];
    } catch (err) {
      console.error("SkyScry telemetry sync error:", err);
    } finally {
      setTimeout(() => elSyncDot.classList.remove("syncing"), 250);
    }
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".view-panel").forEach((p) => p.classList.remove("active"));

      btn.classList.add("active");
      activeTab = btn.getAttribute("data-tab");
      const targetPanel = document.getElementById(`view-${activeTab}`);
      if (targetPanel) targetPanel.classList.add("active");

      if (activeTab === "charts") {
        fetchAndUpdateCharts();
      } else if (activeTab === "logs" && !logsLoadedOnce) {
        loadLogExplorerTable();
      }
    });
  });

  elToggleIgnored.addEventListener("change", patchSensorGridInPlace);
  elBtnRefresh.addEventListener("click", () => refreshAll(true));

  elChartHours.addEventListener("change", fetchAndUpdateCharts);
  elChartLocation.addEventListener("change", fetchAndUpdateCharts);
  elChartBucket.addEventListener("change", fetchAndUpdateCharts);
  elChartToggleEnsemble.addEventListener("change", fetchAndUpdateCharts);

  elBtnRunLogQuery.addEventListener("click", loadLogExplorerTable);
  elBtnExportCsv.addEventListener("click", () => {
    const params = new URLSearchParams({
      hours: elLogHours.value || "24",
      limit: elLogLimit.value || "500",
      format: "csv",
    });
    if (elLogSensor.value) params.set("sensor_key", elLogSensor.value);
    window.location.href = `/api/readings?${params.toString()}`;
  });
  elBtnExportJson.addEventListener("click", () => {
    const params = new URLSearchParams({
      hours: elLogHours.value || "24",
      limit: elLogLimit.value || "500",
    });
    if (elLogSensor.value) params.set("sensor_key", elLogSensor.value);
    window.open(`/api/readings?${params.toString()}`, "_blank");
  });

  elBtnCloseModal.addEventListener("click", closeSensorModal);
  elBtnCancelModal.addEventListener("click", closeSensorModal);
  elBtnClearAlias.addEventListener("click", () => {
    elModalAlias.value = "";
    elModalAlias.focus();
  });
  elModalBackdrop.addEventListener("click", (evt) => {
    if (evt.target === elModalBackdrop) closeSensorModal();
  });
  elModalForm.addEventListener("submit", handleSaveSensorModal);

  // Initial render
  elHeroIcon.innerHTML = getAnimatedWeatherSvg("partly-cloudy-day");
  refreshAll(false);
  setInterval(() => refreshAll(false), POLL_INTERVAL_MS);
})();

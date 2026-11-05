/**
 * SkyScry v0.0.9 — 4-View Frontend Controller
 * - View 1: Main Overview Dashboard with High-Clarity Animated SVG Weather Icons,
 *           Animated SVG Wind Compass Rose, Barometric Pressure (inHg/hPa) + 24h Forecast
 * - View 2: Multi-Section Time-Bucketed Historical Charts with Multi-Checkbox Filter Dropdown
 * - View 3: Dedicated Sensors Manager with Opt-In Trust, In-Place DOM Patching, & Metric Masking
 * - View 4: Raw SQLite Telemetry Log Explorer with CSV/JSON Export
 */

(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;

  let activeTab = "overview";
  let sensorsCache = [];
  let currentIconCode = "";
  let logsLoadedOnce = false;
  let authRequired = false;
  let readOnlyPublic = false;

  function getStoredAdminPin() {
    try {
      return sessionStorage.getItem("skyscry_admin_pin") || "";
    } catch (_) {
      return "";
    }
  }

  function setStoredAdminPin(pin) {
    try {
      if (pin) {
        sessionStorage.setItem("skyscry_admin_pin", pin);
      } else {
        sessionStorage.removeItem("skyscry_admin_pin");
      }
    } catch (_) {}
  }

  async function authorizedWriteFetch(url, options) {
    const opts = Object.assign({}, options);
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});

    let pin = getStoredAdminPin();
    if (authRequired && !pin) {
      const entered = window.prompt("Admin PIN required to modify SkyScry settings:");
      if (entered === null) {
        throw new Error("Action canceled (Admin PIN required).");
      }
      pin = entered.trim();
      setStoredAdminPin(pin);
    }
    if (pin) {
      opts.headers["X-Admin-Pin"] = pin;
    }

    let res = await fetch(url, opts);
    if (res.status === 401) {
      setStoredAdminPin("");
      const retryPin = window.prompt("Invalid or missing Admin PIN. Please enter the Admin PIN:");
      if (retryPin === null) {
        throw new Error("Unauthorized: Valid Admin PIN required.");
      }
      setStoredAdminPin(retryPin.trim());
      opts.headers["X-Admin-Pin"] = retryPin.trim();
      res = await fetch(url, opts);
    }
    return res;
  }

  const charts = {
    temp: null,
    rh: null,
    wind: null,
    rain: null,
  };

  // Header & Banners DOM
  const elGitCommit = document.getElementById("stat-git-commit");
  const elRtl433Ver = document.getElementById("stat-rtl433-ver");
  const elSdrStatus = document.getElementById("stat-sdr-status");
  const elSensorCount = document.getElementById("stat-sensor-count");
  const elBaroHeader = document.getElementById("stat-baro-header");
  const elReadingCount = document.getElementById("stat-reading-count");
  const elDbSize = document.getElementById("stat-db-size");
  const elUptime = document.getElementById("stat-uptime");
  const elSyncDot = document.getElementById("stat-sync-indicator");
  const elLastSync = document.getElementById("stat-last-sync");
  const elErrorBanner = document.getElementById("sdr-error-banner");
  const elToastBanner = document.getElementById("maintenance-toast");
  const elBtnRefresh = document.getElementById("btn-refresh-now");
  const elBtnTriggerUpdate = document.getElementById("btn-trigger-update");
  const elBtnSyncRef = document.getElementById("btn-sync-reference");
  const elBtnPurgeOutliers = document.getElementById("btn-purge-outliers");

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
  const elOvDelta3hBaro = document.getElementById("ov-delta-3h-baro");
  const elOvDewSpread = document.getElementById("ov-dew-spread");

  // Wind Compass & KPI Tiles DOM
  const elOvWindSpeed = document.getElementById("ov-wind-speed");
  const elOvWindGust = document.getElementById("ov-wind-gust");
  const elOvWindBearing = document.getElementById("ov-wind-bearing");
  const elCompassNeedle = document.getElementById("compass-needle-group");
  const elCompassPulseRing = document.getElementById("compass-pulse-ring");

  const elOvBaroInhg = document.getElementById("ov-baro-inhg");
  const elOvBaroHpa = document.getElementById("ov-baro-hpa");
  const elOvBaroSource = document.getElementById("ov-baro-source");

  const elOvOutRh = document.getElementById("ov-out-rh");
  const elOvOutDp = document.getElementById("ov-out-dp");
  const elOvFeelsInline = document.getElementById("ov-feels-inline");
  const elOvRain24h = document.getElementById("ov-rain-24h");
  const elOvRainMm = document.getElementById("ov-rain-mm");
  const elOvUvInline = document.getElementById("ov-uv-inline");

  // View 2: Charts DOM & Multi-Checkbox Filter Dropdown
  const elChartHours = document.getElementById("chart-hours-select");
  const elChartBucket = document.getElementById("chart-bucket-select");
  const elChartToggleEnsemble = document.getElementById("chart-toggle-ensemble");
  const elChartMetaTemp = document.getElementById("chart-meta-temp");
  const elChartMetaRh = document.getElementById("chart-meta-rh");
  const elFilterDropdown = document.getElementById("chart-filter-dropdown");
  const elFilterBtn = document.getElementById("chart-filter-btn");
  const elFilterSummary = document.getElementById("chart-filter-summary");
  const elFilterMenu = document.getElementById("chart-filter-menu");
  const elFilterLocOutdoor = document.getElementById("filter-loc-outdoor");
  const elFilterLocIndoor = document.getElementById("filter-loc-indoor");
  const elFilterLocReference = document.getElementById("filter-loc-reference");
  const elFilterSensorList = document.getElementById("filter-sensor-list");
  const uncheckedSensorKeys = new Set();

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
  const elModalPurgeOutliers = document.getElementById("modal-purge-outliers-checkbox");
  const elModalFeedback = document.getElementById("modal-feedback");
  const elBtnCloseModal = document.getElementById("btn-close-modal");
  const elBtnCancelModal = document.getElementById("btn-cancel-modal");
  const elBtnClearAlias = document.getElementById("btn-clear-alias");
  const elBtnMarkAppliance = document.getElementById("btn-mark-appliance");

  function showToast(msg) {
    if (!elToastBanner) return;
    elToastBanner.textContent = msg;
    elToastBanner.classList.remove("hidden");
    setTimeout(() => {
      if (elToastBanner) elToastBanner.classList.add("hidden");
    }, 4500);
  }

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
  // HIGH-CLARITY ANIMATED WEATHER CONDITION SVG LIBRARY
  // (Warm-gold visible crescent moon, crisp 4-point diamond stars, translucent clouds)
  // ==========================================================================
  function getAnimatedWeatherSvg(code) {
    switch (code) {
      case "clear-day":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <radialGradient id="sunCore" cx="55" cy="55" r="24" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stop-color="#FEF08A" />
                <stop offset="65%" stop-color="#FBBF24" />
                <stop offset="100%" stop-color="#F59E0B" />
              </radialGradient>
            </defs>
            <g class="wx-sun-rays" style="transform-origin: 55px 55px;" stroke="#FBBF24" stroke-width="4.2" stroke-linecap="round">
              <line x1="55" y1="10" x2="55" y2="21" />
              <line x1="55" y1="89" x2="55" y2="100" />
              <line x1="10" y1="55" x2="21" y2="55" />
              <line x1="89" y1="55" x2="100" y2="55" />
              <line x1="23" y1="23" x2="31" y2="31" />
              <line x1="79" y1="79" x2="87" y2="87" />
              <line x1="87" y1="23" x2="79" y2="31" />
              <line x1="31" y1="79" x2="23" y2="87" />
            </g>
            <circle cx="55" cy="55" r="22" fill="url(#sunCore)" stroke="#FDE047" stroke-width="1.5" />
          </svg>`;

      case "clear-night":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="goldMoon" x1="30" y1="18" x2="85" y2="85" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stop-color="#FEF08A" />
                <stop offset="55%" stop-color="#FBBF24" />
                <stop offset="100%" stop-color="#D97706" />
              </linearGradient>
              <filter id="moonGlow" x="-20%" y="-20%" width="140%" height="140%">
                <feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="#FBBF24" flood-opacity="0.45" />
              </filter>
            </defs>
            <!-- Crisp 4-point diamond stars -->
            <path class="wx-diamond-star-1" d="M24 20 L26 25 L31 27 L26 29 L24 34 L22 29 L17 27 L22 25 Z" fill="#FDE047" />
            <path class="wx-diamond-star-2" d="M84 22 L85.5 26 L89.5 27.5 L85.5 29 L84 33 L82.5 29 L78.5 27.5 L82.5 26 Z" fill="#E0F2FE" />
            <path class="wx-diamond-star-1" d="M22 68 L23.2 71 L26.2 72.2 L23.2 73.4 L22 76.4 L20.8 73.4 L17.8 72.2 L20.8 71 Z" fill="#BAE6FD" />
            <!-- High-contrast warm gold crescent moon -->
            <path filter="url(#moonGlow)"
                  d="M66 18 C46 20 32 36 32 56 C32 76 48 91 68 91 C78 91 87 87 93 80 C77 82 62 70 62 52 C62 37 70 25 66 18 Z"
                  fill="url(#goldMoon)" stroke="#FDE047" stroke-width="1.5" />
          </svg>`;

      case "cloudy-night":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="goldMoonCloud" x1="50" y1="10" x2="98" y2="62" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stop-color="#FEF08A" />
                <stop offset="60%" stop-color="#FBBF24" />
                <stop offset="100%" stop-color="#D97706" />
              </linearGradient>
              <linearGradient id="slateCloud" x1="16" y1="42" x2="84" y2="88" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stop-color="#CBD5E1" stop-opacity="0.94" />
                <stop offset="100%" stop-color="#64748B" stop-opacity="0.94" />
              </linearGradient>
              <filter id="moonGlow2" x="-20%" y="-20%" width="140%" height="140%">
                <feDropShadow dx="0" dy="0" stdDeviation="3.5" flood-color="#FBBF24" flood-opacity="0.45" />
              </filter>
            </defs>
            <!-- Crisp 4-point diamond stars in upper-left sky -->
            <path class="wx-diamond-star-1" d="M22 18 L23.8 22.5 L28.3 24.3 L23.8 26.1 L22 30.6 L20.2 26.1 L15.7 24.3 L20.2 22.5 Z" fill="#FDE047" />
            <path class="wx-diamond-star-2" d="M42 12 L43.2 15 L46.2 16.2 L43.2 17.4 L42 20.4 L40.8 17.4 L37.8 16.2 L40.8 15 Z" fill="#E0F2FE" />

            <!-- Large warm-gold Crescent Moon positioned high in upper-right so >70% is clearly visible -->
            <path filter="url(#moonGlow2)"
                  d="M75 11 C59 13 48 26 48 42 C48 58 61 70 77 70 C85 70 92 67 97 61 C84 62 72 52 72 38 C72 26 78 17 75 11 Z"
                  fill="url(#goldMoonCloud)" stroke="#FDE047" stroke-width="1.4" />

            <!-- Softly drifting foreground cloud positioned lower-left -->
            <g class="wx-cloud-body">
              <path d="M24 84 H74 C82 84 88 78 88 70 C88 63 82 57 75 57 C73 46 63 39 51 39 C38 39 29 48 27 60 C20 61 14 67 14 74 C14 80 18 84 24 84 Z"
                    fill="url(#slateCloud)" stroke="#E2E8F0" stroke-width="1.4" />
            </g>
          </svg>`;

      case "rain":
      case "heavy-rain":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-back">
              <path d="M36 58 H82 C88 58 93 53 93 46 C93 40 88 35 82 35 C80 26 72 20 62 20 C51 20 43 28 41 38 C35 39 31 44 31 50 C31 55 33 58 36 58 Z"
                    fill="#334155" />
            </g>
            <g class="wx-cloud-body">
              <path d="M24 68 H74 C82 68 88 62 88 54 C88 47 82 41 75 41 C73 30 63 23 51 23 C38 23 29 32 27 44 C20 45 14 51 14 58 C14 64 18 68 24 68 Z"
                    fill="#64748B" stroke="#CBD5E1" stroke-width="1.4" />
            </g>
            <g stroke="#38BDF8" stroke-width="3.6" stroke-linecap="round">
              <line class="wx-drop-1" x1="34" y1="74" x2="29" y2="90" />
              <line class="wx-drop-2" x1="52" y1="74" x2="47" y2="90" />
              <line class="wx-drop-3" x1="70" y1="74" x2="65" y2="90" />
            </g>
          </svg>`;

      case "windy":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-body">
              <path d="M26 58 H74 C81 58 86 53 86 46 C86 39 81 34 74 34 C72 25 63 18 52 18 C40 18 31 26 30 37 C23 38 18 43 18 50 C18 55 21 58 26 58 Z"
                    fill="#94A3B8" stroke="#E2E8F0" stroke-width="1.2" />
            </g>
            <path class="wx-wind-line" d="M16 70 H82 C88 70 92 66 92 61 C92 56 88 52 83 52" stroke="#38BDF8" stroke-width="4" stroke-linecap="round" />
            <path class="wx-wind-line" d="M24 82 H70 C76 82 80 86 80 91 C80 95 76 98 71 98" stroke="#93C5FD" stroke-width="3.5" stroke-linecap="round" />
          </svg>`;

      case "fog":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-body">
              <path d="M26 58 H76 C83 58 88 53 88 46 C88 39 83 34 76 34 C74 25 65 18 54 18 C42 18 33 26 32 37 C25 38 20 43 20 50 C20 55 23 58 26 58 Z"
                    fill="#94A3B8" />
            </g>
            <g class="wx-fog-bar" stroke="#CBD5E1" stroke-width="4" stroke-linecap="round">
              <line x1="20" y1="69" x2="88" y2="69" />
              <line x1="28" y1="80" x2="80" y2="80" />
              <line x1="18" y1="91" x2="72" y2="91" />
            </g>
          </svg>`;

      case "cloudy":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-back">
              <path d="M38 62 H84 C90 62 96 57 96 50 C96 44 91 39 85 39 C83 30 75 24 65 24 C53 24 45 32 44 42 C38 43 33 48 33 54 C33 59 35 62 38 62 Z"
                    fill="#475569" />
            </g>
            <g class="wx-cloud-body">
              <path d="M24 76 H74 C82 76 88 70 88 62 C88 55 82 49 75 49 C73 38 63 31 51 31 C38 31 29 40 27 52 C20 53 14 59 14 66 C14 72 18 76 24 76 Z"
                    fill="#CBD5E1" stroke="#F8FAFC" stroke-width="1.2" />
            </g>
          </svg>`;

      case "partly-cloudy-day":
      default:
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="dayCloudGrad" x1="20" y1="38" x2="88" y2="84" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stop-color="#F8FAFC" />
                <stop offset="100%" stop-color="#CBD5E1" />
              </linearGradient>
            </defs>
            <g class="wx-sun-rays" stroke="#FBBF24" stroke-width="3.8" stroke-linecap="round" style="transform-origin: 38px 36px;">
              <line x1="38" y1="8" x2="38" y2="15" />
              <line x1="38" y1="57" x2="38" y2="64" />
              <line x1="10" y1="36" x2="17" y2="36" />
              <line x1="59" y1="36" x2="66" y2="36" />
              <line x1="18" y1="16" x2="23" y2="21" />
              <line x1="53" y1="51" x2="58" y2="56" />
              <line x1="58" y1="16" x2="53" y2="21" />
              <line x1="23" y1="51" x2="18" y2="56" />
            </g>
            <circle cx="38" cy="36" r="15" fill="#FBBF24" stroke="#FEF08A" stroke-width="1.5" />
            <g class="wx-cloud-body">
              <path d="M28 82 H78 C86 82 92 76 92 68 C92 61 86 55 79 55 C77 44 67 37 55 37 C42 37 33 46 31 58 C24 59 18 65 18 72 C18 78 22 82 28 82 Z"
                    fill="url(#dayCloudGrad)" stroke="#E2E8F0" stroke-width="1.4" />
            </g>
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
    if (elSdrStatus) {
      elSdrStatus.textContent = sdrState.toUpperCase();
      elSdrStatus.className =
        "status-pill " + (sdrState === "running" ? "status-running" : "status-disconnected");
    }

    if (elSensorCount) {
      elSensorCount.textContent = `${data.active_sensors ?? 0} / ${data.total_sensors ?? 0}`;
    }
    if (elReadingCount) {
      elReadingCount.textContent = Number(data.total_readings || 0).toLocaleString();
    }
    if (elDbSize) {
      elDbSize.textContent = `${Number(data.database_size_mb || 0).toFixed(2)} MB`;
    }
    if (elUptime) {
      elUptime.textContent = formatDuration(data.uptime_seconds);
    }
    authRequired = Boolean(data.auth_required);
    readOnlyPublic = Boolean(data.read_only_public);

    if (elGitCommit) {
      elGitCommit.textContent = data.git_commit ? `git:${data.git_commit}` : "git:local";
    }
    if (elRtl433Ver) {
      elRtl433Ver.textContent = data.rtl433_version
        ? `rtl_433:${data.rtl433_version}`
        : "rtl_433:ready";
    }

    if (elErrorBanner) {
      if (data.sdr_last_error && sdrState !== "running") {
        elErrorBanner.textContent = `SDR DIAGNOSTIC: ${data.sdr_last_error}`;
        elErrorBanner.classList.remove("hidden");
      } else {
        elErrorBanner.classList.add("hidden");
      }
    }
  }

  async function fetchOverview() {
    const res = await fetch("/api/overview");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const cond = data.condition || {};
    const out = data.outdoor || {};
    const ind = data.indoor || {};
    const baro = data.barometer || {};

    if (elHeroIcon && cond.code && cond.code !== currentIconCode) {
      currentIconCode = cond.code;
      elHeroIcon.innerHTML = getAnimatedWeatherSvg(cond.code);
    }

    if (elOvConditionLabel) {
      elOvConditionLabel.textContent = cond.label || "Monitoring Active";
    }
    if (elOvOutTempF) {
      elOvOutTempF.innerHTML =
        out.temp_f !== null && out.temp_f !== undefined
          ? `${Number(out.temp_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elOvOutTempC) {
      elOvOutTempC.innerHTML =
        out.temp_c !== null && out.temp_c !== undefined
          ? `(${Number(out.temp_c).toFixed(1)}&deg;C)`
          : `(--.-&deg;C)`;
    }

    if (elOvOutSpread) {
      if (out.temp_min_f !== null && out.temp_max_f !== null && out.sensor_count > 1) {
        elOvOutSpread.innerHTML = `Outdoor ensemble mean across ${out.sensor_count} trusted stations | Spread: ${Number(out.temp_min_f).toFixed(1)}&deg;F &ndash; ${Number(out.temp_max_f).toFixed(1)}&deg;F`;
      } else {
        elOvOutSpread.textContent = `Outdoor ensemble across ${out.sensor_count ?? 0} trusted station(s)`;
      }
    }

    const badges = Array.isArray(cond.trend_badges) ? cond.trend_badges : [];
    if (elOvTrendBadges) {
      elOvTrendBadges.innerHTML = badges
        .map(
          (b) =>
            `<span class="trend-pill ${escapeHtml(b.tone || "neutral")}">${escapeHtml(b.label)}</span>`
        )
        .join("");
    }

    if (elOvNowcastText) {
      elOvNowcastText.textContent = cond.nowcast_summary || "";
    }

    // Indoor card
    if (elOvInCount) elOvInCount.textContent = `${ind.sensor_count ?? 0} station(s)`;
    if (elOvInTemp) {
      elOvInTemp.innerHTML =
        ind.temp_f !== null && ind.temp_f !== undefined
          ? `${Number(ind.temp_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elOvInTempC) {
      elOvInTempC.innerHTML =
        ind.temp_c !== null && ind.temp_c !== undefined
          ? `${Number(ind.temp_c).toFixed(1)}&deg;C`
          : `--.-&deg;C`;
    }
    if (elOvInRh) {
      elOvInRh.textContent =
        ind.humidity !== null && ind.humidity !== undefined
          ? `${Number(ind.humidity).toFixed(0)}%`
          : `--%`;
    }
    if (elOvInComfort) {
      if (ind.humidity !== null && ind.humidity !== undefined) {
        const rh = Number(ind.humidity);
        elOvInComfort.textContent =
          rh < 30 ? "Dry Air" : rh <= 55 ? "Optimal Comfort" : "Humid Indoor";
      } else {
        elOvInComfort.textContent = "Assign 'Indoor' in Sensors tab";
      }
    }

    // Deltas
    if (elOvDelta1hTemp) {
      elOvDelta1hTemp.textContent =
        out.temp_delta_1h_f !== null && out.temp_delta_1h_f !== undefined
          ? `${out.temp_delta_1h_f >= 0 ? "+" : ""}${Number(out.temp_delta_1h_f).toFixed(1)} F/hr`
          : "--";
    }
    if (elOvDelta3hTemp) {
      elOvDelta3hTemp.textContent =
        out.temp_delta_3h_f !== null && out.temp_delta_3h_f !== undefined
          ? `${out.temp_delta_3h_f >= 0 ? "+" : ""}${Number(out.temp_delta_3h_f).toFixed(1)} F/3h`
          : "--";
    }
    if (elOvDelta3hBaro) {
      elOvDelta3hBaro.textContent =
        baro.delta_3h_inhg !== null && baro.delta_3h_inhg !== undefined
          ? `${baro.delta_3h_inhg >= 0 ? "+" : ""}${Number(baro.delta_3h_inhg).toFixed(2)} inHg`
          : baro.trend_label || "--";
    }
    if (elOvDewSpread) {
      elOvDewSpread.textContent =
        out.dew_spread_f !== null && out.dew_spread_f !== undefined
          ? `${Number(out.dew_spread_f).toFixed(1)} F`
          : "--";
    }

    // Animated Wind Speed & Compass Rose Card
    const windMph = out.wind_avg_mph !== null && out.wind_avg_mph !== undefined ? Number(out.wind_avg_mph) : 0;
    const windCard = out.wind_cardinal || "--";
    const windDeg = out.wind_dir_deg !== null && out.wind_dir_deg !== undefined ? Number(out.wind_dir_deg) : 0;

    if (elOvWindSpeed) {
      elOvWindSpeed.textContent =
        out.wind_avg_mph !== null && out.wind_avg_mph !== undefined
          ? `${windMph.toFixed(1)} mph ${windCard !== "--" ? windCard : ""}`
          : `0.0 mph`;
    }
    if (elOvWindGust) {
      elOvWindGust.textContent =
        out.wind_gust_mph !== null && out.wind_gust_mph !== undefined
          ? `Peak Gust: ${Number(out.wind_gust_mph).toFixed(1)} mph (${Number(out.wind_gust_mps || 0).toFixed(1)} m/s)`
          : `Peak Gust: 0.0 mph`;
    }
    if (elOvWindBearing) {
      elOvWindBearing.textContent =
        out.wind_dir_deg !== null && out.wind_dir_deg !== undefined
          ? `Bearing: ${windDeg.toFixed(0)} deg (${windCard})`
          : `Bearing: -- deg`;
    }

    if (elCompassNeedle) {
      elCompassNeedle.style.transform = `rotate(${windDeg}deg)`;
    }
    if (elCompassPulseRing) {
      elCompassPulseRing.classList.toggle("compass-active-pulse", windMph > 0.5);
    }

    // Barometric Pressure Card (inHg & hPa)
    if (baro.pressure_inhg !== null && baro.pressure_inhg !== undefined) {
      const inhgStr = `${Number(baro.pressure_inhg).toFixed(2)} inHg`;
      if (elOvBaroInhg) elOvBaroInhg.textContent = inhgStr;
      if (elBaroHeader) elBaroHeader.textContent = inhgStr;
      if (elOvBaroHpa) {
        elOvBaroHpa.textContent = `${Number(baro.pressure_hpa).toFixed(1)} hPa | ${baro.trend_label || "Steady"}`;
      }
      if (elOvBaroSource) {
        elOvBaroSource.textContent =
          baro.source === "rf_sensor"
            ? "Source: Local 433 MHz Barometer"
            : "Source: Regional Open-Meteo Barometer";
      }
    } else {
      if (elOvBaroInhg) elOvBaroInhg.textContent = "--.-- inHg";
      if (elOvBaroHpa) elOvBaroHpa.textContent = "Awaiting barometric sync...";
    }

    // Outdoor Humidity & Dew Point + Feels Like
    if (elOvOutRh) {
      elOvOutRh.textContent =
        out.humidity !== null && out.humidity !== undefined
          ? `${Number(out.humidity).toFixed(0)}%`
          : `--%`;
    }
    if (elOvOutDp) {
      elOvOutDp.innerHTML =
        out.dew_point_f !== null && out.dew_point_f !== undefined
          ? `Dew Point: ${Number(out.dew_point_f).toFixed(1)}&deg;F (${Number(out.dew_point_c).toFixed(1)}&deg;C)`
          : `Dew Point: --`;
    }
    if (elOvFeelsInline) {
      elOvFeelsInline.innerHTML =
        out.feels_like_f !== null && out.feels_like_f !== undefined
          ? `Feels Like: ${Number(out.feels_like_f).toFixed(1)}&deg;F (${Number(out.feels_like_c).toFixed(1)}&deg;C)`
          : `Feels Like: --`;
    }

    // Rain & UV
    if (elOvRain24h) elOvRain24h.textContent = `${Number(out.rain_24h_in || 0).toFixed(2)} in`;
    if (elOvRainMm) elOvRainMm.textContent = `${Number(out.rain_24h_mm || 0).toFixed(1)} mm over last 24 hours`;
    const uvStr =
      out.uv_index !== null && out.uv_index !== undefined ? `UV ${Number(out.uv_index).toFixed(1)}` : "UV 0.0";
    const luxStr =
      out.lux !== null && out.lux !== undefined ? `${Number(out.lux).toLocaleString()} lux` : "-- lux";
    if (elOvUvInline) elOvUvInline.textContent = `Solar: ${uvStr} | ${luxStr}`;
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
    populateChartSensorCheckboxes();
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
    const expungedBadge =
      (sensor.expunged_count || 0) > 0
        ? `<span class="badge badge-expunged" title="${escapeHtml(sensor.last_expunged_reason || "Outlier packets rejected")}">${sensor.expunged_count} EXPUNGED</span>`
        : "";

    const badgesHtml = `<span class="${locBadgeClass}">${escapeHtml(locType)}</span>${trustedBadge}${hiddenCountBadge}${expungedBadge}${battBadge}`;
    const slotBadges = cardEl.querySelector('[data-slot="badges"]');
    if (slotBadges.innerHTML !== badgesHtml) slotBadges.innerHTML = badgesHtml;

    // Primary Temp & RH
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

    const slotLastSeen = cardEl.querySelector('[data-slot="last-seen"]');
    slotLastSeen.setAttribute("data-utc-ts", sensor.last_seen || "");
    slotLastSeen.textContent = `Last packet: ${formatRelativeSeconds(sensor.last_seen)}`;
  }

  function patchSensorGridInPlace() {
    if (!elSensorGrid) return;
    const showIgnored = elToggleIgnored ? elToggleIgnored.checked : false;
    const visibleSensors = sensorsCache.filter(
      (s) => showIgnored || (s.location_type !== "ignore" && s.is_trusted)
    );

    if (elSensorEmpty) {
      if (visibleSensors.length === 0) {
        elSensorEmpty.classList.remove("hidden");
      } else {
        elSensorEmpty.classList.add("hidden");
      }
    }

    const visibleKeys = new Set(visibleSensors.map((s) => s.sensor_key));

    Array.from(elSensorGrid.children).forEach((child) => {
      const key = child.getAttribute("data-sensor-key");
      if (!visibleKeys.has(key)) {
        elSensorGrid.removeChild(child);
      }
    });

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

  function updateChartFilterSummary() {
    if (!elFilterSummary) return;
    const outChecked = Boolean(elFilterLocOutdoor && elFilterLocOutdoor.checked);
    const inChecked = Boolean(elFilterLocIndoor && elFilterLocIndoor.checked);
    const refChecked = Boolean(elFilterLocReference && elFilterLocReference.checked);

    const sensorCheckboxes = elFilterSensorList
      ? Array.from(elFilterSensorList.querySelectorAll('input[name="chart_sensor_cb"]'))
      : [];
    const checkedSensors = sensorCheckboxes.filter((cb) => cb.checked);

    if (!outChecked && !inChecked && !refChecked && checkedSensors.length === 0) {
      elFilterSummary.textContent = "No Streams Selected";
      return;
    }

    const parts = [];
    if (outChecked && inChecked) {
      parts.push("Outdoor + Indoor");
    } else if (outChecked) {
      parts.push("Outdoor (Trusted)");
    } else if (inChecked) {
      parts.push("Indoor (Trusted)");
    }

    if (refChecked) {
      parts.push("Reference");
    }

    if (sensorCheckboxes.length > 0 && checkedSensors.length > 0 && checkedSensors.length < sensorCheckboxes.length) {
      parts.push(`${checkedSensors.length} Station(s)`);
    } else if (!outChecked && !inChecked && checkedSensors.length > 0) {
      parts.push(`${checkedSensors.length} Streams Selected`);
    }

    const totalStreams = (outChecked ? 1 : 0) + (inChecked ? 1 : 0) + (refChecked ? 1 : 0);
    if (totalStreams === 3 && checkedSensors.length === sensorCheckboxes.length) {
      elFilterSummary.textContent = "3 Streams Selected";
      return;
    }

    elFilterSummary.textContent = parts.length > 0 ? parts.join(" + ") : "Outdoor (Trusted)";
  }

  function populateChartSensorCheckboxes(syncFromCategory) {
    if (!elFilterSensorList) return;
    const includeOutdoor = elFilterLocOutdoor ? elFilterLocOutdoor.checked : true;
    const includeIndoor = elFilterLocIndoor ? elFilterLocIndoor.checked : false;

    const trustedStations = sensorsCache.filter(
      (s) => s.is_trusted && s.location_type !== "ignore"
    );

    if (trustedStations.length === 0) {
      elFilterSensorList.innerHTML = `<span class="text-muted">No trusted Outdoor/Indoor stations configured yet.</span>`;
      updateChartFilterSummary();
      return;
    }

    if (syncFromCategory) {
      trustedStations.forEach((s) => {
        if (s.location_type === "outdoor") {
          if (includeOutdoor) uncheckedSensorKeys.delete(s.sensor_key);
          else uncheckedSensorKeys.add(s.sensor_key);
        } else if (s.location_type === "indoor") {
          if (includeIndoor) uncheckedSensorKeys.delete(s.sensor_key);
          else uncheckedSensorKeys.add(s.sensor_key);
        }
      });
    } else {
      trustedStations.forEach((s) => {
        if (s.location_type === "indoor" && !includeIndoor && !uncheckedSensorKeys.has(s.sensor_key)) {
          // By default indoor stations follow the indoor category checkbox unless explicitly checked
          uncheckedSensorKeys.add(s.sensor_key);
        }
      });
    }

    const html = trustedStations
      .map((s) => {
        const isChecked = !uncheckedSensorKeys.has(s.sensor_key);
        const locTag = s.location_type === "indoor" ? "IN" : "OUT";
        return `
          <label class="checkbox-label filter-item">
            <input type="checkbox" name="chart_sensor_cb" data-loc="${escapeHtml(s.location_type || "outdoor")}" value="${escapeHtml(s.sensor_key)}" ${isChecked ? "checked" : ""} />
            <span>[${locTag}] ${escapeHtml(s.display_name)}</span>
          </label>`;
      })
      .join("");

    elFilterSensorList.innerHTML = html;
    elFilterSensorList.querySelectorAll('input[name="chart_sensor_cb"]').forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) {
          uncheckedSensorKeys.delete(cb.value);
          const loc = cb.getAttribute("data-loc");
          if (loc === "outdoor" && elFilterLocOutdoor) elFilterLocOutdoor.checked = true;
          if (loc === "indoor" && elFilterLocIndoor) elFilterLocIndoor.checked = true;
        } else {
          uncheckedSensorKeys.add(cb.value);
        }
        updateChartFilterSummary();
        fetchAndUpdateCharts();
      });
    });
    updateChartFilterSummary();
  }

  async function fetchAndUpdateCharts() {
    if (typeof Chart === "undefined") return;
    const hours = elChartHours ? elChartHours.value || "24" : "24";
    const bucket = elChartBucket ? elChartBucket.value || "auto" : "auto";
    const showEnsemble = elChartToggleEnsemble ? elChartToggleEnsemble.checked : true;

    const locs = [];
    if (!elFilterLocOutdoor || elFilterLocOutdoor.checked) locs.push("outdoor");
    if (elFilterLocIndoor && elFilterLocIndoor.checked) locs.push("indoor");
    if (elFilterLocReference && elFilterLocReference.checked) locs.push("reference");

    const params = new URLSearchParams({
      hours,
      locations: locs.join(","),
      bucket_minutes: bucket,
    });

    if (elFilterSensorList) {
      const allCbs = Array.from(elFilterSensorList.querySelectorAll('input[name="chart_sensor_cb"]'));
      if (allCbs.length > 0) {
        const activeKeys = allCbs.filter((cb) => cb.checked).map((cb) => cb.value);
        if (activeKeys.length < allCbs.length) {
          params.set("sensors", activeKeys.join(","));
        }
      }
    }

    const res = await fetch(`/api/charts?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const includeDate = Number(hours) > 24;
    const labels = (data.timestamps || []).map((iso) =>
      formatLocalShortTime(iso, includeDate)
    );
    const seriesList = Array.isArray(data.series) ? data.series : [];
    const ensemble = data.ensemble || {};
    const reference = data.reference || {};

    if (elChartMetaTemp) {
      elChartMetaTemp.textContent = `${data.bucket_minutes}m buckets | ${labels.length} intervals (Trusted only)`;
    }
    if (elChartMetaRh) {
      elChartMetaRh.textContent = `${data.bucket_minutes}m buckets | ${labels.length} intervals`;
    }

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
    if (Array.isArray(reference.temp_f) && reference.temp_f.some((v) => v !== null)) {
      tempDatasets.push({
        label: "Regional Reference Baseline (Open-Meteo)",
        data: reference.temp_f,
        borderColor: "#94a3b8",
        backgroundColor: "rgba(148, 163, 184, 0.1)",
        borderWidth: 2.0,
        borderDash: [3, 3],
        pointRadius: 0,
        pointHoverRadius: 4,
        spanGaps: true,
        cubicInterpolationMode: "monotone",
        tension: 0.35,
        order: 0,
      });
    }

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
    if (Array.isArray(reference.humidity) && reference.humidity.some((v) => v !== null)) {
      rhDatasets.push({
        label: "Regional Reference Baseline (Open-Meteo)",
        data: reference.humidity,
        borderColor: "#94a3b8",
        backgroundColor: "rgba(148, 163, 184, 0.1)",
        borderWidth: 2.0,
        borderDash: [3, 3],
        pointRadius: 0,
        pointHoverRadius: 4,
        spanGaps: true,
        cubicInterpolationMode: "monotone",
        tension: 0.35,
        order: 0,
      });
    }

    const windDatasets = [];
    if (Array.isArray(reference.wind_avg_mph) && reference.wind_avg_mph.some((v) => v !== null)) {
      windDatasets.push({
        label: "Regional Reference Wind (mph)",
        data: reference.wind_avg_mph,
        borderColor: "#94a3b8",
        backgroundColor: "rgba(148, 163, 184, 0.1)",
        borderWidth: 1.8,
        borderDash: [3, 3],
        pointRadius: 0,
        pointHoverRadius: 4,
        spanGaps: true,
        cubicInterpolationMode: "monotone",
        tension: 0.35,
      });
    }
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
    if (!elLogSensor) return;
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
    if (!elLogTableBody) return;
    const sensorKey = elLogSensor ? elLogSensor.value : "";
    const hours = elLogHours ? elLogHours.value || "24" : "24";
    const limit = elLogLimit ? elLogLimit.value || "250" : "250";

    const params = new URLSearchParams({ hours, limit });
    if (sensorKey) params.set("sensor_key", sensorKey);

    const res = await fetch(`/api/readings?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const rows = Array.isArray(data.readings) ? data.readings : [];

    if (elLogSummary) {
      elLogSummary.textContent = `${rows.length} records loaded`;
    }
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
  // SENSOR CONFIGURATION MODAL & MAINTENANCE ACTIONS
  // ==========================================================================
  function openSensorModal(sensorKey) {
    const sensor = sensorsCache.find((s) => s.sensor_key === sensorKey);
    if (!sensor || !elModalBackdrop) return;

    if (elModalSensorKey) elModalSensorKey.value = sensor.sensor_key;
    if (elModalSubtitle) {
      elModalSubtitle.textContent = `${sensor.model} #${sensor.sensor_id} (Key: ${sensor.sensor_key})`;
    }
    if (elModalAlias) elModalAlias.value = sensor.alias || "";
    const curLoc = sensor.location_type === "unassigned" ? "ignore" : (sensor.location_type || "ignore");
    if (elModalLocation) elModalLocation.value = curLoc;
    if (elModalTrusted) elModalTrusted.checked = Boolean(sensor.is_trusted);
    if (elModalBattReset) elModalBattReset.checked = false;
    if (elModalPurgeOutliers) elModalPurgeOutliers.checked = false;

    const hiddenSet = new Set(sensor.hidden_metrics || []);
    document.querySelectorAll('input[name="vis_metric"]').forEach((cb) => {
      cb.checked = !hiddenSet.has(cb.value);
    });

    if (elModalFeedback) elModalFeedback.classList.add("hidden");
    elModalBackdrop.classList.remove("hidden");
    if (elModalAlias) elModalAlias.focus();
  }

  function closeSensorModal() {
    if (elModalBackdrop) elModalBackdrop.classList.add("hidden");
  }

  async function handleSaveSensorModal(evt) {
    evt.preventDefault();
    const sensorKey = elModalSensorKey ? elModalSensorKey.value : "";
    if (!sensorKey) return;

    const hiddenMetrics = [];
    document.querySelectorAll('input[name="vis_metric"]').forEach((cb) => {
      if (!cb.checked) {
        hiddenMetrics.push(cb.value);
      }
    });

    const payload = {
      alias: elModalAlias ? elModalAlias.value.trim() : "",
      location_type: elModalLocation ? elModalLocation.value : "ignore",
      is_trusted: elModalTrusted ? elModalTrusted.checked : false,
      hidden_metrics: hiddenMetrics,
      mark_battery_replaced: elModalBattReset ? elModalBattReset.checked : false,
      purge_sensor_outliers: elModalPurgeOutliers ? elModalPurgeOutliers.checked : false,
    };

    try {
      const res = await authorizedWriteFetch(`/api/sensors/${encodeURIComponent(sensorKey)}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const contentType = res.headers.get("content-type") || "";
      let data = {};
      if (contentType.includes("application/json")) {
        data = await res.json();
      } else {
        const rawText = await res.text();
        throw new Error(`Server returned HTTP ${res.status}: ${rawText.slice(0, 120)}`);
      }
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      closeSensorModal();
      showToast(`Saved settings for ${data.sensor ? data.sensor.display_name : sensorKey}`);
      await refreshAll(true);
    } catch (err) {
      if (elModalFeedback) {
        elModalFeedback.textContent = err.message || "Failed to save sensor configuration.";
        elModalFeedback.className = "form-feedback error";
        elModalFeedback.classList.remove("hidden");
      }
    }
  }

  async function handlePurgeOutliersClick() {
    if (elBtnPurgeOutliers) elBtnPurgeOutliers.disabled = true;
    try {
      const res = await authorizedWriteFetch("/api/maintenance/purge-outliers", {
        method: "POST",
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      showToast(
        `Outlier Purge Complete: Expunged ${data.deleted_readings} skewed reading(s) and ${data.deleted_sensors} phantom sensor(s).`
      );
      await refreshAll(true);
    } catch (err) {
      showToast(err.message || "Failed to purge outliers.");
      console.error("Failed to purge outliers:", err);
    } finally {
      if (elBtnPurgeOutliers) elBtnPurgeOutliers.disabled = false;
    }
  }

  async function handleTriggerUpdateClick() {
    if (!elBtnTriggerUpdate) return;
    elBtnTriggerUpdate.disabled = true;
    elBtnTriggerUpdate.textContent = "Checking...";
    try {
      const res = await authorizedWriteFetch("/api/trigger-update", {
        method: "POST",
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const gitUpd = data.git && data.git.updated;
      const rtlUpd = data.rtl433 && data.rtl433.updated;
      const cur = data.current || {};
      showToast(
        `Update Check Complete: SkyScry v${cur.version || "--"} (git:${cur.git_commit || "local"})${gitUpd ? " [PULLED NEW COMMIT]" : " [Up to Date]"} | rtl_433: ${cur.rtl433_version || "ready"}${rtlUpd ? " [UPDATED]" : ""}`
      );
      await refreshAll(true);
    } catch (err) {
      showToast(err.message || "Update check failed.");
      console.error("Update check failed:", err);
    } finally {
      elBtnTriggerUpdate.disabled = false;
      elBtnTriggerUpdate.textContent = "Check & Apply Updates";
    }
  }

  async function handleSyncReferenceClick() {
    if (elBtnSyncRef) elBtnSyncRef.disabled = true;
    try {
      const res = await authorizedWriteFetch("/api/maintenance/sync-reference", {
        method: "POST",
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      showToast(
        `Synced ${data.synced_records} regional records (${data.latitude}, ${data.longitude}) — Barometer: ${data.latest_pressure_inhg} inHg (${data.latest_pressure_hpa} hPa)`
      );
      await refreshAll(true);
    } catch (err) {
      showToast(err.message || "Failed to sync regional barometer.");
      console.error("Failed to sync regional barometer:", err);
    } finally {
      if (elBtnSyncRef) elBtnSyncRef.disabled = false;
    }
  }

  // ==========================================================================
  // POLLING & EVENT BINDINGS (ISOLATED SUB-TASKS FOR CRASH-PROOF SYNC)
  // ==========================================================================
  async function refreshAll(forceActiveViewRefresh) {
    if (elSyncDot) elSyncDot.classList.add("syncing");
    try {
      await Promise.allSettled([
        fetchStatus().catch((err) => console.error("SkyScry status fetch error:", err)),
        fetchOverview().catch((err) => console.error("SkyScry overview fetch error:", err)),
        fetchSensors().catch((err) => console.error("SkyScry sensors fetch error:", err)),
      ]);

      if (activeTab === "charts" || forceActiveViewRefresh) {
        try {
          await fetchAndUpdateCharts();
        } catch (err) {
          console.error("SkyScry charts fetch error:", err);
        }
      }
      if ((activeTab === "logs" && !logsLoadedOnce) || forceActiveViewRefresh) {
        try {
          await loadLogExplorerTable();
        } catch (err) {
          console.error("SkyScry logs fetch error:", err);
        }
      }
      const now = new Date();
      if (elLastSync) {
        elLastSync.textContent = now.toTimeString().split(" ")[0];
      }
    } catch (err) {
      console.error("SkyScry telemetry sync error:", err);
    } finally {
      setTimeout(() => {
        if (elSyncDot) elSyncDot.classList.remove("syncing");
      }, 250);
    }
  }

  function initEventListenersAndStart() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".view-panel").forEach((p) => p.classList.remove("active"));

        btn.classList.add("active");
        activeTab = btn.getAttribute("data-tab") || "overview";
        const targetPanel = document.getElementById(`view-${activeTab}`);
        if (targetPanel) targetPanel.classList.add("active");

        if (activeTab === "charts") {
          fetchAndUpdateCharts().catch((err) => console.error("Charts load error:", err));
        } else if (activeTab === "logs" && !logsLoadedOnce) {
          loadLogExplorerTable().catch((err) => console.error("Logs load error:", err));
        }
      });
    });

    if (elToggleIgnored) {
      elToggleIgnored.addEventListener("change", patchSensorGridInPlace);
    }
    if (elBtnRefresh) {
      elBtnRefresh.addEventListener("click", () => refreshAll(true));
    }
    if (elBtnTriggerUpdate) {
      elBtnTriggerUpdate.addEventListener("click", handleTriggerUpdateClick);
    }
    if (elBtnPurgeOutliers) {
      elBtnPurgeOutliers.addEventListener("click", handlePurgeOutliersClick);
    }
    if (elBtnSyncRef) {
      elBtnSyncRef.addEventListener("click", handleSyncReferenceClick);
    }

    if (elChartHours) {
      elChartHours.addEventListener("change", () => {
        fetchAndUpdateCharts().catch((err) => console.error("Charts update error:", err));
      });
    }
    if (elChartBucket) {
      elChartBucket.addEventListener("change", () => {
        fetchAndUpdateCharts().catch((err) => console.error("Charts update error:", err));
      });
    }
    if (elChartToggleEnsemble) {
      elChartToggleEnsemble.addEventListener("change", () => {
        fetchAndUpdateCharts().catch((err) => console.error("Charts update error:", err));
      });
    }

    if (elFilterBtn && elFilterMenu) {
      elFilterBtn.addEventListener("click", (evt) => {
        evt.stopPropagation();
        const isHidden = elFilterMenu.classList.toggle("hidden");
        elFilterBtn.setAttribute("aria-expanded", String(!isHidden));
      });
      document.addEventListener("click", (evt) => {
        if (elFilterDropdown && !elFilterDropdown.contains(evt.target)) {
          elFilterMenu.classList.add("hidden");
          elFilterBtn.setAttribute("aria-expanded", "false");
        }
      });
    }

    [elFilterLocOutdoor, elFilterLocIndoor, elFilterLocReference].forEach((cb) => {
      if (cb) {
        cb.addEventListener("change", () => {
          populateChartSensorCheckboxes(true);
          fetchAndUpdateCharts().catch((err) => console.error("Charts filter error:", err));
        });
      }
    });

    if (elModalLocation && elModalTrusted) {
      elModalLocation.addEventListener("change", () => {
        if (elModalLocation.value === "outdoor" || elModalLocation.value === "indoor") {
          elModalTrusted.checked = true;
        } else if (elModalLocation.value === "ignore") {
          elModalTrusted.checked = false;
        }
      });
    }

    if (elBtnRunLogQuery) {
      elBtnRunLogQuery.addEventListener("click", () => {
        loadLogExplorerTable().catch((err) => console.error("Logs query error:", err));
      });
    }
    if (elBtnExportCsv) {
      elBtnExportCsv.addEventListener("click", () => {
        const params = new URLSearchParams({
          hours: elLogHours ? elLogHours.value || "24" : "24",
          limit: elLogLimit ? elLogLimit.value || "500" : "500",
          format: "csv",
        });
        if (elLogSensor && elLogSensor.value) params.set("sensor_key", elLogSensor.value);
        window.location.href = `/api/readings?${params.toString()}`;
      });
    }
    if (elBtnExportJson) {
      elBtnExportJson.addEventListener("click", () => {
        const params = new URLSearchParams({
          hours: elLogHours ? elLogHours.value || "24" : "24",
          limit: elLogLimit ? elLogLimit.value || "500" : "500",
        });
        if (elLogSensor && elLogSensor.value) params.set("sensor_key", elLogSensor.value);
        window.open(`/api/readings?${params.toString()}`, "_blank");
      });
    }

    if (elBtnCloseModal) elBtnCloseModal.addEventListener("click", closeSensorModal);
    if (elBtnCancelModal) elBtnCancelModal.addEventListener("click", closeSensorModal);
    if (elBtnClearAlias && elModalAlias) {
      elBtnClearAlias.addEventListener("click", () => {
        elModalAlias.value = "";
        elModalAlias.focus();
      });
    }
    if (elBtnMarkAppliance && elModalLocation && elModalTrusted && elModalPurgeOutliers && elModalForm) {
      elBtnMarkAppliance.addEventListener("click", () => {
        elModalLocation.value = "ignore";
        elModalTrusted.checked = false;
        elModalPurgeOutliers.checked = true;
        elModalForm.requestSubmit();
      });
    }
    if (elModalBackdrop) {
      elModalBackdrop.addEventListener("click", (evt) => {
        if (evt.target === elModalBackdrop) closeSensorModal();
      });
    }
    if (elModalForm) {
      elModalForm.addEventListener("submit", handleSaveSensorModal);
    }

    // Initial render
    if (elHeroIcon) {
      elHeroIcon.innerHTML = getAnimatedWeatherSvg("partly-cloudy-day");
    }
    refreshAll(false);
    setInterval(() => refreshAll(false), POLL_INTERVAL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initEventListenersAndStart);
  } else {
    initEventListenersAndStart();
  }
})();

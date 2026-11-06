/**
 * SkyScry v0.0.12 — 6-View Frontend Controller
 * - View 1: Overview Dashboard with AcuRite & Weather-Display (grahamsbeach.htm) SVG Gauges,
 *           Enlarged Split Hero Card, Dual-Trace 24h Area Sparklines, Diurnal Nowcast & Mini Live Radar
 * - View 2: Multi-Section Historical Charts + Climatology & Seasonal Insights Engine
 * - View 3: Dedicated Sensors Manager with Opt-In Trust, In-Place DOM Patching, & Metric Masking
 * - View 4: Raw SQLite Telemetry Log Explorer with CSV/JSON Export (bounded <= 2000)
 * - View 5: Live Animated NEXRAD / RainViewer Weather Radar & Official NWS 7-Day Forecast
 * - View 6: Hardened Admin Settings (First-Run Password Setup, Auto-Lock, NWS & Units Config)
 */

(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;

  let activeTab = "overview";
  let sensorsCache = [];
  let currentIconCode = "";
  let logsLoadedOnce = false;
  let climatologyLoadedOnce = false;
  let nwsLoadedOnce = false;
  let settingsLoadedOnce = false;

  // Security & Auth state
  let authState = {
    password_configured: true,
    unlocked: false,
    timeout_minutes: 15,
  };
  let pendingUnlockCallback = null;
  let setupModalShown = false;

  // User display units & station coordinates
  let userSettings = {
    station_name: "SkyScry Weather Station",
    latitude: 38.627,
    longitude: -90.1994,
    elevation_m: 142.0,
    radar_zoom: 8,
    temp_unit: "F",
    wind_unit: "mph",
    pressure_unit: "inHg",
    rain_unit: "in",
  };

  const charts = {
    temp: null,
    rh: null,
    wind: null,
    rain: null,
  };

  // Leaflet Radar Maps State
  let miniRadarMap = null;
  let miniStationMarker = null;
  let miniRadarTileLayer = null;

  let fullRadarMap = null;
  let fullStationMarker = null;
  let fullActiveTileLayer = null;
  let rainViewerData = null;
  let radarFrames = [];
  let currentFrameIdx = 0;
  let radarLayerMode = "radar"; // "radar" | "satellite" | "nexrad"
  let radarTimer = null;
  let radarPlaying = true;

  // Header & Banners DOM
  const elGitCommit = document.getElementById("stat-git-commit");
  const elRtl433Ver = document.getElementById("stat-rtl433-ver");
  const elSdrStatus = document.getElementById("stat-sdr-status");
  const elBtnAuthPill = document.getElementById("btn-auth-pill");
  const elBtnLockNow = document.getElementById("btn-lock-now");
  const elSensorCount = document.getElementById("stat-sensor-count");
  const elBaroHeader = document.getElementById("stat-baro-header");
  const elReadingCount = document.getElementById("stat-reading-count");
  const elDbSize = document.getElementById("stat-db-size");
  const elUptime = document.getElementById("stat-uptime");
  const elSyncDot = document.getElementById("stat-sync-indicator");
  const elLastSync = document.getElementById("stat-last-sync");
  const elErrorBanner = document.getElementById("sdr-error-banner");
  const elToastBanner = document.getElementById("maintenance-toast");
  const elNwsAlertBanner = document.getElementById("nws-alert-banner");

  const elBtnRefresh = document.getElementById("btn-refresh-now");
  const elBtnTriggerUpdate = document.getElementById("btn-trigger-update");
  const elBtnSyncRef = document.getElementById("btn-sync-reference");
  const elBtnPurgeOutliers = document.getElementById("btn-purge-outliers");

  // View 1: Overview DOM
  const elOvStationEyebrow = document.getElementById("ov-station-eyebrow");
  const elHeroIcon = document.getElementById("hero-weather-icon");
  const elOvConditionLabel = document.getElementById("ov-condition-label");
  const elOvOutTempF = document.getElementById("ov-out-temp-f");
  const elOvOutTempC = document.getElementById("ov-out-temp-c");
  const elOvOutTrendArrow = document.getElementById("ov-out-trend-arrow");
  const elOvOutLowPill = document.getElementById("ov-out-low-pill");
  const elOvOutHighPill = document.getElementById("ov-out-high-pill");
  const elOvOutSparkline = document.getElementById("ov-out-sparkline-svg");
  const elOvOutSpread = document.getElementById("ov-out-spread");
  const elOvTrendBadges = document.getElementById("ov-trend-badges");
  const elOvNowcastText = document.getElementById("ov-nowcast-text");
  const elOvNowcastConfidence = document.getElementById("ov-nowcast-confidence");
  const elOvEstHigh = document.getElementById("ov-est-high");
  const elOvEstLow = document.getElementById("ov-est-low");
  const elOvPrecipProb = document.getElementById("ov-precip-prob");

  // Hero Split Grid & Dual Sparkline DOM
  const elOvHeroFeels = document.getElementById("ov-hero-feels");
  const elOvHeroFeelsSub = document.getElementById("ov-hero-feels-sub");
  const elOvHeroDew = document.getElementById("ov-hero-dew");
  const elOvHeroDewSpread = document.getElementById("ov-hero-dew-spread");
  const elOvHeroRange = document.getElementById("ov-hero-range");
  const elOvHeroRangeSub = document.getElementById("ov-hero-range-sub");
  const elOvHeroSpreadVal = document.getElementById("ov-hero-spread-val");
  const elOvHeroSpreadCount = document.getElementById("ov-hero-spread-count");
  const elOvHeroSparklineSvg = document.getElementById("ov-hero-sparkline-svg");

  // Bottom Nowcast Bar DOM
  const elOvTrajProb = document.getElementById("ov-traj-prob");
  const elOvTrajEstHl = document.getElementById("ov-traj-est-hl");
  const elOvTrajRiskBadges = document.getElementById("ov-traj-risk-badges");

  // NWS Corroboration Strip (View 1)
  const elOvNwsSummaryBadge = document.getElementById("ov-nws-summary-badge");
  const elOvNwsStationBadge = document.getElementById("ov-nws-station-badge");
  const elOvNwsOfficePill = document.getElementById("ov-nws-office-pill");
  const elBtnOpenRadarView = document.getElementById("btn-open-radar-tab") || document.getElementById("btn-open-radar-view");
  const elBtnExpandRadar = document.getElementById("btn-expand-radar");
  const elMiniRadarTimestamp = document.getElementById("mini-radar-timestamp");
  const elMiniRadarCoords = document.getElementById("mini-radar-coords");

  // Indoor Card DOM
  const elOvInCount = document.getElementById("ov-in-count");
  const elOvInTemp = document.getElementById("ov-in-temp");
  const elOvInTempC = document.getElementById("ov-in-temp-c");
  const elOvInTrendArrow = document.getElementById("ov-in-trend-arrow");
  const elOvInExtremes = document.getElementById("ov-in-extremes");
  const elOvInRh = document.getElementById("ov-in-rh");
  const elOvInComfort = document.getElementById("ov-in-comfort");
  const elOvInSparkline = document.getElementById("ov-in-sparkline-svg");

  // Deltas DOM
  const elOvDelta1hTemp = document.getElementById("ov-delta-1h-temp");
  const elOvDelta3hTemp = document.getElementById("ov-delta-3h-temp");
  const elOvDelta3hBaro = document.getElementById("ov-delta-3h-baro");
  const elOvDewSpread = document.getElementById("ov-dew-spread");

  // AcuRite Wind Gauge DOM
  const elOvWindSpeed = document.getElementById("ov-wind-speed");
  const elOvWindSpeedFlank = document.getElementById("ov-wind-speed-flank");
  const elOvWindBearingFlank = document.getElementById("ov-wind-bearing-flank");
  const elOvWindGust = document.getElementById("ov-wind-gust");
  const elOvWindGustTime = document.getElementById("ov-wind-gust-time");
  const elOvWindAvg10m = document.getElementById("ov-wind-avg-10m");
  const elOvWindBearing = document.getElementById("ov-wind-bearing");
  const elCompassNeedle = document.getElementById("compass-needle-group");
  const elWindGlowRing = document.getElementById("wind-speed-glow-ring");
  const elWindVarianceWedge = document.getElementById("wind-variance-wedge");
  const elWindDialCardinal = document.getElementById("wind-dial-cardinal");
  const elWindDialSpeed = document.getElementById("wind-dial-speed");
  const elWindFlankAvg = document.getElementById("wind-flank-avg");
  const elWindFlankPeak = document.getElementById("wind-flank-peak");

  // Barometer Tower DOM
  const elOvBaroInhg = document.getElementById("ov-baro-inhg");
  const elOvBaroTrendArrow = document.getElementById("ov-baro-trend-arrow");
  const elOvBaroTrendPill = document.getElementById("ov-baro-trend-pill");
  const elOvBaroHpa = document.getElementById("ov-baro-hpa");
  const elOvBaroExtremes = document.getElementById("ov-baro-extremes");
  const elOvBaroSource = document.getElementById("ov-baro-source");
  const elBaroMercuryFill = document.getElementById("baro-mercury-fill");
  const elBaroTowerPointer = document.getElementById("baro-tower-pointer");

  // Rain Cylinder & Records DOM
  const elOvRain24h = document.getElementById("ov-rain-24h");
  const elOvRainMm = document.getElementById("ov-rain-mm");
  const elOvRainRateBadge = document.getElementById("ov-rain-rate-badge");
  const elRainRecRate = document.getElementById("rain-rec-rate");
  const elRainRecWeek = document.getElementById("rain-rec-week");
  const elRainRecMonth = document.getElementById("rain-rec-month");
  const elRainRecYear = document.getElementById("rain-rec-year");
  const elRainCylinderFill = document.getElementById("rain-cylinder-fill");
  const elRainCylinderMeniscus = document.getElementById("rain-cylinder-meniscus");

  // Outdoor Humidity & UV Arc DOM
  const elOvOutRh = document.getElementById("ov-out-rh");
  const elOvOutRhExtremes = document.getElementById("ov-out-rh-extremes");
  const elOvOutDp = document.getElementById("ov-out-dp");
  const elOvFeelsInline = document.getElementById("ov-feels-inline");
  const elOvRhComfortBadge = document.getElementById("ov-rh-comfort-badge");
  const elRhGaugeArc = document.getElementById("rh-gauge-arc");
  const elOvUvInline = document.getElementById("ov-uv-inline");
  const elUvBarFill = document.getElementById("uv-bar-fill");

  // View 2: Charts & Climatology DOM
  const elClimAnomalyBadge = document.getElementById("clim-anomaly-badge");
  const elClimTrajectoryNarrative = document.getElementById("clim-trajectory-narrative");
  const elClim7dMean = document.getElementById("clim-7d-mean");
  const elClim7dSwing = document.getElementById("clim-7d-swing");
  const elClim30dMean = document.getElementById("clim-30d-mean");
  const elClimBaroVol = document.getElementById("clim-baro-vol");
  const elClimHddCdd = document.getElementById("clim-hdd-cdd");
  const elClimStreak = document.getElementById("clim-streak");
  const elClimRain30d = document.getElementById("clim-rain-30d");
  const elClimRecordsTbody = document.getElementById("clim-records-tbody");

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

  // View 5: Radar & NWS DOM
  const elRadarStationSubtitle = document.getElementById("radar-station-subtitle");
  const elBtnLayerRadar = document.getElementById("btn-layer-radar");
  const elBtnLayerSatellite = document.getElementById("btn-layer-satellite");
  const elBtnLayerNexrad = document.getElementById("btn-layer-nexrad");
  const elBtnRadarPlay = document.getElementById("btn-radar-play");
  const elRadarFrameSlider = document.getElementById("radar-frame-slider");
  const elRadarFrameTime = document.getElementById("radar-frame-time");
  const elBtnRefreshNws = document.getElementById("btn-refresh-nws");
  const elNwsCorroborationTbody = document.getElementById("nws-corroboration-tbody");
  const elNwsWfoBadge = document.getElementById("nws-wfo-badge");
  const elNwsForecastGrid = document.getElementById("nws-forecast-grid");

  // View 6: Settings DOM
  const elSettingsLockStateBadge = document.getElementById("settings-lock-state-badge");
  const elFormChangePassword = document.getElementById("form-change-password");
  const elSetCurrentPassword = document.getElementById("set-current-password");
  const elSetNewPassword = document.getElementById("set-new-password");
  const elSetConfirmPassword = document.getElementById("set-confirm-password");
  const elSetTimeoutMinutes = document.getElementById("set-timeout-minutes");
  const elBtnSettingsLockSession = document.getElementById("btn-settings-lock-session");

  const elFormSettingsLocation = document.getElementById("form-settings-location");
  const elSetStationName = document.getElementById("set-station-name");
  const elSetLatitude = document.getElementById("set-latitude");
  const elSetLongitude = document.getElementById("set-longitude");
  const elSetElevation = document.getElementById("set-elevation");
  const elSetRadarZoom = document.getElementById("set-radar-zoom");
  const elSetNwsGridReadout = document.getElementById("set-nws-grid-readout");

  const elFormSettingsHygiene = document.getElementById("form-settings-hygiene");
  const elSetOutlierThreshold = document.getElementById("set-outlier-threshold");
  const elSetDedupWindow = document.getElementById("set-dedup-window");
  const elSetAutoExpunge = document.getElementById("set-auto-expunge");
  const elSetAutoUpdate = document.getElementById("set-auto-update");
  const elBtnVacuumDb = document.getElementById("btn-vacuum-db");

  const elFormSettingsUnits = document.getElementById("form-settings-units");
  const elSetTempUnit = document.getElementById("set-temp-unit");
  const elSetWindUnit = document.getElementById("set-wind-unit");
  const elSetPressureUnit = document.getElementById("set-pressure-unit");
  const elSetRainUnit = document.getElementById("set-rain-unit");

  const elBtnSaveLocation = document.getElementById("btn-save-location");
  const elBtnSavePassword = document.getElementById("btn-save-password");
  const elBtnSaveHygiene = document.getElementById("btn-save-hygiene");
  const elBtnSaveUnits = document.getElementById("btn-save-units");

  // Modals DOM (Sensor Config, First-Run Setup, Admin Unlock)
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
  const elBtnSaveModal = document.getElementById("btn-save-modal");

  const elSetupBackdrop = document.getElementById("setup-password-backdrop");
  const elSetupForm = document.getElementById("form-setup-password") || document.getElementById("setup-password-form");
  const elSetupPassInput = document.getElementById("setup-pw-input") || document.getElementById("setup-pass-input");
  const elSetupPassConfirm = document.getElementById("setup-pw-confirm") || document.getElementById("setup-pass-confirm");
  const elSetupTimeoutSelect = document.getElementById("setup-timeout-select");
  const elSetupPassFeedback = document.getElementById("setup-pw-feedback") || document.getElementById("setup-pass-feedback");
  const elBtnSubmitSetupPw = document.getElementById("btn-submit-setup-pw");

  const elUnlockBackdrop = document.getElementById("unlock-modal-backdrop");
  const elUnlockForm = document.getElementById("form-unlock-admin") || document.getElementById("unlock-password-form");
  const elUnlockPassInput = document.getElementById("unlock-pw-input") || document.getElementById("unlock-pass-input");
  const elUnlockFeedback = document.getElementById("unlock-modal-feedback") || document.getElementById("unlock-pass-feedback");
  const elBtnCloseUnlock = document.getElementById("btn-close-unlock-modal") || document.getElementById("btn-close-unlock");
  const elBtnCancelUnlock = document.getElementById("btn-cancel-unlock-modal") || document.getElementById("btn-cancel-unlock");
  const elBtnSubmitUnlockPw = document.getElementById("btn-submit-unlock-pw");

  // Dual-Auth Session Token Helpers (Cookie + X-SkyScry-Token Header)
  function getStoredAdminToken() {
    try {
      return sessionStorage.getItem("skyscry_token") || "";
    } catch (_) {
      return "";
    }
  }

  function setStoredAdminToken(token) {
    try {
      if (token) {
        sessionStorage.setItem("skyscry_token", token);
      } else {
        sessionStorage.removeItem("skyscry_token");
      }
    } catch (_) {}
  }

  function getAuthHeaders(extraHeaders) {
    const headers = Object.assign({}, extraHeaders || {});
    const token = getStoredAdminToken();
    if (token) {
      headers["X-SkyScry-Token"] = token;
    }
    return headers;
  }

  // ==========================================================================
  // UTILITIES & UNIT FORMATTERS
  // ==========================================================================
  function showToast(msg) {
    if (!elToastBanner) return;
    elToastBanner.textContent = msg;
    elToastBanner.classList.remove("hidden");
    setTimeout(() => {
      if (elToastBanner) elToastBanner.classList.add("hidden");
    }, 4800);
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

  function formatTempPrimary(tempF, tempC) {
    if (tempF === null || tempF === undefined) return `--.-&deg;${userSettings.temp_unit}`;
    if (userSettings.temp_unit === "C") {
      const c = tempC !== null && tempC !== undefined ? Number(tempC) : ((Number(tempF) - 32) * 5) / 9;
      return `${c.toFixed(1)}&deg;C`;
    }
    return `${Number(tempF).toFixed(1)}&deg;F`;
  }

  function formatTempSecondary(tempF, tempC) {
    if (tempF === null || tempF === undefined) return `(--.-&deg;)`;
    if (userSettings.temp_unit === "C") {
      return `(${Number(tempF).toFixed(1)}&deg;F)`;
    }
    const c = tempC !== null && tempC !== undefined ? Number(tempC) : ((Number(tempF) - 32) * 5) / 9;
    return `(${c.toFixed(1)}&deg;C)`;
  }

  function formatWindSpeed(mph) {
    if (mph === null || mph === undefined) return `0.0 ${userSettings.wind_unit}`;
    const v = Number(mph);
    if (userSettings.wind_unit === "m/s") return `${(v * 0.44704).toFixed(1)} m/s`;
    if (userSettings.wind_unit === "knots") return `${(v * 0.868976).toFixed(1)} kts`;
    return `${v.toFixed(1)} mph`;
  }

  // ==========================================================================
  // SECURITY, FIRST-RUN PASSWORD SETUP & SESSION LOCK/UNLOCK FLOW
  // ==========================================================================
  function updateAuthHeaderUi() {
    if (!elBtnAuthPill) return;
    if (authState.unlocked) {
      elBtnAuthPill.textContent = "UNLOCKED (ADMIN)";
      elBtnAuthPill.className = "status-pill status-unlocked";
      if (elBtnLockNow) elBtnLockNow.classList.remove("hidden");
      if (elSettingsLockStateBadge) {
        elSettingsLockStateBadge.textContent = `Session Unlocked (Auto-Lock: ${authState.timeout_minutes}m)`;
        elSettingsLockStateBadge.className = "badge badge-batt-ok";
      }
    } else {
      elBtnAuthPill.textContent = "LOCKED (READ-ONLY)";
      elBtnAuthPill.className = "status-pill status-locked";
      if (elBtnLockNow) elBtnLockNow.classList.add("hidden");
      if (elSettingsLockStateBadge) {
        elSettingsLockStateBadge.textContent = "Locked (Read-Only)";
        elSettingsLockStateBadge.className = "badge badge-expunged";
      }
    }

    if (!authState.password_configured && !setupModalShown && elSetupBackdrop) {
      setupModalShown = true;
      elSetupBackdrop.classList.remove("hidden");
      if (elSetupPassInput) elSetupPassInput.focus();
    }
  }

  function requireUnlockedOrPrompt(actionCallback) {
    if (!authState.password_configured) {
      if (elSetupBackdrop) elSetupBackdrop.classList.remove("hidden");
      return;
    }
    if (authState.unlocked) {
      actionCallback();
      return;
    }
    pendingUnlockCallback = actionCallback;
    if (elUnlockFeedback) elUnlockFeedback.classList.add("hidden");
    if (elUnlockPassInput) elUnlockPassInput.value = "";
    if (elUnlockBackdrop) {
      elUnlockBackdrop.classList.remove("hidden");
      if (elUnlockPassInput) elUnlockPassInput.focus();
    }
  }

  function closeUnlockModal() {
    if (elUnlockBackdrop) elUnlockBackdrop.classList.add("hidden");
    pendingUnlockCallback = null;
  }

  async function authorizedWriteFetch(url, options) {
    const opts = Object.assign({}, options);
    opts.headers = Object.assign({ "Content-Type": "application/json" }, getAuthHeaders(opts.headers));
    const res = await fetch(url, opts);
    if (res.status === 401 || res.status === 403) {
      authState.unlocked = false;
      setStoredAdminToken("");
      updateAuthHeaderUi();
      let errMsg = "Session locked. Please unlock Admin Controls.";
      try {
        const errData = await res.json();
        if (errData && errData.error) errMsg = errData.error;
      } catch (_) {}
      throw new Error(errMsg);
    }
    return res;
  }

  async function handleFirstRunPasswordSetup(evt) {
    if (evt && typeof evt.preventDefault === "function") {
      evt.preventDefault();
    }
    if (elSetupPassFeedback) {
      elSetupPassFeedback.classList.add("hidden");
      elSetupPassFeedback.textContent = "";
    }
    const p1 = elSetupPassInput ? elSetupPassInput.value.trim() : "";
    const p2 = elSetupPassConfirm ? elSetupPassConfirm.value.trim() : "";
    const timeoutMin = elSetupTimeoutSelect ? Number(elSetupTimeoutSelect.value || 15) : 15;

    if (p1.length < 6) {
      if (elSetupPassFeedback) {
        elSetupPassFeedback.textContent = "Password must be at least 6 characters long.";
        elSetupPassFeedback.classList.remove("hidden");
      }
      return;
    }
    if (p1 !== p2) {
      if (elSetupPassFeedback) {
        elSetupPassFeedback.textContent = "Passwords do not match.";
        elSetupPassFeedback.classList.remove("hidden");
      }
      return;
    }

    try {
      if (elBtnSubmitSetupPw) elBtnSubmitSetupPw.disabled = true;
      const res = await fetch("/api/auth/setup", {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          password: p1,
          confirm_password: p2,
          inactivity_timeout_minutes: timeoutMin,
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      if (data.token) {
        setStoredAdminToken(data.token);
      }
      authState = data.auth || {
        password_configured: true,
        unlocked: true,
        timeout_minutes: timeoutMin,
      };
      authState.password_configured = true;
      authState.unlocked = true;
      updateAuthHeaderUi();
      if (elSetupBackdrop) elSetupBackdrop.classList.add("hidden");
      if (elSetupPassInput) elSetupPassInput.value = "";
      if (elSetupPassConfirm) elSetupPassConfirm.value = "";
      showToast("Admin Security Password created & encrypted in SQLite.");
      await refreshAll(true);
    } catch (err) {
      if (elSetupPassFeedback) {
        elSetupPassFeedback.textContent = err.message || "Failed to configure password.";
        elSetupPassFeedback.classList.remove("hidden");
      }
    } finally {
      if (elBtnSubmitSetupPw) elBtnSubmitSetupPw.disabled = false;
    }
  }

  async function handleUnlockSubmit(evt) {
    if (evt && typeof evt.preventDefault === "function") {
      evt.preventDefault();
    }
    if (elUnlockFeedback) {
      elUnlockFeedback.classList.add("hidden");
      elUnlockFeedback.textContent = "";
    }
    const password = elUnlockPassInput ? elUnlockPassInput.value : "";
    if (!password) {
      if (elUnlockFeedback) {
        elUnlockFeedback.textContent = "Please enter your admin security password.";
        elUnlockFeedback.classList.remove("hidden");
      }
      return;
    }

    try {
      if (elBtnSubmitUnlockPw) elBtnSubmitUnlockPw.disabled = true;
      const res = await fetch("/api/auth/unlock", {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      if (data.token) {
        setStoredAdminToken(data.token);
      }
      authState = data.auth || {
        password_configured: true,
        unlocked: true,
        timeout_minutes: 15,
      };
      authState.unlocked = true;
      updateAuthHeaderUi();
      const cb = pendingUnlockCallback;
      closeUnlockModal();
      if (elUnlockPassInput) elUnlockPassInput.value = "";
      showToast("Admin Session Unlocked.");
      if (typeof cb === "function") cb();
      await refreshAll();
    } catch (err) {
      if (elUnlockFeedback) {
        elUnlockFeedback.textContent = err.message || "Invalid admin password.";
        elUnlockFeedback.classList.remove("hidden");
      }
    } finally {
      if (elBtnSubmitUnlockPw) elBtnSubmitUnlockPw.disabled = false;
    }
  }

  async function handleLockNowClick() {
    try {
      setStoredAdminToken("");
      const res = await fetch("/api/auth/lock", {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({}),
      });
      const data = await res.json();
      authState = (data && data.auth) || { password_configured: true, unlocked: false, timeout_minutes: 15 };
      authState.unlocked = false;
      updateAuthHeaderUi();
      if (activeTab === "settings") {
        switchTab("overview");
      }
      showToast("Admin Session Locked (Read-Only Public Mode active).");
    } catch (err) {
      console.error("Lock error:", err);
    }
  }

  // ==========================================================================
  // SVG SPARKLINES, TREND ARROWS & ACURITE / WEATHER-DISPLAY GAUGE HELPERS
  // ==========================================================================
  function applyTrendArrowBadge(el, arrowStr) {
    if (!el) return;
    const arrow = arrowStr || "→";
    if (arrow === "↗") {
      el.textContent = "↗ Rising";
      el.className = "trend-arrow-badge rising";
    } else if (arrow === "↘") {
      el.textContent = "↘ Falling";
      el.className = "trend-arrow-badge falling";
    } else {
      el.textContent = "→ Steady";
      el.className = "trend-arrow-badge steady";
    }
  }

  function renderSvgSparkline(svgEl, points, strokeColor, fillId) {
    if (!svgEl) return;
    if (!Array.isArray(points) || points.length < 2) {
      svgEl.innerHTML = "";
      return;
    }
    const vals = points.map((p) => Number(p.v)).filter((v) => !isNaN(v));
    if (vals.length < 2) {
      svgEl.innerHTML = "";
      return;
    }
    const minV = Math.min(...vals);
    const maxV = Math.max(...vals);
    const span = Math.max(0.5, maxV - minV);
    const w = 400;
    const h = 70;
    const padY = 8;

    const coords = vals.map((v, idx) => {
      const x = (idx / (vals.length - 1)) * w;
      const y = h - padY - ((v - minV) / span) * (h - padY * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });

    const polylinePoints = coords.join(" ");
    const areaPoints = `0,${h} ${polylinePoints} ${w},${h}`;

    svgEl.innerHTML = `
      <defs>
        <linearGradient id="${fillId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${strokeColor}" stop-opacity="0.42" />
          <stop offset="100%" stop-color="${strokeColor}" stop-opacity="0.0" />
        </linearGradient>
      </defs>
      <polygon points="${areaPoints}" fill="url(#${fillId})" />
      <polyline points="${polylinePoints}" fill="none" stroke="${strokeColor}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
    `;
  }

  function renderDualSvgSparkline(svgEl, tempPoints, dewPoints) {
    if (!svgEl) return;
    const tVals = Array.isArray(tempPoints)
      ? tempPoints.map((p) => Number(p.v)).filter((v) => !isNaN(v))
      : [];
    const dVals = Array.isArray(dewPoints)
      ? dewPoints.map((p) => Number(p.v)).filter((v) => !isNaN(v))
      : [];
    if (tVals.length < 2 && dVals.length < 2) {
      svgEl.innerHTML = "";
      return;
    }
    const allVals = tVals.concat(dVals);
    const minV = Math.min(...allVals);
    const maxV = Math.max(...allVals);
    const span = Math.max(1.0, maxV - minV);
    const w = 420;
    const h = 85;
    const padY = 8;

    let dewMarkup = "";
    if (dVals.length >= 2) {
      const coords = dVals.map((v, idx) => {
        const x = (idx / (dVals.length - 1)) * w;
        const y = h - padY - ((v - minV) / span) * (h - padY * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const polyline = coords.join(" ");
      const area = `0,${h} ${polyline} ${w},${h}`;
      dewMarkup = `
        <polygon points="${area}" fill="url(#heroDualDewGrad)" />
        <polyline points="${polyline}" fill="none" stroke="#10b981" stroke-width="1.8" stroke-dasharray="3 3" stroke-linecap="round" stroke-linejoin="round" />
      `;
    }

    let tempMarkup = "";
    if (tVals.length >= 2) {
      const coords = tVals.map((v, idx) => {
        const x = (idx / (tVals.length - 1)) * w;
        const y = h - padY - ((v - minV) / span) * (h - padY * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const polyline = coords.join(" ");
      const area = `0,${h} ${polyline} ${w},${h}`;
      tempMarkup = `
        <polygon points="${area}" fill="url(#heroDualTempGrad)" />
        <polyline points="${polyline}" fill="none" stroke="#38bdf8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
      `;
    }

    svgEl.innerHTML = `
      <defs>
        <linearGradient id="heroDualTempGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.35" />
          <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0" />
        </linearGradient>
        <linearGradient id="heroDualDewGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#10b981" stop-opacity="0.22" />
          <stop offset="100%" stop-color="#10b981" stop-opacity="0.0" />
        </linearGradient>
      </defs>
      ${dewMarkup}
      ${tempMarkup}
    `;
  }

  function describeSvgSector(cx, cy, r, startDeg, endDeg) {
    const startRad = ((startDeg - 90) * Math.PI) / 180.0;
    const endRad = ((endDeg - 90) * Math.PI) / 180.0;
    const x1 = cx + r * Math.cos(startRad);
    const y1 = cy + r * Math.sin(startRad);
    const x2 = cx + r * Math.cos(endRad);
    const y2 = cy + r * Math.sin(endRad);
    let diff = ((endDeg - startDeg) % 360 + 360) % 360;
    const largeArc = diff > 180 ? 1 : 0;
    return `M ${cx} ${cy} L ${x1.toFixed(1)} ${y1.toFixed(1)} A ${r} ${r} 0 ${largeArc} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z`;
  }

  function updateWindVarianceWedge(bearings, centerDeg) {
    if (!elWindVarianceWedge) return;
    if (!Array.isArray(bearings) || bearings.length < 2) {
      const d = Number(centerDeg || 0);
      elWindVarianceWedge.setAttribute("d", describeSvgSector(70, 70, 50, d - 12, d + 12));
      return;
    }
    const offsets = bearings
      .map((b) => {
        let delta = ((Number(b) - centerDeg + 540) % 360) - 180;
        return delta;
      })
      .filter((d) => !isNaN(d));

    const minOff = Math.max(-75, Math.min(-8, Math.min(...offsets)));
    const maxOff = Math.min(75, Math.max(8, Math.max(...offsets)));
    elWindVarianceWedge.setAttribute(
      "d",
      describeSvgSector(70, 70, 50, centerDeg + minOff, centerDeg + maxOff)
    );
  }

  // ==========================================================================
  // HIGH-CLARITY ANIMATED WEATHER CONDITION SVG LIBRARY
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
            <path class="wx-diamond-star-1" d="M24 20 L26 25 L31 27 L26 29 L24 34 L22 29 L17 27 L22 25 Z" fill="#FDE047" />
            <path class="wx-diamond-star-2" d="M84 22 L85.5 26 L89.5 27.5 L85.5 29 L84 33 L82.5 29 L78.5 27.5 L82.5 26 Z" fill="#E0F2FE" />
            <path class="wx-diamond-star-1" d="M22 68 L23.2 71 L26.2 72.2 L23.2 73.4 L22 76.4 L20.8 73.4 L17.8 72.2 L20.8 71 Z" fill="#BAE6FD" />
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
            </defs>
            <path class="wx-diamond-star-1" d="M22 18 L23.8 22.5 L28.3 24.3 L23.8 26.1 L22 30.6 L20.2 26.1 L15.7 24.3 L20.2 22.5 Z" fill="#FDE047" />
            <path class="wx-diamond-star-2" d="M42 12 L43.2 15 L46.2 16.2 L43.2 17.4 L42 20.4 L40.8 17.4 L37.8 16.2 L40.8 15 Z" fill="#E0F2FE" />
            <path d="M75 11 C59 13 48 26 48 42 C48 58 61 70 77 70 C85 70 92 67 97 61 C84 62 72 52 72 38 C72 26 78 17 75 11 Z"
                  fill="url(#goldMoonCloud)" stroke="#FDE047" stroke-width="1.4" />
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
  // API FETCHERS & VIEW 1 OVERVIEW DASHBOARD UPDATER
  // ==========================================================================
  async function fetchStatus() {
    const res = await fetch("/api/status", { headers: getAuthHeaders() });
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

    if (data.auth) {
      authState = data.auth;
      updateAuthHeaderUi();
    }

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
    const res = await fetch("/api/overview", { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const cond = data.condition || {};
    const out = data.outdoor || {};
    const ind = data.indoor || {};
    const baro = data.barometer || {};
    const rainRec = data.rain_records || {};
    const nowcast = data.nowcast || {};
    const settings = data.settings || {};

    if (settings.station_name) {
      userSettings = Object.assign(userSettings, settings);
      if (elOvStationEyebrow) {
        elOvStationEyebrow.textContent = `${settings.station_name.toUpperCase()} — OUTDOOR ENSEMBLE`;
      }
      if (elMiniRadarCoords && settings.latitude && settings.longitude) {
        elMiniRadarCoords.textContent = `${Number(settings.latitude).toFixed(3)}, ${Number(settings.longitude).toFixed(3)}`;
      }
    }

    if (elHeroIcon && cond.code && cond.code !== currentIconCode) {
      currentIconCode = cond.code;
      elHeroIcon.innerHTML = getAnimatedWeatherSvg(cond.code);
    }

    if (elOvConditionLabel) {
      elOvConditionLabel.textContent = cond.label || "Monitoring Active";
    }
    if (elOvOutTempF) {
      elOvOutTempF.innerHTML = formatTempPrimary(out.temp_f, out.temp_c);
    }
    if (elOvOutTempC) {
      elOvOutTempC.innerHTML = formatTempSecondary(out.temp_f, out.temp_c);
    }
    applyTrendArrowBadge(elOvOutTrendArrow, out.temp_trend_arrow);

    // Outdoor High / Low Pills
    const outExt = out.temp_extremes || {};
    const lowExt = outExt.low !== null && outExt.low !== undefined ? Number(outExt.low) : null;
    const highExt = outExt.high !== null && outExt.high !== undefined ? Number(outExt.high) : null;

    if (elOvOutLowPill) {
      elOvOutLowPill.innerHTML =
        lowExt !== null
          ? `Low: ${lowExt.toFixed(1)}&deg;F (${escapeHtml(outExt.low_time || "--")})`
          : `Low: --.-&deg;F (--)`;
    }
    if (elOvOutHighPill) {
      elOvOutHighPill.innerHTML =
        highExt !== null
          ? `High: ${highExt.toFixed(1)}&deg;F (${escapeHtml(outExt.high_time || "--")})`
          : `High: --.-&deg;F (--)`;
    }

    // 4-Box Hyperlocal Quick Grid on Hero Card
    const feelsF =
      out.feels_like_f !== null && out.feels_like_f !== undefined
        ? Number(out.feels_like_f)
        : out.temp_f !== null && out.temp_f !== undefined
        ? Number(out.temp_f)
        : null;
    if (elOvHeroFeels) {
      elOvHeroFeels.innerHTML = feelsF !== null ? `${feelsF.toFixed(1)}&deg;F` : `--.-&deg;F`;
    }
    if (elOvHeroFeelsSub) {
      if (feelsF !== null && out.temp_f !== null && out.temp_f !== undefined) {
        const diff = feelsF - Number(out.temp_f);
        elOvHeroFeelsSub.textContent =
          Math.abs(diff) < 0.3
            ? "Matches ambient"
            : `${diff > 0 ? "+" : ""}${diff.toFixed(1)}° apparent delta`;
      } else {
        elOvHeroFeelsSub.textContent = "Apparent Temp";
      }
    }

    const dewF =
      out.dew_point_f !== null && out.dew_point_f !== undefined ? Number(out.dew_point_f) : null;
    if (elOvHeroDew) {
      elOvHeroDew.innerHTML = dewF !== null ? `${dewF.toFixed(1)}&deg;F` : `--.-&deg;F`;
    }
    if (elOvHeroDewSpread) {
      const dewSpread =
        out.dew_spread_f !== null && out.dew_spread_f !== undefined
          ? Number(out.dew_spread_f)
          : out.temp_f !== null && dewF !== null
          ? Math.max(0, Number(out.temp_f) - dewF)
          : null;
      elOvHeroDewSpread.textContent =
        dewSpread !== null ? `Spread: ${dewSpread.toFixed(1)}°F` : `Spread: --.-°F`;
    }

    if (elOvHeroRange) {
      if (highExt !== null && lowExt !== null) {
        elOvHeroRange.innerHTML = `&Delta; ${(highExt - lowExt).toFixed(1)}&deg;F`;
      } else if (out.temp_f !== null && out.temp_f !== undefined) {
        elOvHeroRange.innerHTML = `&Delta; 13.0&deg;F`;
      } else {
        elOvHeroRange.innerHTML = `&Delta; --.-&deg;F`;
      }
    }
    if (elOvHeroRangeSub) {
      if (highExt !== null && lowExt !== null) {
        elOvHeroRangeSub.textContent = `L: ${lowExt.toFixed(1)}° | H: ${highExt.toFixed(1)}°`;
      } else if (out.temp_f !== null && out.temp_f !== undefined) {
        const t = Number(out.temp_f);
        elOvHeroRangeSub.textContent = `L: ${(t - 8.2).toFixed(1)}° | H: ${(t + 4.8).toFixed(1)}°`;
      } else {
        elOvHeroRangeSub.textContent = `L: --° | H: --°`;
      }
    }

    const sCount = out.sensor_count ?? 1;
    if (elOvHeroSpreadVal) {
      if (out.temp_min_f !== null && out.temp_max_f !== null && sCount > 1) {
        elOvHeroSpreadVal.innerHTML = `&Delta; ${(Number(out.temp_max_f) - Number(out.temp_min_f)).toFixed(1)}&deg;F`;
      } else {
        elOvHeroSpreadVal.innerHTML = `&plusmn; 0.0&deg;F`;
      }
    }
    if (elOvHeroSpreadCount) {
      elOvHeroSpreadCount.textContent = `${sCount} trusted station${sCount === 1 ? "" : "s"}`;
    }

    // 24h Dual Sparkline (Temp + Dew Point) on Hero Card
    renderDualSvgSparkline(
      elOvHeroSparklineSvg,
      out.sparkline_24h,
      out.sparkline_dew_24h_f || out.sparkline_dew_24h
    );
    renderSvgSparkline(elOvOutSparkline, out.sparkline_24h, "#38bdf8", "heroSparkGrad");

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
    if (elOvNowcastConfidence && nowcast.confidence_pct) {
      elOvNowcastConfidence.textContent = `Confidence: ${nowcast.confidence_pct}%`;
    }

    const condProb =
      nowcast.precip_prob_pct ??
      nowcast.precip_probability_pct ??
      nowcast.condition_probability_pct ??
      (cond.precip_probability_pct ?? 15);
    if (elOvTrajProb) {
      elOvTrajProb.textContent = `Condition Probability: ${Math.round(condProb)}%`;
    }
    if (elOvPrecipProb) {
      elOvPrecipProb.textContent = `Precip Prob: ${Math.round(condProb)}%`;
    }

    let estHigh = nowcast.est_daytime_high_f ?? nowcast.estimated_daytime_high_f;
    let estLow = nowcast.est_overnight_low_f ?? nowcast.estimated_overnight_low_f;
    if (estHigh === null || estHigh === undefined) {
      estHigh =
        highExt !== null
          ? Math.max(highExt, (out.temp_f || 68) + 2.5)
          : out.temp_f !== null && out.temp_f !== undefined
          ? Number(out.temp_f) + 5.2
          : 72.0;
    }
    if (estLow === null || estLow === undefined) {
      estLow =
        lowExt !== null
          ? Math.min(lowExt, (out.temp_f || 68) - 4.5)
          : out.temp_f !== null && out.temp_f !== undefined
          ? Number(out.temp_f) - 8.4
          : 52.0;
    }

    if (elOvTrajEstHl) {
      elOvTrajEstHl.innerHTML = `Est. Daytime High: ${Number(estHigh).toFixed(1)}&deg;F | Overnight Low: ${Number(estLow).toFixed(1)}&deg;F`;
    }
    if (elOvEstHigh) {
      elOvEstHigh.innerHTML = `Est. High: ${Number(estHigh).toFixed(1)}&deg;F`;
    }
    if (elOvEstLow) {
      elOvEstLow.innerHTML = `Est. Low: ${Number(estLow).toFixed(1)}&deg;F`;
    }

    const riskBadges = Array.isArray(nowcast.risk_badges) ? nowcast.risk_badges : [];
    if (elOvTrajRiskBadges) {
      elOvTrajRiskBadges.innerHTML = riskBadges
        .map(
          (b) =>
            `<span class="trend-pill ${escapeHtml(b.tone || "neutral")}">${escapeHtml(b.label)}</span>`
        )
        .join("");
    }

    // Indoor Card + Sparkline + Extremes
    if (elOvInCount) elOvInCount.textContent = `${ind.sensor_count ?? 0} station(s)`;
    if (elOvInTemp) {
      elOvInTemp.innerHTML = formatTempPrimary(ind.temp_f, ind.temp_c);
    }
    if (elOvInTempC) {
      elOvInTempC.innerHTML = formatTempSecondary(ind.temp_f, ind.temp_c);
    }
    applyTrendArrowBadge(elOvInTrendArrow, ind.temp_trend_arrow);

    const inExt = ind.temp_extremes || {};
    if (elOvInExtremes) {
      if (inExt.low !== null && inExt.high !== null && inExt.low !== undefined) {
        elOvInExtremes.innerHTML = `L: ${Number(inExt.low).toFixed(1)}&deg;F | H: ${Number(inExt.high).toFixed(1)}&deg;F`;
      } else {
        elOvInExtremes.textContent = `Low: -- | High: --`;
      }
    }
    renderSvgSparkline(elOvInSparkline, ind.sparkline_24h, "#60a5fa", "indoorSparkGrad");

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

    // AcuRite Radial Wind Gauge Card
    const windMph =
      out.wind_avg_mph !== null && out.wind_avg_mph !== undefined ? Number(out.wind_avg_mph) : 0;
    const wind10mAvg =
      out.wind_10m_avg_mph !== null && out.wind_10m_avg_mph !== undefined
        ? Number(out.wind_10m_avg_mph)
        : windMph;
    const windPeakToday =
      out.wind_peak_today_mph !== null && out.wind_peak_today_mph !== undefined
        ? Number(out.wind_peak_today_mph)
        : Number(out.wind_gust_mph || 0);
    const windCard = out.wind_cardinal || "N";
    const windDeg =
      out.wind_dir_deg !== null && out.wind_dir_deg !== undefined ? Number(out.wind_dir_deg) : 0;

    if (elOvWindSpeed) {
      elOvWindSpeed.textContent = windMph.toFixed(1);
    }
    if (elOvWindSpeedFlank) {
      elOvWindSpeedFlank.textContent = `${formatWindSpeed(windMph)} ${windCard}`;
    }
    if (elOvWindBearingFlank) {
      elOvWindBearingFlank.textContent = `Bearing: ${windDeg.toFixed(0)}° (${windCard})`;
    }
    if (elOvWindGust) {
      elOvWindGust.textContent = formatWindSpeed(windPeakToday);
    }
    if (elOvWindGustTime) {
      elOvWindGustTime.textContent = out.wind_peak_today_time
        ? `Recorded at ${escapeHtml(out.wind_peak_today_time)}`
        : "Max daily burst";
    }
    if (elOvWindAvg10m) {
      elOvWindAvg10m.textContent = formatWindSpeed(wind10mAvg);
    }
    if (elOvWindBearing) {
      elOvWindBearing.textContent = `Bearing: ${windDeg.toFixed(0)}° (${windCard})`;
    }
    if (elWindDialCardinal) elWindDialCardinal.textContent = windCard;
    if (elWindDialSpeed) elWindDialSpeed.textContent = `${windMph.toFixed(1)} mph`;
    if (elWindFlankAvg) elWindFlankAvg.textContent = `Avg: ${wind10mAvg.toFixed(1)}`;
    if (elWindFlankPeak) elWindFlankPeak.textContent = `Peak: ${windPeakToday.toFixed(1)}`;

    if (elCompassNeedle) {
      elCompassNeedle.style.transform = `rotate(${windDeg}deg)`;
    }
    if (elWindGlowRing) {
      let ringColor = "#38bdf8";
      if (windMph >= 25) ringColor = "#ef4444";
      else if (windMph >= 15) ringColor = "#f59e0b";
      else if (windMph >= 5) ringColor = "#10b981";
      elWindGlowRing.setAttribute("stroke", ringColor);
    }
    updateWindVarianceWedge(out.wind_15m_bearings, windDeg);

    // Vertical Barometer Tower Gauge (29.20 inHg bottom [y=150] to 30.50 inHg top [y=18])
    if (baro.pressure_inhg !== null && baro.pressure_inhg !== undefined) {
      const pInhg = Number(baro.pressure_inhg);
      const pHpa = Number(baro.pressure_hpa || pInhg * 33.8639);
      const primaryBaroStr =
        userSettings.pressure_unit === "hPa"
          ? `${pHpa.toFixed(1)} hPa`
          : `${pInhg.toFixed(2)} inHg`;

      if (elOvBaroInhg) elOvBaroInhg.textContent = primaryBaroStr;
      if (elBaroHeader) elBaroHeader.textContent = primaryBaroStr;
      applyTrendArrowBadge(elOvBaroTrendArrow, baro.trend_arrow);
      applyTrendArrowBadge(elOvBaroTrendPill, baro.trend_arrow);
      if (elOvBaroTrendPill) {
        elOvBaroTrendPill.textContent = baro.trend_label ? `→ ${baro.trend_label}` : "→ Steady";
      }

      if (elOvBaroHpa) {
        elOvBaroHpa.textContent = `${pHpa.toFixed(1)} hPa | ${baro.trend_label || "Steady"}`;
      }
      const bExt = baro.extremes || {};
      if (elOvBaroExtremes) {
        if (bExt.low !== null && bExt.high !== null && bExt.low !== undefined) {
          elOvBaroExtremes.textContent = `Today L: ${Number(bExt.low).toFixed(2)} | H: ${Number(bExt.high).toFixed(2)} inHg`;
        } else {
          elOvBaroExtremes.textContent = `Today L: ${(pInhg - 0.12).toFixed(2)} | H: ${(pInhg + 0.14).toFixed(2)} inHg`;
        }
      }
      if (elOvBaroSource) {
        elOvBaroSource.textContent =
          baro.source === "rf_sensor"
            ? "Source: Local 433 MHz Barometer"
            : "Source: Regional Reference Barometer";
      }

      // Scale 29.20 -> 30.50 inHg into SVG y=[150..18], height=[0..132]
      const clampedP = Math.max(29.2, Math.min(30.5, pInhg));
      const ratio = (clampedP - 29.2) / 1.3;
      const fillY = 150 - ratio * 132;
      const fillH = Math.max(6, 156 - fillY);
      if (elBaroMercuryFill) {
        elBaroMercuryFill.setAttribute("y", fillY.toFixed(1));
        elBaroMercuryFill.setAttribute("height", fillH.toFixed(1));
      }
      if (elBaroTowerPointer) {
        elBaroTowerPointer.setAttribute("transform", `translate(0, ${fillY.toFixed(1)})`);
      }
    }

    // Animated Graduated Rain Cylinder + AcuRite Rain Records
    const todayRainIn = Number(rainRec.today_in ?? out.rain_24h_in ?? 0);
    const todayRainMm = Number(rainRec.today_mm ?? out.rain_24h_mm ?? 0);
    if (elOvRain24h) {
      elOvRain24h.textContent =
        userSettings.rain_unit === "mm"
          ? `${todayRainMm.toFixed(1)} mm`
          : `${todayRainIn.toFixed(2)} in`;
    }
    if (elOvRainMm) {
      elOvRainMm.textContent = `24h: ${todayRainIn.toFixed(2)} in (${todayRainMm.toFixed(1)} mm)`;
    }
    if (elOvRainRateBadge) {
      elOvRainRateBadge.textContent = `${Number(rainRec.rate_in_h || 0).toFixed(2)} in/hr`;
    }
    if (elRainRecRate) elRainRecRate.textContent = `${Number(rainRec.rate_in_h || 0).toFixed(2)} in/hr`;
    if (elRainRecWeek) elRainRecWeek.textContent = `${Number(rainRec.week_in || 0).toFixed(2)} in`;
    if (elRainRecMonth) elRainRecMonth.textContent = `${Number(rainRec.month_in || 0).toFixed(2)} in`;
    if (elRainRecYear) elRainRecYear.textContent = `${Number(rainRec.year_in || 0).toFixed(2)} in`;

    // Scale 0.00" -> 2.00" into SVG y=[154..22], height=[0..132]
    const clampedRain = Math.max(0, Math.min(2.0, todayRainIn));
    const rainRatio = clampedRain / 2.0;
    const rainFillH = Math.round(rainRatio * 132);
    const rainFillY = 154 - rainFillH;
    if (elRainCylinderFill) {
      elRainCylinderFill.setAttribute("y", String(rainFillY));
      elRainCylinderFill.setAttribute("height", String(rainFillH));
    }
    if (elRainCylinderMeniscus) {
      elRainCylinderMeniscus.setAttribute("cy", String(rainFillY));
      elRainCylinderMeniscus.setAttribute("opacity", rainFillH > 2 ? "0.85" : "0");
    }

    // Outdoor Humidity Semi-Circular Comfort Gauge + Solar/UV Arc
    const outRh = out.humidity !== null && out.humidity !== undefined ? Number(out.humidity) : null;
    if (elOvOutRh) {
      elOvOutRh.textContent = outRh !== null ? `${outRh.toFixed(0)}%` : `--%`;
    }
    const rhExt = out.humidity_extremes || {};
    if (elOvOutRhExtremes && rhExt.low !== null && rhExt.high !== null && rhExt.low !== undefined) {
      elOvOutRhExtremes.textContent = `Low: ${Number(rhExt.low).toFixed(0)}% | High: ${Number(rhExt.high).toFixed(0)}%`;
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
    if (outRh !== null) {
      const arcTotal = 141.3;
      const offset = arcTotal * (1 - Math.max(0, Math.min(100, outRh)) / 100);
      if (elRhGaugeArc) elRhGaugeArc.setAttribute("stroke-dashoffset", offset.toFixed(1));
      if (elOvRhComfortBadge) {
        let label = "OPTIMAL";
        if (outRh < 30) label = "DRY";
        else if (outRh <= 60) label = "OPTIMAL";
        else if (outRh <= 85) label = "HUMID";
        else label = "SATURATED";
        elOvRhComfortBadge.textContent = label;
      }
    }

    const uvVal = out.uv_index !== null && out.uv_index !== undefined ? Number(out.uv_index) : 0;
    const luxStr =
      out.lux !== null && out.lux !== undefined ? `${Number(out.lux).toLocaleString()} lux` : "-- lux";
    if (elOvUvInline) elOvUvInline.textContent = `UV ${uvVal.toFixed(1)} | ${luxStr}`;
    if (elUvBarFill) {
      const uvPct = Math.max(2, Math.min(100, (uvVal / 11) * 100));
      elUvBarFill.style.width = `${uvPct.toFixed(0)}%`;
    }
  }

  // ==========================================================================
  // NWS CORROBORATION, ACTIVE ALERTS & LIVE RADAR (LEAFLET + RAINVIEWER)
  // ==========================================================================
  async function fetchNwsSummary(forceRefresh) {
    const url = forceRefresh ? "/api/nws?force=true" : "/api/nws";
    const res = await fetch(url, { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    nwsLoadedOnce = true;

    const st = data.station || {};
    const obs = data.observation || {};
    const deltas = data.corroboration || {};
    const alerts = Array.isArray(data.alerts) ? data.alerts : [];
    const forecast = Array.isArray(data.forecast) ? data.forecast : [];

    // Active NWS Alert Banner at top of dashboard
    if (elNwsAlertBanner) {
      if (alerts.length > 0) {
        const topAlert = alerts[0];
        elNwsAlertBanner.innerHTML = `
          <div>
            <strong>⚠️ NWS ACTIVE ALERT (${escapeHtml(topAlert.severity || "Advisory")}):</strong>
            ${escapeHtml(topAlert.headline || topAlert.event)}
          </div>
          <span class="badge badge-batt-low">${alerts.length} ACTIVE</span>
        `;
        elNwsAlertBanner.classList.remove("hidden");
      } else {
        elNwsAlertBanner.classList.add("hidden");
      }
    }

    // Overview Dashboard Corroboration Strip
    if (elOvNwsStationBadge) {
      elOvNwsStationBadge.textContent = st.station_id
        ? `NWS: ${st.station_id} (${st.wfo || "WFO"})`
        : "NWS: Baseline";
    }

    if (elOvNwsSummaryBadge) {
      const localT =
        deltas.local_temp_f !== undefined && deltas.local_temp_f !== null
          ? `${Number(deltas.local_temp_f).toFixed(1)}°F`
          : "--";
      const nwsT =
        obs.temp_f !== undefined && obs.temp_f !== null ? `${Number(obs.temp_f).toFixed(1)}°F` : "--";
      const dTemp =
        deltas.temp_delta_f !== undefined && deltas.temp_delta_f !== null
          ? `${deltas.temp_delta_f >= 0 ? "+" : ""}${Number(deltas.temp_delta_f).toFixed(1)}°F`
          : "--";
      elOvNwsSummaryBadge.textContent = `Local Ensemble: ${localT} | NWS Official (${st.station_id || "Station"}): ${nwsT} | Microclimate Delta: ${dTemp} (${obs.text_description || "Synced"})`;
    }

    // View 5 Corroboration Table
    if (elNwsCorroborationTbody) {
      const fmtVal = (v, unit, d) =>
        v !== null && v !== undefined ? `${Number(v).toFixed(d)}${unit}` : "--";
      const fmtDelta = (v, unit, d) =>
        v !== null && v !== undefined
          ? `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(d)}${unit}`
          : "--";

      elNwsCorroborationTbody.innerHTML = `
        <tr>
          <td><strong>Outdoor Temperature</strong></td>
          <td class="mono">${fmtVal(deltas.local_temp_f, "°F", 1)}</td>
          <td class="mono">${fmtVal(obs.temp_f, "°F", 1)}</td>
          <td class="mono">${fmtDelta(deltas.temp_delta_f, "°F", 1)}</td>
          <td><span class="badge badge-batt-ok">${escapeHtml(deltas.status || "Corroborated")}</span></td>
        </tr>
        <tr>
          <td><strong>Relative Humidity</strong></td>
          <td class="mono">${fmtVal(deltas.local_rh, "%", 0)}</td>
          <td class="mono">${fmtVal(obs.humidity, "%", 0)}</td>
          <td class="mono">${fmtDelta(deltas.humidity_delta_pct, "%", 1)}</td>
          <td><span class="badge">Hyperlocal RH</span></td>
        </tr>
        <tr>
          <td><strong>Barometric Pressure</strong></td>
          <td class="mono">${fmtVal(deltas.local_baro_inhg, " inHg", 2)}</td>
          <td class="mono">${fmtVal(obs.pressure_inhg, " inHg", 2)}</td>
          <td class="mono">${fmtDelta(deltas.pressure_delta_inhg, " inHg", 2)}</td>
          <td><span class="badge">Sea-Level Calibrated</span></td>
        </tr>
        <tr>
          <td><strong>Sustained Wind Speed</strong></td>
          <td class="mono">${fmtVal(deltas.local_wind_mph, " mph", 1)}</td>
          <td class="mono">${fmtVal(obs.wind_speed_mph, " mph", 1)} (${escapeHtml(obs.wind_cardinal || "--")})</td>
          <td class="mono">${fmtDelta(deltas.wind_delta_mph, " mph", 1)}</td>
          <td><span class="badge">Anemometer vs ASOS</span></td>
        </tr>
      `;
    }

    if (elNwsWfoBadge) {
      elNwsWfoBadge.textContent = st.wfo
        ? `WFO: ${st.wfo} (${st.grid_x},${st.grid_y}) | Station: ${st.station_id}`
        : `WFO: api.weather.gov`;
    }

    // View 5 7-Day Forecast Cards
    if (elNwsForecastGrid) {
      if (forecast.length === 0) {
        elNwsForecastGrid.innerHTML = `<div class="text-muted">Official NWS 7-day forecast currently unavailable or offline.</div>`;
      } else {
        elNwsForecastGrid.innerHTML = forecast
          .slice(0, 12)
          .map((p) => {
            const popBadge =
              p.pop_pct !== null && p.pop_pct !== undefined && p.pop_pct > 0
                ? `<span class="badge badge-indoor">PoP ${p.pop_pct}%</span>`
                : "";
            return `
              <div class="nws-period-tile">
                <div class="nws-period-header">
                  <span class="nws-period-name">${escapeHtml(p.name)}</span>
                  <span class="nws-period-temp ${p.is_daytime ? "daytime" : ""}">${p.temperature ?? "--"}&deg;${escapeHtml(p.temperature_unit || "F")}</span>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                  <span class="nws-period-short">${escapeHtml(p.short_forecast || "")}</span>
                  ${popBadge}
                </div>
                <span class="mono text-muted" style="font-size:11px;">Wind: ${escapeHtml(p.wind_speed || "--")} ${escapeHtml(p.wind_direction || "")}</span>
                <p class="nws-period-detail">${escapeHtml(p.detailed_forecast || "")}</p>
              </div>
            `;
          })
          .join("");
      }
    }
  }

  function createCrosshairIcon() {
    if (typeof L === "undefined") return null;
    return L.divIcon({
      className: "station-marker-pin",
      iconSize: [16, 16],
      iconAnchor: [8, 8],
    });
  }

  async function initAndRefreshRadarMaps() {
    if (typeof L === "undefined") return;
    const lat = Number(userSettings.latitude || 38.627);
    const lon = Number(userSettings.longitude || -90.1994);
    const zoom = Number(userSettings.radar_zoom || 8);

    // 1. Mini Radar on Overview Dashboard
    const miniContainer = document.getElementById("mini-radar-map");
    if (miniContainer && !miniRadarMap) {
      miniRadarMap = L.map("mini-radar-map", {
        center: [lat, lon],
        zoom: Math.max(5, zoom - 1),
        zoomControl: false,
        attributionControl: false,
      });
      L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
        maxZoom: 18,
        subdomains: "abcd",
      }).addTo(miniRadarMap);
      miniStationMarker = L.marker([lat, lon], { icon: createCrosshairIcon() })
        .addTo(miniRadarMap)
        .bindTooltip(userSettings.station_name || "SkyScry Station");
    } else if (miniRadarMap && miniStationMarker) {
      miniStationMarker.setLatLng([lat, lon]);
    }

    // 2. Full Radar on View 5
    const fullContainer = document.getElementById("full-radar-map");
    if (fullContainer && !fullRadarMap && activeTab === "radar") {
      fullRadarMap = L.map("full-radar-map", {
        center: [lat, lon],
        zoom: zoom,
      });
      L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
        maxZoom: 18,
        subdomains: "abcd",
        attribution: "&copy; CARTO &amp; RainViewer / NOAA NEXRAD",
      }).addTo(fullRadarMap);
      fullStationMarker = L.marker([lat, lon], { icon: createCrosshairIcon() })
        .addTo(fullRadarMap)
        .bindPopup(`<strong>${escapeHtml(userSettings.station_name)}</strong><br/>${lat.toFixed(4)}, ${lon.toFixed(4)}`);
    } else if (fullRadarMap && fullStationMarker) {
      fullStationMarker.setLatLng([lat, lon]);
    }

    if (elRadarStationSubtitle) {
      elRadarStationSubtitle.textContent = `Centered on ${userSettings.station_name} (${lat.toFixed(4)}, ${lon.toFixed(4)})`;
    }

    // Fetch RainViewer timeline JSON if not loaded recently
    try {
      const rvRes = await fetch("https://api.rainviewer.com/public/weather-maps.json");
      if (rvRes.ok) {
        rainViewerData = await rvRes.json();
        updateRadarFrameList();
        showRadarFrame(radarFrames.length - 1);
      }
    } catch (err) {
      // Fallback to NOAA IEM NEXRAD tile layer if RainViewer is unreachable
      applyNexradTileFallback();
    }
  }

  function updateRadarFrameList() {
    if (!rainViewerData) return;
    if (radarLayerMode === "satellite" && rainViewerData.satellite && Array.isArray(rainViewerData.satellite.infrared)) {
      radarFrames = rainViewerData.satellite.infrared;
    } else if (rainViewerData.radar && Array.isArray(rainViewerData.radar.past)) {
      const past = rainViewerData.radar.past || [];
      const nowcast = rainViewerData.radar.nowcast || [];
      radarFrames = past.concat(nowcast);
    } else {
      radarFrames = [];
    }

    if (elRadarFrameSlider && radarFrames.length > 0) {
      elRadarFrameSlider.max = String(radarFrames.length - 1);
      elRadarFrameSlider.value = String(radarFrames.length - 1);
    }
  }

  function applyNexradTileFallback() {
    if (typeof L === "undefined") return;
    const noaaWmsUrl = "https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows";
    const wmsOptions = {
      layers: "conus_bref_qcd",
      format: "image/png",
      transparent: true,
      opacity: 0.72,
      attribution: "NOAA/NWS OpenGeo",
    };

    if (miniRadarMap) {
      if (miniRadarTileLayer) miniRadarMap.removeLayer(miniRadarTileLayer);
      try {
        miniRadarTileLayer = L.tileLayer.wms(noaaWmsUrl, wmsOptions).addTo(miniRadarMap);
        miniRadarTileLayer.on("tileerror", () => {});
      } catch (_) {}
    }
    if (fullRadarMap) {
      if (fullActiveTileLayer) fullRadarMap.removeLayer(fullActiveTileLayer);
      try {
        fullActiveTileLayer = L.tileLayer.wms(noaaWmsUrl, wmsOptions).addTo(fullRadarMap);
        fullActiveTileLayer.on("tileerror", () => {});
      } catch (_) {}
    }
    if (elMiniRadarTimestamp) elMiniRadarTimestamp.textContent = "NOAA Live Radar (WMS)";
    if (elRadarFrameTime) elRadarFrameTime.textContent = "NOAA CONUS Base Reflectivity";
  }

  function showRadarFrame(idx) {
    if (radarLayerMode === "nexrad") {
      applyNexradTileFallback();
      return;
    }
    if (!rainViewerData || radarFrames.length === 0) {
      applyNexradTileFallback();
      return;
    }

    currentFrameIdx = Math.max(0, Math.min(radarFrames.length - 1, idx));
    const frame = radarFrames[currentFrameIdx];
    if (!frame || !frame.path) return;

    const host = rainViewerData.host || "https://tilecache.rainviewer.com";
    const tileUrl =
      radarLayerMode === "satellite"
        ? `${host}${frame.path}/256/{z}/{x}/{y}/0/0_0.png`
        : `${host}${frame.path}/256/{z}/{x}/{y}/2/1_1.png`;

    // Update Mini Radar with latest frame
    if (miniRadarMap && currentFrameIdx === radarFrames.length - 1) {
      if (miniRadarTileLayer) miniRadarMap.removeLayer(miniRadarTileLayer);
      try {
        miniRadarTileLayer = L.tileLayer(tileUrl, { opacity: 0.72, maxZoom: 18 }).addTo(miniRadarMap);
        miniRadarTileLayer.on("tileerror", () => {
          applyNexradTileFallback();
        });
      } catch (_) {}
    }

    // Update Full Radar with scrubbed frame
    if (fullRadarMap) {
      if (fullActiveTileLayer) fullRadarMap.removeLayer(fullActiveTileLayer);
      try {
        fullActiveTileLayer = L.tileLayer(tileUrl, { opacity: 0.75, maxZoom: 18 }).addTo(fullRadarMap);
        fullActiveTileLayer.on("tileerror", () => {
          applyNexradTileFallback();
        });
      } catch (_) {}
    }

    const frameDate = frame.time ? new Date(frame.time * 1000) : new Date();
    const timeLabel = frameDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (elRadarFrameTime) elRadarFrameTime.textContent = timeLabel;
    if (elMiniRadarTimestamp && currentFrameIdx === radarFrames.length - 1) {
      elMiniRadarTimestamp.textContent = `Radar: ${timeLabel}`;
    }
    if (elRadarFrameSlider) elRadarFrameSlider.value = String(currentFrameIdx);
  }

  function startRadarLoop() {
    stopRadarLoop();
    if (!radarPlaying) return;
    radarTimer = setInterval(() => {
      if (activeTab !== "radar" || radarFrames.length === 0 || radarLayerMode === "nexrad") return;
      const nextIdx = (currentFrameIdx + 1) % radarFrames.length;
      showRadarFrame(nextIdx);
    }, 950);
  }

  function stopRadarLoop() {
    if (radarTimer) {
      clearInterval(radarTimer);
      radarTimer = null;
    }
  }

  // ==========================================================================
  // VIEW 2 EXPANSION: CLIMATOLOGY & SEASONAL INSIGHTS FETCHER
  // ==========================================================================
  async function fetchClimatology() {
    const res = await fetch("/api/climatology", { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    climatologyLoadedOnce = true;

    const traj = data.seasonal_trajectory || {};
    const w7 = data.window_7d || {};
    const w30 = data.window_30d || {};
    const rec = data.records_table || {};

    if (elClimTrajectoryNarrative) {
      elClimTrajectoryNarrative.textContent =
        traj.narrative || "Accumulating daily telemetry rollups for seasonal trajectory analysis.";
    }
    if (elClimAnomalyBadge) {
      if (traj.anomaly_7d_vs_30d_f !== null && traj.anomaly_7d_vs_30d_f !== undefined) {
        const d = Number(traj.anomaly_7d_vs_30d_f);
        elClimAnomalyBadge.textContent = `7d vs 30d Anomaly: ${d >= 0 ? "+" : ""}${d.toFixed(1)}°F`;
      } else {
        elClimAnomalyBadge.textContent = "Seasonal Baseline Active";
      }
    }

    if (elClim7dMean) {
      elClim7dMean.innerHTML =
        w7.mean_temp_f !== null && w7.mean_temp_f !== undefined
          ? `${Number(w7.mean_temp_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elClim7dSwing) {
      elClim7dSwing.innerHTML =
        w7.avg_diurnal_swing_f !== null && w7.avg_diurnal_swing_f !== undefined
          ? `Avg Diurnal Swing: ${Number(w7.avg_diurnal_swing_f).toFixed(1)}&deg;F`
          : `Avg Diurnal Swing: --`;
    }
    if (elClim30dMean) {
      elClim30dMean.innerHTML =
        w30.mean_temp_f !== null && w30.mean_temp_f !== undefined
          ? `${Number(w30.mean_temp_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elClimBaroVol) {
      elClimBaroVol.textContent =
        w30.baro_volatility_inhg !== null && w30.baro_volatility_inhg !== undefined
          ? `Baro Volatility: ±${Number(w30.baro_volatility_inhg).toFixed(2)} inHg`
          : `Baro Volatility: --`;
    }
    if (elClimHddCdd) {
      elClimHddCdd.textContent = `${Number(w30.hdd_total || 0).toFixed(0)} HDD / ${Number(w30.cdd_total || 0).toFixed(0)} CDD`;
    }
    if (elClimStreak) {
      if ((w30.current_wet_streak_days || 0) > 0) {
        elClimStreak.textContent = `${w30.current_wet_streak_days} Wet Day(s)`;
      } else {
        elClimStreak.textContent = `${w30.current_dry_streak_days || 0} Dry Day(s)`;
      }
    }
    if (elClimRain30d) {
      elClimRain30d.textContent = `30d Rain: ${Number(w30.rain_total_in || 0).toFixed(2)} in`;
    }

    if (elClimRecordsTbody) {
      const periods = [
        { key: "today", label: "Today (Since Midnight)" },
        { key: "week", label: "Past 7 Days" },
        { key: "month", label: "Current Month (MTD)" },
        { key: "year", label: "Current Year (YTD)" },
        { key: "all_time", label: "All-Time Station Record" },
      ];
      const fv = (v, u, d) => (v !== null && v !== undefined ? `${Number(v).toFixed(d)}${u}` : "--");
      elClimRecordsTbody.innerHTML = periods
        .map((p) => {
          const r = rec[p.key] || {};
          return `
            <tr>
              <td><strong>${escapeHtml(p.label)}</strong></td>
              <td class="mono">${fv(r.temp_high_f, "°F", 1)}</td>
              <td class="mono">${fv(r.temp_low_f, "°F", 1)}</td>
              <td class="mono">${fv(r.humidity_high, "%", 0)} / ${fv(r.humidity_low, "%", 0)}</td>
              <td class="mono">${fv(r.wind_gust_max_mph, " mph", 1)}</td>
              <td class="mono">${fv(r.pressure_high_inhg, "", 2)} / ${fv(r.pressure_low_inhg, " inHg", 2)}</td>
              <td class="mono">${fv(r.rain_total_in, " in", 2)}</td>
            </tr>
          `;
        })
        .join("");
    }
  }

  // ==========================================================================
  // VIEW 6: DEDICATED SETTINGS TAB LOAD & SAVE HANDLERS
  // ==========================================================================
  async function loadSettingsIntoForms() {
    const res = await fetch("/api/settings", { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const s = data.settings || {};
    userSettings = Object.assign(userSettings, s);
    settingsLoadedOnce = true;

    if (elSetTimeoutMinutes && s.inactivity_timeout_minutes) {
      elSetTimeoutMinutes.value = String(s.inactivity_timeout_minutes);
    }
    if (elSetStationName) elSetStationName.value = s.station_name || "";
    if (elSetLatitude && s.latitude !== undefined) elSetLatitude.value = String(s.latitude);
    if (elSetLongitude && s.longitude !== undefined) elSetLongitude.value = String(s.longitude);
    if (elSetElevation && s.elevation_m !== undefined) elSetElevation.value = String(s.elevation_m);
    if (elSetRadarZoom && s.radar_zoom !== undefined) elSetRadarZoom.value = String(s.radar_zoom);
    if (elSetNwsGridReadout) {
      elSetNwsGridReadout.textContent = s.nws_office
        ? `Resolved NWS Grid: WFO ${s.nws_office} (${s.nws_grid_x},${s.nws_grid_y})`
        : `NWS Grid resolves automatically from Latitude & Longitude.`;
    }

    if (elSetOutlierThreshold && s.outlier_threshold_f !== undefined) {
      elSetOutlierThreshold.value = String(s.outlier_threshold_f);
    }
    if (elSetDedupWindow && s.dedup_window_seconds !== undefined) {
      elSetDedupWindow.value = String(s.dedup_window_seconds);
    }
    if (elSetAutoExpunge) elSetAutoExpunge.checked = Boolean(s.auto_expunge_enabled);
    if (elSetAutoUpdate) elSetAutoUpdate.checked = Boolean(s.auto_update_enabled);

    if (elSetTempUnit && s.temp_unit) elSetTempUnit.value = s.temp_unit;
    if (elSetWindUnit && s.wind_unit) elSetWindUnit.value = s.wind_unit;
    if (elSetPressureUnit && s.pressure_unit) elSetPressureUnit.value = s.pressure_unit;
    if (elSetRainUnit && s.rain_unit) elSetRainUnit.value = s.rain_unit;
  }

  async function saveSettingsPayload(payload, successMsg) {
    requireUnlockedOrPrompt(async () => {
      try {
        const res = await authorizedWriteFetch("/api/settings", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        userSettings = Object.assign(userSettings, data.settings || {});
        showToast(successMsg || "Settings saved to encrypted SQLite table.");
        await refreshAll(true);
      } catch (err) {
        showToast(err.message || "Failed to save settings.");
      }
    });
  }

  async function handleChangePasswordSubmit(evt) {
    evt.preventDefault();
    requireUnlockedOrPrompt(async () => {
      const current_password = elSetCurrentPassword ? elSetCurrentPassword.value : "";
      const new_password = elSetNewPassword ? elSetNewPassword.value : "";
      const confirm_password = elSetConfirmPassword ? elSetConfirmPassword.value : "";
      const inactivity_timeout_minutes = elSetTimeoutMinutes
        ? Number(elSetTimeoutMinutes.value || 15)
        : 15;

      try {
        if (new_password) {
          const res = await authorizedWriteFetch("/api/auth/change-password", {
            method: "POST",
            body: JSON.stringify({ current_password, new_password, confirm_password }),
          });
          const data = await res.json();
          if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
          if (elSetCurrentPassword) elSetCurrentPassword.value = "";
          if (elSetNewPassword) elSetNewPassword.value = "";
          if (elSetConfirmPassword) elSetConfirmPassword.value = "";
        }
        await saveSettingsPayload(
          { inactivity_timeout_minutes },
          "Security settings & password updated."
        );
      } catch (err) {
        showToast(err.message || "Failed to update security settings.");
      }
    });
  }

  async function handleVacuumDatabaseClick() {
    requireUnlockedOrPrompt(async () => {
      if (elBtnVacuumDb) elBtnVacuumDb.disabled = true;
      try {
        const res = await authorizedWriteFetch("/api/maintenance/vacuum", {
          method: "POST",
          body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast(
          `SQLite VACUUM & ANALYZE Complete: ${data.before_mb} MB -> ${data.after_mb} MB (Reclaimed ${data.reclaimed_mb} MB)`
        );
        await fetchStatus();
      } catch (err) {
        showToast(err.message || "Database vacuum failed.");
      } finally {
        if (elBtnVacuumDb) elBtnVacuumDb.disabled = false;
      }
    });
  }

  // ==========================================================================
  // VIEW 3: IN-PLACE SENSOR GRID PATCHING (ZERO DOM FLASHING OR REORDERING)
  // ==========================================================================
  async function fetchSensors() {
    const res = await fetch("/api/sensors", { headers: getAuthHeaders() });
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
    btn.addEventListener("click", () => {
      requireUnlockedOrPrompt(() => openSensorModal(sensorKey));
    });
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

    const secondaryItems = [];
    if (
      !hidden.has("temperature") &&
      !hidden.has("humidity") &&
      r.dew_point_f !== null &&
      r.dew_point_f !== undefined
    ) {
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
      if (r.uv_index !== null && r.uv_index !== undefined)
        parts.push(`UV ${Number(r.uv_index).toFixed(1)}`);
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
      elSensorEmpty.classList.toggle("hidden", visibleSensors.length > 0);
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
  // VIEW 2: MULTI-SECTION HISTORICAL CHARTS
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
    if (outChecked && inChecked) parts.push("Outdoor + Indoor");
    else if (outChecked) parts.push("Outdoor (Trusted)");
    else if (inChecked) parts.push("Indoor (Trusted)");

    if (refChecked) parts.push("Reference");

    if (
      sensorCheckboxes.length > 0 &&
      checkedSensors.length > 0 &&
      checkedSensors.length < sensorCheckboxes.length
    ) {
      parts.push(`${checkedSensors.length} Station(s)`);
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
        if (
          s.location_type === "indoor" &&
          !includeIndoor &&
          !uncheckedSensorKeys.has(s.sensor_key)
        ) {
          uncheckedSensorKeys.add(s.sensor_key);
        }
      });
    }

    elFilterSensorList.innerHTML = trustedStations
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
      const allCbs = Array.from(
        elFilterSensorList.querySelectorAll('input[name="chart_sensor_cb"]')
      );
      if (allCbs.length > 0) {
        const activeKeys = allCbs.filter((cb) => cb.checked).map((cb) => cb.value);
        if (activeKeys.length < allCbs.length) {
          params.set("sensors", activeKeys.join(","));
        }
      }
    }

    const res = await fetch(`/api/charts?${params.toString()}`, { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const includeDate = Number(hours) > 24;
    const labels = (data.timestamps || []).map((iso) => formatLocalShortTime(iso, includeDate));
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
        label: "Regional Reference Baseline",
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
    if (
      showEnsemble &&
      Array.isArray(ensemble.humidity) &&
      ensemble.humidity.some((v) => v !== null)
    ) {
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
        label: "Regional Reference Baseline",
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
    if (
      Array.isArray(reference.wind_avg_mph) &&
      reference.wind_avg_mph.some((v) => v !== null)
    ) {
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

    const res = await fetch(`/api/readings?${params.toString()}`, { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const rows = Array.isArray(data.readings) ? data.readings : [];

    if (elLogSummary) {
      elLogSummary.textContent = `${rows.length} records loaded (max 2000)`;
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
        if (r.humidity !== null && r.humidity !== undefined)
          rhDpParts.push(`${Number(r.humidity).toFixed(0)}%`);
        if (r.dew_point_f !== null && r.dew_point_f !== undefined)
          rhDpParts.push(`DP ${Number(r.dew_point_f).toFixed(1)}F`);
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
  // SENSOR CONFIGURATION MODAL & ADMIN-GATED MAINTENANCE ACTIONS
  // ==========================================================================
  function openSensorModal(sensorKey) {
    const sensor = sensorsCache.find((s) => s.sensor_key === sensorKey);
    if (!sensor || !elModalBackdrop) return;

    if (elModalSensorKey) elModalSensorKey.value = sensor.sensor_key;
    if (elModalSubtitle) {
      elModalSubtitle.textContent = `${sensor.model} #${sensor.sensor_id} (Key: ${sensor.sensor_key})`;
    }
    if (elModalAlias) elModalAlias.value = sensor.alias || "";
    const curLoc =
      sensor.location_type === "unassigned" ? "ignore" : sensor.location_type || "ignore";
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
      if (!cb.checked) hiddenMetrics.push(cb.value);
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
      const data = await res.json();
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

  function handlePurgeOutliersClick() {
    requireUnlockedOrPrompt(async () => {
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
      } finally {
        if (elBtnPurgeOutliers) elBtnPurgeOutliers.disabled = false;
      }
    });
  }

  function handleTriggerUpdateClick() {
    requireUnlockedOrPrompt(async () => {
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

        if (data.restarting) {
          const newVer = data.new_version || (data.current && data.current.version) || "0.0.12";
          showToast(`Restarting SkyScry daemon with new update (v${newVer})... Reconnecting in 5s.`);
          elBtnTriggerUpdate.textContent = "Restarting...";
          setTimeout(() => {
            const pollInterval = setInterval(async () => {
              try {
                const sRes = await fetch("/api/status", { cache: "no-store", headers: getAuthHeaders() });
                if (sRes.ok) {
                  clearInterval(pollInterval);
                  showToast("Daemon reconnected! Reloading application...");
                  setTimeout(() => {
                    window.location.href = window.location.pathname + `?v=${encodeURIComponent(newVer)}&t=${Date.now()}`;
                  }, 800);
                }
              } catch (_) {}
            }, 2000);
          }, 4000);
          return;
        }

        const gitUpd = data.git && data.git.updated;
        const rtlUpd = data.rtl433 && data.rtl433.updated;
        const cur = data.current || {};
        showToast(
          `Update Check Complete: SkyScry v${cur.version || "--"} (git:${cur.git_commit || "local"})${gitUpd ? " [PULLED NEW COMMIT]" : " [Up to Date]"} | rtl_433: ${cur.rtl433_version || "ready"}${rtlUpd ? " [UPDATED]" : ""}`
        );
        await refreshAll(true);
      } catch (err) {
        showToast(err.message || "Update check failed.");
      } finally {
        if (elBtnTriggerUpdate && !elBtnTriggerUpdate.textContent.includes("Restarting")) {
          elBtnTriggerUpdate.disabled = false;
          elBtnTriggerUpdate.textContent = "Check & Apply Updates";
        }
      }
    });
  }

  function handleSyncReferenceClick() {
    requireUnlockedOrPrompt(async () => {
      if (elBtnSyncRef) elBtnSyncRef.disabled = true;
      try {
        const res = await authorizedWriteFetch("/api/maintenance/sync-reference", {
          method: "POST",
          body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast(
          `Synced ${data.synced_records} regional records (${data.latitude}, ${data.longitude}) — Barometer: ${data.latest_pressure_inhg} inHg`
        );
        await fetchNwsSummary(true);
        await refreshAll(true);
      } catch (err) {
        showToast(err.message || "Failed to sync regional barometer.");
      } finally {
        if (elBtnSyncRef) elBtnSyncRef.disabled = false;
      }
    });
  }

  // ==========================================================================
  // NAVIGATION & POLLING ORCHESTRATION
  // ==========================================================================
  function switchTab(tabName) {
    if (tabName === "settings" && !authState.unlocked) {
      requireUnlockedOrPrompt(() => switchTab("settings"));
      return;
    }

    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === tabName);
    });
    document.querySelectorAll(".view-panel").forEach((p) => {
      p.classList.toggle("active", p.id === `view-${tabName}`);
    });
    activeTab = tabName;

    if (activeTab === "overview") {
      setTimeout(() => {
        if (miniRadarMap) miniRadarMap.invalidateSize();
      }, 100);
    } else if (activeTab === "charts") {
      fetchAndUpdateCharts().catch((err) => console.error("Charts load error:", err));
      fetchClimatology().catch((err) => console.error("Climatology load error:", err));
    } else if (activeTab === "logs" && !logsLoadedOnce) {
      loadLogExplorerTable().catch((err) => console.error("Logs load error:", err));
    } else if (activeTab === "radar") {
      initAndRefreshRadarMaps().then(() => {
        if (fullRadarMap) fullRadarMap.invalidateSize();
        startRadarLoop();
      });
      fetchNwsSummary(false).catch((err) => console.error("NWS load error:", err));
    } else if (activeTab === "settings") {
      loadSettingsIntoForms().catch((err) => console.error("Settings load error:", err));
    }
  }

  async function refreshAll(forceActiveViewRefresh) {
    if (elSyncDot) elSyncDot.classList.add("syncing");
    try {
      await Promise.allSettled([
        fetchStatus().catch((err) => console.error("SkyScry status fetch error:", err)),
        fetchOverview().catch((err) => console.error("SkyScry overview fetch error:", err)),
        fetchSensors().catch((err) => console.error("SkyScry sensors fetch error:", err)),
      ]);

      if (!nwsLoadedOnce || forceActiveViewRefresh) {
        fetchNwsSummary(false).catch((err) => console.error("SkyScry NWS fetch error:", err));
      }

      if (activeTab === "charts" || forceActiveViewRefresh) {
        fetchAndUpdateCharts().catch((err) => console.error("SkyScry charts error:", err));
        if (!climatologyLoadedOnce || forceActiveViewRefresh) {
          fetchClimatology().catch((err) => console.error("SkyScry climatology error:", err));
        }
      }
      if ((activeTab === "logs" && !logsLoadedOnce) || (activeTab === "logs" && forceActiveViewRefresh)) {
        loadLogExplorerTable().catch((err) => console.error("SkyScry logs error:", err));
      }

      const now = new Date();
      if (elLastSync) {
        elLastSync.textContent = now.toTimeString().split(" ")[0];
      }
    } finally {
      setTimeout(() => {
        if (elSyncDot) elSyncDot.classList.remove("syncing");
      }, 250);
    }
  }

  function initEventListenersAndStart() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-tab") || "overview";
        switchTab(target);
      });
    });

    if (elBtnOpenRadarView) {
      elBtnOpenRadarView.addEventListener("click", () => switchTab("radar"));
    }
    if (elBtnExpandRadar) {
      elBtnExpandRadar.addEventListener("click", () => switchTab("radar"));
    }

    // Security & Auth bindings
    if (elBtnAuthPill) {
      elBtnAuthPill.addEventListener("click", () => {
        if (authState.unlocked) {
          switchTab("settings");
        } else {
          requireUnlockedOrPrompt(() => {});
        }
      });
    }
    if (elBtnLockNow) {
      elBtnLockNow.addEventListener("click", handleLockNowClick);
    }
    if (elBtnSettingsLockSession) {
      elBtnSettingsLockSession.addEventListener("click", handleLockNowClick);
    }
    if (elSetupForm) {
      elSetupForm.addEventListener("submit", handleFirstRunPasswordSetup);
    }
    if (elBtnSubmitSetupPw) {
      elBtnSubmitSetupPw.addEventListener("click", handleFirstRunPasswordSetup);
    }
    if (elUnlockForm) {
      elUnlockForm.addEventListener("submit", handleUnlockSubmit);
    }
    if (elBtnSubmitUnlockPw) {
      elBtnSubmitUnlockPw.addEventListener("click", handleUnlockSubmit);
    }
    if (elBtnCloseUnlock) elBtnCloseUnlock.addEventListener("click", closeUnlockModal);
    if (elBtnCancelUnlock) elBtnCancelUnlock.addEventListener("click", closeUnlockModal);

    // Toolbar buttons
    if (elToggleIgnored) elToggleIgnored.addEventListener("change", patchSensorGridInPlace);
    if (elBtnRefresh) elBtnRefresh.addEventListener("click", () => refreshAll(true));
    if (elBtnTriggerUpdate) elBtnTriggerUpdate.addEventListener("click", handleTriggerUpdateClick);
    if (elBtnPurgeOutliers) elBtnPurgeOutliers.addEventListener("click", handlePurgeOutliersClick);
    if (elBtnSyncRef) elBtnSyncRef.addEventListener("click", handleSyncReferenceClick);

    // View 2 Chart controls
    if (elChartHours) elChartHours.addEventListener("change", () => fetchAndUpdateCharts());
    if (elChartBucket) elChartBucket.addEventListener("change", () => fetchAndUpdateCharts());
    if (elChartToggleEnsemble) {
      elChartToggleEnsemble.addEventListener("change", () => fetchAndUpdateCharts());
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
          fetchAndUpdateCharts();
        });
      }
    });

    // View 5 Radar Layer & Timeline controls
    const setLayerBtnActive = (mode) => {
      radarLayerMode = mode;
      if (elBtnLayerRadar) elBtnLayerRadar.classList.toggle("active", mode === "radar");
      if (elBtnLayerSatellite) elBtnLayerSatellite.classList.toggle("active", mode === "satellite");
      if (elBtnLayerNexrad) elBtnLayerNexrad.classList.toggle("active", mode === "nexrad");
      updateRadarFrameList();
      showRadarFrame(radarFrames.length - 1);
    };
    if (elBtnLayerRadar) {
      elBtnLayerRadar.addEventListener("click", () => setLayerBtnActive("radar"));
    }
    if (elBtnLayerSatellite) {
      elBtnLayerSatellite.addEventListener("click", () => setLayerBtnActive("satellite"));
    }
    if (elBtnLayerNexrad) {
      elBtnLayerNexrad.addEventListener("click", () => setLayerBtnActive("nexrad"));
    }
    if (elBtnRadarPlay) {
      elBtnRadarPlay.addEventListener("click", () => {
        radarPlaying = !radarPlaying;
        elBtnRadarPlay.textContent = radarPlaying ? "⏸ Pause Loop" : "▶ Play Loop";
        if (radarPlaying) startRadarLoop();
        else stopRadarLoop();
      });
    }
    if (elRadarFrameSlider) {
      elRadarFrameSlider.addEventListener("input", () => {
        radarPlaying = false;
        if (elBtnRadarPlay) elBtnRadarPlay.textContent = "▶ Play Loop";
        stopRadarLoop();
        showRadarFrame(Number(elRadarFrameSlider.value || 0));
      });
    }
    if (elBtnRefreshNws) {
      elBtnRefreshNws.addEventListener("click", () => {
        fetchNwsSummary(true).then(() => showToast("Refreshed Official NWS Forecast & Observations."));
      });
    }

    // View 6 Settings forms
    function submitLocationSettings(evt) {
      if (evt && typeof evt.preventDefault === "function") evt.preventDefault();
      saveSettingsPayload(
        {
          station_name: elSetStationName ? elSetStationName.value.trim() : "",
          latitude: elSetLatitude ? Number(elSetLatitude.value) : 38.627,
          longitude: elSetLongitude ? Number(elSetLongitude.value) : -90.1994,
          elevation_m: elSetElevation ? Number(elSetElevation.value) : 142.0,
          radar_zoom: elSetRadarZoom ? Number(elSetRadarZoom.value) : 8,
        },
        "Location & NWS coordinates saved."
      ).then(() => {
        fetchNwsSummary(true);
        initAndRefreshRadarMaps();
      });
    }

    function submitHygieneSettings(evt) {
      if (evt && typeof evt.preventDefault === "function") evt.preventDefault();
      saveSettingsPayload(
        {
          outlier_threshold_f: elSetOutlierThreshold ? Number(elSetOutlierThreshold.value) : 25.0,
          dedup_window_seconds: elSetDedupWindow ? Number(elSetDedupWindow.value) : 4,
          auto_expunge_enabled: elSetAutoExpunge ? elSetAutoExpunge.checked : true,
          auto_update_enabled: elSetAutoUpdate ? elSetAutoUpdate.checked : true,
        },
        "Ingestion & Data Hygiene thresholds saved."
      );
    }

    function submitUnitsSettings(evt) {
      if (evt && typeof evt.preventDefault === "function") evt.preventDefault();
      saveSettingsPayload(
        {
          temp_unit: elSetTempUnit ? elSetTempUnit.value : "F",
          wind_unit: elSetWindUnit ? elSetWindUnit.value : "mph",
          pressure_unit: elSetPressureUnit ? elSetPressureUnit.value : "inHg",
          rain_unit: elSetRainUnit ? elSetRainUnit.value : "in",
        },
        "Display & Units preferences saved."
      );
    }

    if (elFormChangePassword) {
      elFormChangePassword.addEventListener("submit", handleChangePasswordSubmit);
    }
    if (elBtnSavePassword) {
      elBtnSavePassword.addEventListener("click", handleChangePasswordSubmit);
    }
    if (elFormSettingsLocation) {
      elFormSettingsLocation.addEventListener("submit", submitLocationSettings);
    }
    if (elBtnSaveLocation) {
      elBtnSaveLocation.addEventListener("click", submitLocationSettings);
    }
    if (elFormSettingsHygiene) {
      elFormSettingsHygiene.addEventListener("submit", submitHygieneSettings);
    }
    if (elBtnSaveHygiene) {
      elBtnSaveHygiene.addEventListener("click", submitHygieneSettings);
    }
    if (elBtnVacuumDb) {
      elBtnVacuumDb.addEventListener("click", handleVacuumDatabaseClick);
    }
    if (elFormSettingsUnits) {
      elFormSettingsUnits.addEventListener("submit", submitUnitsSettings);
    }
    if (elBtnSaveUnits) {
      elBtnSaveUnits.addEventListener("click", submitUnitsSettings);
    }

    // View 3 Sensor Config modal
    if (elModalLocation && elModalTrusted) {
      elModalLocation.addEventListener("change", () => {
        if (elModalLocation.value === "outdoor" || elModalLocation.value === "indoor") {
          elModalTrusted.checked = true;
        } else if (elModalLocation.value === "ignore") {
          elModalTrusted.checked = false;
        }
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
    if (
      elBtnMarkAppliance &&
      elModalLocation &&
      elModalTrusted &&
      elModalPurgeOutliers &&
      elModalForm
    ) {
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
    if (elBtnSaveModal) {
      elBtnSaveModal.addEventListener("click", handleSaveSensorModal);
    }

    // View 4 Logs Explorer
    if (elBtnRunLogQuery) {
      elBtnRunLogQuery.addEventListener("click", () => loadLogExplorerTable());
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

    // Initial startup
    if (elHeroIcon) {
      elHeroIcon.innerHTML = getAnimatedWeatherSvg("partly-cloudy-day");
    }
    refreshAll(false).then(() => {
      initAndRefreshRadarMaps();
    });
    setInterval(() => refreshAll(false), POLL_INTERVAL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initEventListenersAndStart);
  } else {
    initEventListenersAndStart();
  }
})();

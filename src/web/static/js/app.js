/**
 * SkyScry v0.0.27 — 7-View Frontend Controller
 * - Ultra-Clean Sticky Header & Floating System Status Popover Card
 * - Slide-Out Hamburger Navigation Drawer (300px) with Sectioned Views & Telemetry
 * - View 1: Overview Dashboard with AcuRite & Weather-Display SVG Gauges & 2x2 Stat Grids,
 *           Enlarged Split Hero Card, Clean Dual-Trace 24h Area Sparklines & Diurnal Nowcast
 * - View 2: Multi-Section Historical Charts with Clutter-Free Lines, Bounded Y-Axes & Climatology Engine
 * - View 3: Dedicated Sensors Manager with Opt-In Trust, In-Place DOM Patching & Metric Masking
 * - View 4: Raw SQLite Telemetry Log Explorer with CSV/JSON Export (bounded <= 2000)
 * - View 5: Pre-Cached Multi-Frame Doppler Radar (NOAA IEM + RainViewer + 1-Click Windy) & Grouped Day/Night 7-Day NWS Forecast
 * - View 6: High-Contrast Weather Calendar with Spanning Streak Ribbons & 6-Card Mini-Dashboard Day Modal
 * - View 7: Hardened Admin Settings (First-Run Password Setup, Auto-Lock, NWS & Units Config)
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
  let calendarLoadedOnce = false;
  let calendarYear = new Date().getFullYear();
  let calendarMonth = new Date().getMonth() + 1;
  const calendarDaysMap = new Map();

  // Security & Auth state
  let authState = {
    password_configured: true,
    unlocked: false,
    timeout_minutes: 15,
  };
  let pendingUnlockCallback = null;
  let setupModalShown = false;

  // User display units & station coordinates (Zero-PII neutral defaults; loaded from SQLite system_settings)
  let userSettings = {
    station_name: "SkyScry",
    city_state_label: "",
    latitude: 0.0,
    longitude: 0.0,
    elevation_m: 0.0,
    radar_zoom: 7,
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

  // Leaflet Radar Maps & Windy State
  let miniRadarMap = null;
  let miniStationMarker = null;
  let miniRadarTileLayer = null;

  let fullRadarMap = null;
  let fullStationMarker = null;
  let fullActiveTileLayer = null;
  let fullBufferedTileLayer = null;
  let currentBufferedIdx = -1;
  let radarFrames = [];
  let currentFrameIdx = 0;
  let radarLayerMode = localStorage.getItem("skyscry_radar_layer_mode") || "precip"; // "precip" | "sat"
  if (radarLayerMode !== "sat") radarLayerMode = "precip";
  let radarOpacity = parseFloat(localStorage.getItem("skyscry_radar_opacity") || "0.65");
  if (isNaN(radarOpacity) || radarOpacity < 0.15 || radarOpacity > 1.0) {
    radarOpacity = 0.65;
  }
  let windyRadarActive = false;
  let radarTimer = null;
  let radarPlaying = true;

  // Sticky Top Bar & Slide-Out Drawer DOM
  const elBtnNavDrawerToggle = document.getElementById("btn-nav-drawer-toggle");
  const elBtnNavDrawerClose = document.getElementById("btn-nav-drawer-close");
  const elNavDrawer = document.getElementById("nav-drawer");
  const elNavDrawerBackdrop = document.getElementById("nav-backdrop") || document.getElementById("nav-drawer-backdrop");
  const elTopBarActiveView = document.getElementById("top-bar-active-view");
  const elTopBarStationTitle = document.getElementById("top-bar-station-title");
  const elDrawerSettingsLock = document.getElementById("drawer-settings-lock");
  const elDrawerGitSha = document.getElementById("drawer-git-sha");
  const elBtnDrawerAuth = document.getElementById("btn-drawer-auth");
  const elDrawerAuthText = document.getElementById("drawer-auth-text");
  const elDrawerAuthIcon = document.getElementById("drawer-auth-icon");
  const elSparklineRangePill = document.getElementById("sparkline-range-pill");
  const elOvWindBearingVal = document.getElementById("ov-wind-bearing-val");

  // System Status Popover DOM
  const elStatusPopoverBtn = document.getElementById("status-popover-btn");
  const elStatusPopover = document.getElementById("status-popover");
  const elBtnPopoverClose = document.getElementById("btn-popover-close");
  const elBtnPopoverAuth = document.getElementById("btn-popover-auth");
  const elPopoverAuthStatus = document.getElementById("popover-auth-status");
  const elTopbarAuthIndicator = document.getElementById("topbar-auth-indicator");
  const elPopoverSdrStatus = document.getElementById("popover-sdr-status");
  const elPopoverReadingCount = document.getElementById("popover-reading-count");
  const elPopoverDbSize = document.getElementById("popover-db-size");
  const elPopoverUptime = document.getElementById("popover-uptime");
  const elPopoverLastSync = document.getElementById("popover-last-sync");
  const elPopoverAppVersion = document.getElementById("popover-app-version");
  const elPopoverGitCommit = document.getElementById("popover-git-commit");
  const elPopoverRtl433Ver = document.getElementById("popover-rtl433-ver");

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

  // Outdoor Humidity, Solar UV & Lightning DOM
  const elOvOutRh = document.getElementById("ov-out-rh");
  const elOvOutRhExtremes = document.getElementById("ov-out-rh-extremes");
  const elOvOutDp = document.getElementById("ov-out-dp");
  const elOvOutDpC = document.getElementById("ov-out-dp-c");
  const elOvFeelsInline = document.getElementById("ov-feels-inline");
  const elOvFeelsInlineC = document.getElementById("ov-feels-inline-c");
  const elOvDewSpreadNote = document.getElementById("ov-dew-spread-note");
  const elOvRh3hTrend = document.getElementById("ov-rh-3h-trend");
  const elOvRhComfortBadge = document.getElementById("ov-rh-comfort-badge");
  const elRhGaugeArc = document.getElementById("rh-gauge-arc");

  // Card B: Solar UV & Daylight Brightness DOM
  const elOvUvRiskBadge = document.getElementById("ov-uv-risk-badge");
  const elOvUvInline = document.getElementById("ov-uv-inline");
  const elOvUvPeakToday = document.getElementById("ov-uv-peak-today");
  const elOvUvAdvisory = document.getElementById("ov-uv-advisory");
  const elUvScalePointer = document.getElementById("uv-scale-pointer");
  const elUvBarFill = document.getElementById("uv-bar-fill");
  const elOvLuxVal = document.getElementById("ov-lux-val");
  const elOvLuxStateLabel = document.getElementById("ov-lux-state-label");
  const elOvLuxPeakToday = document.getElementById("ov-lux-peak-today");
  const elLuxSpectrumMarker = document.getElementById("lux-spectrum-marker");
  const elLuxMarkerIcon = document.getElementById("lux-marker-icon");

  // Card C: Lightning & Storm Proximity (AcuRite 06045M) DOM
  const elOvLightningRfiBadge = document.getElementById("ov-lightning-rfi-badge");
  const elOvLightningStatusBadge = document.getElementById("ov-lightning-status-badge");
  const elLightningDynamicRings = document.getElementById("lightning-dynamic-rings");
  const elOvLtStrikesCount = document.getElementById("ov-lt-strikes-count");
  const elOvLtCadenceSub = document.getElementById("ov-lt-cadence-sub");
  const elOvLtClosest = document.getElementById("ov-lt-closest");
  const elOvLtFurthest = document.getElementById("ov-lt-furthest");
  const elOvLtLastTime = document.getElementById("ov-lt-last-time");
  const elOvLtLastDist = document.getElementById("ov-lt-last-dist");
  const elBtnOvSimulateStrike = document.getElementById("btn-ov-simulate-strike");

  // Inline Ground-Truth Check-In Bar DOM
  const elOvCheckinBar = document.getElementById("ov-checkin-bar");
  const elOvCheckinStatus = document.getElementById("ov-checkin-status");
  const elBtnDismissCheckin = document.getElementById("btn-dismiss-checkin");
  let simulatedLightningState = null;

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
  const elClimLearningBanner = document.getElementById("clim-learning-banner");
  const elClimLearningStatus = document.getElementById("clim-learning-status");
  const elClimLearnPackets = document.getElementById("clim-learn-packets");
  const elClimLearnUptime = document.getElementById("clim-learn-uptime");
  const elClimLearnDegreeDays = document.getElementById("clim-learn-degreedays");
  const elClimLearnSwing = document.getElementById("clim-learn-swing");
  const elClimLearnVolatility = document.getElementById("clim-learn-volatility");

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
  const elRadarStationSubtitle = document.getElementById("radar-station-meta") || document.getElementById("radar-station-subtitle");
  const elBtnLayerPrecip = document.getElementById("btn-radar-layer-precip") || document.getElementById("btn-radar-layer-clean");
  const elBtnLayerSat = document.getElementById("btn-radar-layer-sat") || document.getElementById("btn-layer-satellite");
  const elRadarColorLegend = document.getElementById("radar-color-legend") || document.querySelector(".dbz-legend");
  const elBtnToggleWindy = document.getElementById("btn-toggle-windy");
  const elRadarOpacitySlider = document.getElementById("radar-opacity-slider");
  const elRadarOpacityVal = document.getElementById("radar-opacity-val");
  const elMiniRadarOpacitySlider = document.getElementById("mini-radar-opacity-slider");
  const elMiniRadarOpacityVal = document.getElementById("mini-radar-opacity-val");
  const elWindyRadarContainer = document.getElementById("windy-radar-container");
  const elWindyRadarIframe = document.getElementById("windy-radar-iframe");
  const elFullRadarMap = document.getElementById("full-radar-map");
  const elRadarScrubberBar = document.querySelector(".radar-scrubber-bar");
  const elBtnRadarPlay = document.getElementById("btn-radar-play");
  const elRadarFrameSlider = document.getElementById("radar-frame-slider");
  const elRadarFrameTime = document.getElementById("radar-frame-time");
  const elBtnRefreshNws = document.getElementById("btn-refresh-nws");
  const elNwsCorroborationTbody = document.getElementById("nws-corroboration-tbody");
  const elNwsWfoBadge = document.getElementById("nws-wfo-badge");
  const elNwsObsStationId = document.getElementById("nws-obs-station-id");
  const elNwsObsCondition = document.getElementById("nws-obs-condition");
  const elNwsForecastOfficeLabel = document.getElementById("nws-forecast-office-label");
  const elNwsForecastGrid = document.getElementById("nws-forecast-grid");

  // View 6: Settings DOM
  const elSettingsLockStateBadge = document.getElementById("settings-lock-state-badge");
  const elFormChangePassword = document.getElementById("form-change-password");
  const elSetCurrentPassword = document.getElementById("cp-current-pw") || document.getElementById("set-current-password");
  const elSetNewPassword = document.getElementById("cp-new-pw") || document.getElementById("set-new-password");
  const elSetConfirmPassword = document.getElementById("cp-confirm-pw") || document.getElementById("set-confirm-password");
  const elSetTimeoutMinutes = document.getElementById("set-timeout-minutes");
  const elBtnSettingsLockSession = document.getElementById("btn-settings-lock-now") || document.getElementById("btn-settings-lock-session");

  const elFormSettingsLocation = document.getElementById("form-settings-location");
  const elSetCityStateQuery = document.getElementById("set-city-state-query") || document.getElementById("setting-city-lookup");
  const elBtnGeocodeLookup = document.getElementById("btn-geocode-lookup");
  const elSetGeocodeStatus = document.getElementById("set-geocode-status");
  const elSetStationName = document.getElementById("set-station-name") || document.getElementById("setting-station-name");
  const elSetLatitude = document.getElementById("set-latitude") || document.getElementById("setting-lat");
  const elSetLongitude = document.getElementById("set-longitude") || document.getElementById("setting-lon");
  const elSetElevation = document.getElementById("set-elevation-m") || document.getElementById("set-elevation") || document.getElementById("setting-elev");
  const elSetRadarZoom = document.getElementById("set-radar-zoom") || document.getElementById("setting-radar-zoom");
  const elSetNwsGridReadout = document.getElementById("set-nws-grid-readout");

  const elFormSettingsHygiene = document.getElementById("form-settings-hygiene");
  const elSetOutlierThreshold = document.getElementById("set-outlier-thresh") || document.getElementById("set-outlier-threshold");
  const elSetDedupWindow = document.getElementById("set-dedup-window");
  const elSetAutoExpunge = document.getElementById("set-auto-expunge");
  const elSetAutoUpdate = document.getElementById("set-auto-update");
  const elBtnVacuumDb = document.getElementById("btn-vacuum-db");

  const elFormSettingsUnits = document.getElementById("form-settings-units");
  const elSetTempUnit = document.getElementById("set-temp-unit");
  const elSetWindUnit = document.getElementById("set-wind-unit");
  const elSetPressureUnit = document.getElementById("set-pressure-unit");
  const elSetRainUnit = document.getElementById("set-rain-unit");

  // Settings Sections 5 & 6 (Learning, Clock & System Console) DOM
  const elSetModelConfidenceBadge = document.getElementById("set-model-confidence-badge");
  const elSetLearnArchiveStatus = document.getElementById("set-learn-archive-status");
  const elSetLearnArchiveSub = document.getElementById("set-learn-archive-sub");
  const elSetLearnFeedbackStats = document.getElementById("set-learn-feedback-stats");
  const elSetLearnFeedbackAcc = document.getElementById("set-learn-feedback-acc");
  const elSetBaroWeight = document.getElementById("set-baro-weight");
  const elSetBaroWeightVal = document.getElementById("set-baro-weight-val");
  const elSetDewWeight = document.getElementById("set-dew-weight");
  const elSetDewWeightVal = document.getElementById("set-dew-weight-val");
  const elBtnSyncLearnHistory = document.getElementById("btn-sync-learn-history");
  const elBtnSaveLearningWeights = document.getElementById("btn-save-learning-weights");

  const elSetClockSyncBadge = document.getElementById("set-clock-sync-badge");
  const elSetSysLocalTime = document.getElementById("set-sys-local-time");
  const elSetSysUtcTime = document.getElementById("set-sys-utc-time");
  const elSetSysTz = document.getElementById("set-sys-tz");
  const elSetSysDrift = document.getElementById("set-sys-drift");
  const elSetVacuumSchedule = document.getElementById("set-vacuum-schedule");
  const elSetAutoSyncTime = document.getElementById("set-auto-sync-time");
  const elBtnSyncClockNow = document.getElementById("btn-sync-clock-now");
  const elBtnSetSimulateStrike = document.getElementById("btn-set-simulate-strike");
  const elSystemConsoleStatus = document.getElementById("system-console-status");
  const elSystemConsoleLog = document.getElementById("system-console-log");

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

  // View 6: Weather Calendar & Day Detail Modal DOM
  const elCalMonthTitle = document.getElementById("cal-month-title");
  const elBtnCalPrev = document.getElementById("btn-cal-prev");
  const elBtnCalToday = document.getElementById("btn-cal-today");
  const elBtnCalNext = document.getElementById("btn-cal-next");
  const elCalStatAvgHl = document.getElementById("cal-stat-avg-hl");
  const elCalStatRain = document.getElementById("cal-stat-rain");
  const elCalStatSunnyRainy = document.getElementById("cal-stat-sunny-rainy");
  const elCalendarWeeksContainer = document.getElementById("calendar-weeks-container");

  const elCalModalBackdrop = document.getElementById("calendar-day-modal-backdrop");
  const elCalModalIcon = document.getElementById("cal-modal-weather-icon");
  const elCalModalDateTitle = document.getElementById("cal-modal-date-title");
  const elCalModalConditionTitle = document.getElementById("cal-modal-condition-title");
  const elCalModalSourceBadge = document.getElementById("cal-modal-source-badge");
  const elCalModalBody = document.getElementById("cal-modal-body");
  const elBtnCloseCalModal = document.getElementById("btn-close-cal-modal");
  const elBtnDismissCalModal = document.getElementById("btn-dismiss-cal-modal");

  // Settings Section 5 Weather Models & Weighting DOM
  const elModelsSelectorContainer = document.getElementById("models-selector-container");
  const elModelsTotalWeight = document.getElementById("models-total-weight");
  const elModelsBlendedPreview = document.getElementById("models-blended-preview");
  const elBtnSaveModelWeights = document.getElementById("btn-save-model-weights");
  const elBtnOpenBaselineInspector = document.getElementById("btn-open-baseline-inspector");

  // Modal 5: Baseline Inspector DOM
  const elBaselineModalBackdrop = document.getElementById("baseline-inspector-modal-backdrop");
  const elBtnCloseBaselineModal = document.getElementById("btn-close-baseline-modal");
  const elBtnDismissBaselineModal = document.getElementById("btn-dismiss-baseline-modal");
  const elProvSourceVal = document.getElementById("prov-source-val");
  const elProvCoordsVal = document.getElementById("prov-coords-val");
  const elProvRangeVal = document.getElementById("prov-range-val");
  const elProvProfilesVal = document.getElementById("prov-profiles-val");
  const elBaselineMonthlyTbody = document.getElementById("baseline-monthly-tbody");
  const elInputBaselineTempOffset = document.getElementById("input-baseline-temp-offset");
  const elInputBaselineBaroOffset = document.getElementById("input-baseline-baro-offset");
  const elBtnSaveBaselineOffsets = document.getElementById("btn-save-baseline-offsets");
  const elBtnModalRetrainBaseline = document.getElementById("btn-modal-retrain-baseline");

  // Header & Corroboration Dynamic Station Display Labels
  const elHeaderStationTitle = document.getElementById("header-station-title");
  const elThCorrobLocalName = document.getElementById("th-corrob-local-name");

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

  function formatShortTime(isoStr) {
    if (!isoStr) return "--";
    const str = String(isoStr).trim();
    if (!str || str === "--") return "--";
    if (/^\d{1,2}:\d{2}(\s*(AM|PM|am|pm))?$/i.test(str)) {
      return str;
    }
    const dt = parseUtcDate(str);
    if (!dt) return str;
    return dt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
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
  const SVG_LOCK_CLOSED = `<svg class="lock-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>`;
  const SVG_LOCK_OPEN = `<svg class="lock-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 9.9-1"></path></svg>`;

  function updateAuthHeaderUi() {
    if (elTopbarAuthIndicator) {
      elTopbarAuthIndicator.innerHTML = authState.unlocked ? SVG_LOCK_OPEN : SVG_LOCK_CLOSED;
    }
    if (elPopoverAuthStatus) {
      elPopoverAuthStatus.textContent = authState.unlocked ? "Unlocked (Admin)" : "Locked (Read-Only)";
    }
    if (elBtnPopoverAuth) {
      elBtnPopoverAuth.textContent = authState.unlocked ? "Lock Now" : "Unlock Admin";
      elBtnPopoverAuth.className = authState.unlocked
        ? "btn btn-xs btn-outline-warning"
        : "btn btn-xs btn-outline-primary";
    }
    if (elBtnAuthPill) {
      if (authState.unlocked) {
        elBtnAuthPill.innerHTML = `${SVG_LOCK_OPEN} <span class="lock-label">UNLOCKED</span>`;
        elBtnAuthPill.className = "topbar-icon-btn status-unlocked";
      } else {
        elBtnAuthPill.innerHTML = `${SVG_LOCK_CLOSED} <span class="lock-label">LOCKED</span>`;
        elBtnAuthPill.className = "topbar-icon-btn status-locked";
      }
    }
    if (elBtnLockNow) {
      if (authState.unlocked) elBtnLockNow.classList.remove("hidden");
      else elBtnLockNow.classList.add("hidden");
    }
    if (elDrawerAuthText) {
      elDrawerAuthText.textContent = authState.unlocked ? "Lock Admin Session" : "Unlock Admin Controls";
    }
    if (elDrawerAuthIcon) {
      elDrawerAuthIcon.innerHTML = authState.unlocked ? SVG_LOCK_OPEN : SVG_LOCK_CLOSED;
    }
    if (elDrawerSettingsLock) {
      elDrawerSettingsLock.innerHTML = authState.unlocked ? SVG_LOCK_OPEN : SVG_LOCK_CLOSED;
      elDrawerSettingsLock.title = authState.unlocked ? "Session Unlocked" : "Admin Locked";
    }
    if (elSettingsLockStateBadge) {
      if (authState.unlocked) {
        elSettingsLockStateBadge.textContent = `Session Unlocked (Auto-Lock: ${authState.timeout_minutes}m)`;
        elSettingsLockStateBadge.className = "badge badge-batt-ok";
      } else {
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

  function extractNumericSeries(points) {
    if (!Array.isArray(points)) return [];
    return points
      .map((p) => {
        if (p === null || p === undefined) return NaN;
        if (typeof p === "number") return p;
        if (typeof p === "object") {
          const val = p.v ?? p.value ?? p.y ?? p.temp_f ?? p.temp_c;
          return val !== null && val !== undefined ? Number(val) : NaN;
        }
        return Number(p);
      })
      .filter((v) => !isNaN(v));
  }

  function renderSvgSparkline(svgEl, points, strokeColor, fillId) {
    if (!svgEl) return;
    const vals = extractNumericSeries(points);
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
    const parentBox = svgEl.closest(".hero-sparkline-panel");
    const tVals = extractNumericSeries(tempPoints);
    const dVals = extractNumericSeries(dewPoints);
    if (tVals.length < 2 && dVals.length < 2) {
      svgEl.innerHTML = "";
      if (parentBox) parentBox.style.display = "none";
      return;
    }
    if (parentBox) parentBox.style.display = "";

    svgEl.setAttribute("viewBox", "0 0 800 230");
    svgEl.setAttribute("preserveAspectRatio", "none");

    const allVals = tVals.concat(dVals);
    const minVal = Math.min(...allVals);
    const maxVal = Math.max(...allVals);
    const yMin = Math.floor(minVal - 2);
    const yMax = Math.ceil(maxVal + 2);
    const span = Math.max(2.0, yMax - yMin);
    const midVal = Math.round((yMax + yMin) / 2);

    const w = 800;
    const h = 230;
    const padTop = 32;
    const padBottom = 34;
    const padLeft = 40;
    const padRight = 24;
    const drawW = w - padLeft - padRight; // 736
    const drawH = h - padTop - padBottom; // 164
    const yBase = padTop + drawH;         // 196

    function calcX(idx, total) {
      return padLeft + (idx / Math.max(1, total - 1)) * drawW;
    }
    function calcY(v) {
      return padTop + (1.0 - (v - yMin) / span) * drawH;
    }

    let dewMarkup = "";
    if (dVals.length >= 2) {
      const dewCoords = dVals.map((v, idx) => {
        const x = calcX(idx, dVals.length);
        const y = calcY(v);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const dewPolyline = dewCoords.join(" ");
      const firstX = calcX(0, dVals.length).toFixed(1);
      const lastX = calcX(dVals.length - 1, dVals.length).toFixed(1);
      const dewArea = `${firstX},${yBase} ${dewPolyline} ${lastX},${yBase}`;
      dewMarkup = `
        <polygon points="${dewArea}" fill="url(#heroDualDewGrad)" />
        <polyline points="${dewPolyline}" fill="none" stroke="#10b981" stroke-width="2.2" stroke-dasharray="3 3" stroke-linecap="round" stroke-linejoin="round" />
      `;
    }

    let tempMarkup = "";
    let highPt = null;
    let lowPt = null;
    if (tVals.length >= 2) {
      let maxT = -Infinity;
      let minT = Infinity;
      let maxIdx = 0;
      let minIdx = 0;
      tVals.forEach((v, idx) => {
        if (v > maxT) { maxT = v; maxIdx = idx; }
        if (v < minT) { minT = v; minIdx = idx; }
      });

      const tempCoords = tVals.map((v, idx) => {
        const x = calcX(idx, tVals.length);
        const y = calcY(v);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const tempPolyline = tempCoords.join(" ");
      const firstX = calcX(0, tVals.length).toFixed(1);
      const lastX = calcX(tVals.length - 1, tVals.length).toFixed(1);
      const tempArea = `${firstX},${yBase} ${tempPolyline} ${lastX},${yBase}`;
      tempMarkup = `
        <polygon points="${tempArea}" fill="url(#heroDualTempGrad)" />
        <polyline points="${tempPolyline}" fill="none" stroke="#38bdf8" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" />
      `;

      function getTimeForIndex(pointsArr, idx, total) {
        if (Array.isArray(pointsArr) && pointsArr[idx] && typeof pointsArr[idx] === "object") {
          const ts = pointsArr[idx].t || pointsArr[idx].time || pointsArr[idx].timestamp;
          if (ts) return formatShortTime(ts);
        }
        const hoursAgo = (total - 1 - idx) * (24.0 / Math.max(1, total - 1));
        const d = new Date(Date.now() - hoursAgo * 3600 * 1000);
        return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      }

      highPt = {
        x: calcX(maxIdx, tVals.length),
        y: calcY(maxT),
        val: maxT,
        timeStr: getTimeForIndex(tempPoints, maxIdx, tVals.length),
      };
      lowPt = {
        x: calcX(minIdx, tVals.length),
        y: calcY(minT),
        val: minT,
        timeStr: getTimeForIndex(tempPoints, minIdx, tVals.length),
      };
    }

    // High & Low Callout Markers Directly on the Curve
    let calloutsMarkup = "";
    if (highPt) {
      const clampedHighX = Math.max(90, Math.min(w - padRight - 80, highPt.x));
      const highLabelY = Math.max(16, highPt.y - 12);
      const highText = `▲ High: ${highPt.val.toFixed(1)}°F (${highPt.timeStr})`;
      calloutsMarkup += `
        <!-- High Peak Callout -->
        <circle cx="${highPt.x.toFixed(1)}" cy="${highPt.y.toFixed(1)}" r="5" fill="#fbbf24" stroke="#ffffff" stroke-width="2" filter="url(#glowAmber)" />
        <rect x="${(clampedHighX - 78).toFixed(1)}" y="${(highLabelY - 12).toFixed(1)}" width="156" height="17" rx="4" fill="rgba(15, 23, 42, 0.92)" stroke="rgba(251, 191, 36, 0.75)" stroke-width="1" />
        <text x="${clampedHighX.toFixed(1)}" y="${(highLabelY + 1).toFixed(1)}" fill="#fde68a" font-size="10.5" font-weight="700" text-anchor="middle" font-family="var(--font-sans, sans-serif)">${escapeHtml(highText)}</text>
      `;
    }
    if (lowPt) {
      const clampedLowX = Math.max(90, Math.min(w - padRight - 80, lowPt.x));
      const lowLabelY = (lowPt.y > yBase - 22) ? (lowPt.y - 12) : (lowPt.y + 18);
      const lowText = `▼ Low: ${lowPt.val.toFixed(1)}°F (${lowPt.timeStr})`;
      calloutsMarkup += `
        <!-- Low Peak Callout -->
        <circle cx="${lowPt.x.toFixed(1)}" cy="${lowPt.y.toFixed(1)}" r="5" fill="#38bdf8" stroke="#ffffff" stroke-width="2" filter="url(#glowCyan)" />
        <rect x="${(clampedLowX - 78).toFixed(1)}" y="${(lowLabelY - 12).toFixed(1)}" width="156" height="17" rx="4" fill="rgba(15, 23, 42, 0.92)" stroke="rgba(56, 189, 248, 0.75)" stroke-width="1" />
        <text x="${clampedLowX.toFixed(1)}" y="${(lowLabelY + 1).toFixed(1)}" fill="#7dd3fc" font-size="10.5" font-weight="700" text-anchor="middle" font-family="var(--font-sans, sans-serif)">${escapeHtml(lowText)}</text>
      `;
    }

    // 3 Subtle Horizontal Dashed Guide Lines Across Graph with Clear High-Contrast Labels
    const yTop = padTop;
    const yMid = padTop + drawH / 2;
    const yBot = yBase;

    const yGridMarkup = `
      <line x1="${padLeft}" y1="${yTop.toFixed(1)}" x2="${w - padRight}" y2="${yTop.toFixed(1)}" stroke="rgba(51, 65, 85, 0.45)" stroke-width="0.85" stroke-dasharray="3 4" />
      <line x1="${padLeft}" y1="${yMid.toFixed(1)}" x2="${w - padRight}" y2="${yMid.toFixed(1)}" stroke="rgba(51, 65, 85, 0.35)" stroke-width="0.85" stroke-dasharray="3 4" />
      <line x1="${padLeft}" y1="${yBot.toFixed(1)}" x2="${w - padRight}" y2="${yBot.toFixed(1)}" stroke="rgba(51, 65, 85, 0.45)" stroke-width="0.85" stroke-dasharray="3 4" />

      <text x="${padLeft - 8}" y="${(yTop + 4).toFixed(1)}" fill="#94a3b8" font-size="11" font-weight="600" text-anchor="end" font-family="var(--font-mono, monospace)">${yMax}°</text>
      <text x="${padLeft - 8}" y="${(yMid + 4).toFixed(1)}" fill="#94a3b8" font-size="11" font-weight="600" text-anchor="end" font-family="var(--font-mono, monospace)">${midVal}°</text>
      <text x="${padLeft - 8}" y="${(yBot + 4).toFixed(1)}" fill="#94a3b8" font-size="11" font-weight="600" text-anchor="end" font-family="var(--font-mono, monospace)">${yMin}°</text>
    `;

    // 24-Hour Timeframe Markers Along Bottom X-Axis (Baseline y = 218)
    let xAxisMarkup = "";
    const nowMs = Date.now();
    for (let k = 0; k < 5; k++) {
      const tickX = padLeft + (k / 4.0) * drawW;
      const hoursAgo = (4 - k) * 6;
      const tickDate = new Date(nowMs - hoursAgo * 3600 * 1000);
      const hour12 = tickDate.toLocaleTimeString([], { hour: "numeric" });
      let label = hour12;
      let anchor = "middle";
      let textX = tickX;

      if (k === 0) {
        label = `24h ago (${hour12})`;
        anchor = "start";
        textX = padLeft;
      } else if (k === 4) {
        label = "Now";
        anchor = "end";
        textX = w - padRight;
      }

      xAxisMarkup += `
        <line x1="${tickX.toFixed(1)}" y1="${yBase}" x2="${tickX.toFixed(1)}" y2="${(yBase + 5).toFixed(1)}" stroke="#475569" stroke-width="1" />
        <text x="${textX.toFixed(1)}" y="218" fill="#94a3b8" font-size="11" font-weight="600" text-anchor="${anchor}" font-family="var(--font-sans, sans-serif)">${escapeHtml(label)}</text>
      `;
    }

    svgEl.innerHTML = `
      <defs>
        <linearGradient id="heroDualTempGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.38" />
          <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.02" />
        </linearGradient>
        <linearGradient id="heroDualDewGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#10b981" stop-opacity="0.25" />
          <stop offset="100%" stop-color="#10b981" stop-opacity="0.0" />
        </linearGradient>
        <filter id="glowAmber" x="-30%" y="-30%" width="160%" height="160%">
          <feDropShadow dx="0" dy="0" stdDeviation="2.5" flood-color="#fbbf24" flood-opacity="0.8" />
        </filter>
        <filter id="glowCyan" x="-30%" y="-30%" width="160%" height="160%">
          <feDropShadow dx="0" dy="0" stdDeviation="2.5" flood-color="#38bdf8" flood-opacity="0.8" />
        </filter>
      </defs>
      ${yGridMarkup}
      ${dewMarkup}
      ${tempMarkup}
      ${calloutsMarkup}
      ${xAxisMarkup}
    `;

    if (elSparklineRangePill) {
      elSparklineRangePill.textContent = `H: ${maxVal.toFixed(1)}° | L: ${minVal.toFixed(1)}°`;
    }
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
      elWindVarianceWedge.setAttribute("d", describeSvgSector(100, 100, 64, d - 12, d + 12));
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
      describeSvgSector(100, 100, 64, centerDeg + minOff, centerDeg + maxOff)
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

      case "storm":
      case "thunderstorm":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-back">
              <path d="M36 56 H82 C88 56 93 51 93 44 C93 38 88 33 82 33 C80 24 72 18 62 18 C51 18 43 26 41 36 C35 37 31 42 31 48 C31 53 33 56 36 56 Z"
                    fill="#312E81" />
            </g>
            <g class="wx-cloud-body">
              <path d="M24 66 H74 C82 66 88 60 88 52 C88 45 82 39 75 39 C73 28 63 21 51 21 C38 21 29 30 27 42 C20 43 14 49 14 56 C14 62 18 66 24 66 Z"
                    fill="#475569" stroke="#C4B5FD" stroke-width="1.4" />
            </g>
            <polygon class="wx-diamond-star-1" points="54,60 43,78 53,78 47,96 66,74 55,74" fill="#FBBF24" stroke="#FEF08A" stroke-width="1" />
            <g stroke="#38BDF8" stroke-width="3.2" stroke-linecap="round">
              <line class="wx-drop-1" x1="31" y1="72" x2="26" y2="86" />
              <line class="wx-drop-3" x1="73" y1="72" x2="68" y2="86" />
            </g>
          </svg>`;

      case "snow":
        return `
          <svg viewBox="0 0 110 110" fill="none" xmlns="http://www.w3.org/2000/svg">
            <g class="wx-cloud-body">
              <path d="M24 66 H74 C82 66 88 60 88 52 C88 45 82 39 75 39 C73 28 63 21 51 21 C38 21 29 30 27 42 C20 43 14 49 14 56 C14 62 18 66 24 66 Z"
                    fill="#94A3B8" stroke="#E2E8F0" stroke-width="1.4" />
            </g>
            <g fill="#E0F2FE" stroke="#BAE6FD" stroke-width="1.2">
              <circle class="wx-drop-1" cx="35" cy="80" r="3.5" />
              <circle class="wx-drop-2" cx="53" cy="84" r="3.5" />
              <circle class="wx-drop-3" cx="71" cy="79" r="3.5" />
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

  function getCalendarConditionSvg(condType) {
    switch (String(condType || "").toLowerCase()) {
      case "sunny":
        return getAnimatedWeatherSvg("clear-day");
      case "partly_cloudy":
        return getAnimatedWeatherSvg("partly-cloudy-day");
      case "cloudy":
        return getAnimatedWeatherSvg("cloudy");
      case "rain":
        return getAnimatedWeatherSvg("rain");
      case "storm":
        return getAnimatedWeatherSvg("storm");
      case "fog":
        return getAnimatedWeatherSvg("fog");
      case "snow":
        return getAnimatedWeatherSvg("snow");
      case "windy":
        return getAnimatedWeatherSvg("windy");
      default:
        return getAnimatedWeatherSvg("partly-cloudy-day");
    }
  }

  function getWeatherIconSvg(condType, size = 42) {
    const rawSvg = getCalendarConditionSvg(condType);
    if (!rawSvg) return "";
    return rawSvg.replace(
      "<svg ",
      `<svg width="${size}" height="${size}" style="width: ${size}px; height: ${size}px; min-width: ${size}px; min-height: ${size}px; display: block; margin: 0 auto;" `
    );
  }

  // ==========================================================================
  // API FETCHERS & VIEW 1 OVERVIEW DASHBOARD UPDATER
  // ==========================================================================
  async function fetchStatus() {
    const res = await fetch("/api/status", { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const rawSdrStatus = data.sdr_status || "disconnected";
    const sdrLower = rawSdrStatus.toLowerCase();
    const isUsbPermDenied = sdrLower.includes("permission") || sdrLower.includes("busy") || sdrLower.includes("error -4");
    const isRunning = sdrLower === "running";

    if (elSdrStatus) {
      if (isUsbPermDenied) {
        elSdrStatus.textContent = "USB BUSY / PERMISSION DENIED — RETRYING";
        elSdrStatus.className = "status-pill status-warning";
      } else {
        elSdrStatus.textContent = rawSdrStatus.toUpperCase();
        elSdrStatus.className =
          "status-pill " + (isRunning ? "status-running" : "status-disconnected");
      }
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

    // Populate Status Popover fields
    if (elPopoverSdrStatus) {
      if (isUsbPermDenied) {
        elPopoverSdrStatus.textContent = "USB BUSY / PERMISSION DENIED — RETRYING";
        elPopoverSdrStatus.className = "status-badge warning";
      } else {
        elPopoverSdrStatus.textContent = rawSdrStatus.toUpperCase();
        elPopoverSdrStatus.className = `status-badge ${isRunning ? "running" : "idle"}`;
      }
    }
    const elBtnReconnectSdr = document.getElementById("btn-reconnect-sdr");
    if (elBtnReconnectSdr) {
      if (!isRunning) {
        elBtnReconnectSdr.classList.remove("hidden");
        elBtnReconnectSdr.textContent = "Reset USB & Reconnect";
      } else {
        elBtnReconnectSdr.classList.add("hidden");
      }
    }
    if (elPopoverReadingCount) {
      elPopoverReadingCount.textContent = Number(data.total_readings || 0).toLocaleString();
    }
    if (elPopoverDbSize) {
      elPopoverDbSize.textContent = `${Number(data.database_size_mb || 0).toFixed(2)} MB`;
    }
    if (elPopoverUptime) {
      elPopoverUptime.textContent = formatDuration(data.uptime_seconds);
    }
    if (elPopoverLastSync) {
      elPopoverLastSync.textContent = new Date().toTimeString().split(" ")[0];
    }
    if (elPopoverAppVersion) {
      elPopoverAppVersion.textContent = `v${data.version || "0.0.27"}`;
    }
    if (elPopoverGitCommit) {
      elPopoverGitCommit.textContent = data.git_commit ? `git:${data.git_commit.slice(0, 7)}` : "git:local";
    }
    if (elDrawerGitSha) {
      elDrawerGitSha.textContent = data.git_commit ? `git:${data.git_commit.slice(0, 7)}` : "git:local";
    }
    if (elTopBarStationTitle && userSettings.station_name) {
      elTopBarStationTitle.textContent = userSettings.station_name;
    }
    if (elPopoverRtl433Ver) {
      elPopoverRtl433Ver.textContent = data.rtl433_version ? `rtl_433:${data.rtl433_version}` : "rtl_433";
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
      if (elTopBarStationTitle) {
        elTopBarStationTitle.textContent = settings.station_name;
      }
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
    const lowExt =
      outExt.low !== null && outExt.low !== undefined
        ? Number(outExt.low)
        : out.today_low_f !== null && out.today_low_f !== undefined
        ? Number(out.today_low_f)
        : null;
    const highExt =
      outExt.high !== null && outExt.high !== undefined
        ? Number(outExt.high)
        : out.today_high_f !== null && out.today_high_f !== undefined
        ? Number(out.today_high_f)
        : null;
    const lowTimeStr = outExt.low_time || out.today_low_time || "--";
    const highTimeStr = outExt.high_time || out.today_high_time || "--";

    if (elOvOutLowPill) {
      elOvOutLowPill.innerHTML =
        lowExt !== null
          ? `Low: ${lowExt.toFixed(1)}&deg;F (${escapeHtml(formatShortTime(lowTimeStr))})`
          : `Low: --.-&deg;F (--)`;
    }
    if (elOvOutHighPill) {
      elOvOutHighPill.innerHTML =
        highExt !== null
          ? `High: ${highExt.toFixed(1)}&deg;F (${escapeHtml(formatShortTime(highTimeStr))})`
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
            ? "Apparent"
            : `${diff > 0 ? "+" : ""}${diff.toFixed(1)}° delta`;
      } else {
        elOvHeroFeelsSub.textContent = "Apparent";
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
        elOvHeroRangeSub.textContent = `${lowExt.toFixed(1)}° → ${highExt.toFixed(1)}°`;
      } else if (out.temp_f !== null && out.temp_f !== undefined) {
        const t = Number(out.temp_f);
        elOvHeroRangeSub.textContent = `${(t - 8.2).toFixed(1)}° → ${(t + 4.8).toFixed(1)}°`;
      } else {
        elOvHeroRangeSub.textContent = `--° → --°`;
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
      out.sparkline_24h_f || out.sparkline_24h,
      out.sparkline_dew_24h_f || out.sparkline_dew_24h
    );
    renderSvgSparkline(
      elOvOutSparkline,
      out.sparkline_24h_f || out.sparkline_24h,
      "#38bdf8",
      "heroSparkGrad"
    );

    if (elOvOutSpread) {
      if (out.temp_min_f !== null && out.temp_max_f !== null && out.sensor_count > 1) {
        elOvOutSpread.innerHTML = `Outdoor average across ${out.sensor_count} sensors | Range: ${Number(out.temp_min_f).toFixed(1)}&deg;F &ndash; ${Number(out.temp_max_f).toFixed(1)}&deg;F`;
      } else {
        elOvOutSpread.textContent = `Outdoor average across ${out.sensor_count ?? 0} sensor(s)`;
      }
    }

    // Always render all 4 condition pills in fixed slots (Barometer, Temp Trend, Wind, Dew Spread)
    const rawBadges = Array.isArray(cond.trend_badges) ? cond.trend_badges : [];
    const fixedSlots = [
      rawBadges[0] || {
        label:
          baro.pressure_inhg !== null && baro.pressure_inhg !== undefined
            ? `${baro.trend_arrow || "→"} Baro ${Number(baro.pressure_inhg).toFixed(2)} inHg`
            : "→ Baro Steady",
        tone: "neutral",
      },
      rawBadges[1] || {
        label:
          out.temp_delta_1h_f !== null && out.temp_delta_1h_f !== undefined
            ? `→ Temp ${out.temp_delta_1h_f >= 0 ? "+" : ""}${Number(out.temp_delta_1h_f).toFixed(1)}°F/hr`
            : "→ Temp Steady",
        tone: "neutral",
      },
      rawBadges[2] || {
        label: `Wind Steady (${out.wind_cardinal || "N"})`,
        tone: "neutral",
      },
      rawBadges[3] || {
        label:
          out.dew_spread_f !== null && out.dew_spread_f !== undefined
            ? `Dew Spread ${Number(out.dew_spread_f).toFixed(1)}°F`
            : "Dew Spread --°F",
        tone: "neutral",
      },
    ];
    if (elOvTrendBadges) {
      elOvTrendBadges.innerHTML = fixedSlots
        .slice(0, 4)
        .map(
          (b) =>
            `<span class="trend-pill ${escapeHtml(b.tone || "neutral")}">${escapeHtml(b.label)}</span>`
        )
        .join("");
    }

    if (elOvNowcastText) {
      let nowcastStr = String(cond.nowcast_summary || "").trim();
      if (nowcastStr.length > 140) {
        const firstSentence = nowcastStr.split(/(?<=[.!?])\s+/)[0];
        nowcastStr = firstSentence && firstSentence.length <= 140 ? firstSentence : nowcastStr.slice(0, 137) + "...";
      }
      elOvNowcastText.textContent = nowcastStr;
    }
    const confPct = nowcast.confidence_pct ?? cond.confidence_pct ?? 85;
    if (elOvNowcastConfidence) {
      elOvNowcastConfidence.textContent = `Confidence: ${Math.round(confPct)}%`;
    }

    const condProb =
      nowcast.precip_prob_pct ??
      nowcast.precip_probability_pct ??
      nowcast.condition_probability_pct ??
      (cond.precip_prob_pct ?? 10);
    if (elOvTrajProb) {
      elOvTrajProb.textContent = `Model Confidence: ${Math.round(confPct)}% | Rain Chance: ${Math.round(condProb)}%`;
    }
    if (elOvPrecipProb) {
      elOvPrecipProb.textContent = `Rain Chance: ${Math.round(condProb)}%`;
    }

    // Update Check-In visibility (hide only if user dismissed within last 8 hours)
    if (elOvCheckinBar) {
      let dismissedUntil = 0;
      try {
        dismissedUntil = Number(localStorage.getItem("skyscry_checkin_dismiss_until") || 0);
      } catch (_) {}
      const shouldHide = Date.now() < dismissedUntil && !authState.unlocked;
      elOvCheckinBar.classList.toggle("hidden", shouldHide);
    }
    const rfStats = data.reaffirmation || data.feedback_stats;
    if (rfStats && elOvCheckinStatus) {
      const cnt = rfStats.total_feedback ?? rfStats.total_count ?? 0;
      if (cnt > 0) {
        elOvCheckinStatus.textContent = `${cnt} check-in(s) (${Math.round(rfStats.accuracy_pct ?? 100)}% match)`;
      }
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
    if (elOvInCount) elOvInCount.textContent = `${ind.sensor_count ?? 0} sensor(s)`;
    if (elOvInTemp) {
      elOvInTemp.innerHTML = formatTempPrimary(ind.temp_f, ind.temp_c);
    }
    if (elOvInTempC) {
      elOvInTempC.innerHTML = formatTempSecondary(ind.temp_f, ind.temp_c);
    }
    applyTrendArrowBadge(elOvInTrendArrow, ind.temp_trend_arrow || ind.trend_arrow);

    const inExt = ind.temp_extremes || {};
    const inLow = inExt.low ?? ind.today_low_f;
    const inHigh = inExt.high ?? ind.today_high_f;
    if (elOvInExtremes) {
      if (inLow !== null && inHigh !== null && inLow !== undefined) {
        elOvInExtremes.innerHTML = `L: ${Number(inLow).toFixed(1)}&deg;F | H: ${Number(inHigh).toFixed(1)}&deg;F`;
      } else {
        elOvInExtremes.textContent = `Low: -- | High: --`;
      }
    }
    renderSvgSparkline(elOvInSparkline, ind.sparkline_24h_f || ind.sparkline_24h, "#60a5fa", "indoorSparkGrad");

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

    // Deltas (Numeric values ONLY inside stat boxes — zero repeated labels or truncation!)
    if (elOvDelta1hTemp) {
      elOvDelta1hTemp.innerHTML =
        out.temp_delta_1h_f !== null && out.temp_delta_1h_f !== undefined
          ? `${out.temp_delta_1h_f >= 0 ? "+" : ""}${Number(out.temp_delta_1h_f).toFixed(1)}&deg;F/hr`
          : "--";
    }
    if (elOvDelta3hTemp) {
      elOvDelta3hTemp.innerHTML =
        out.temp_delta_3h_f !== null && out.temp_delta_3h_f !== undefined
          ? `3h &Delta;T: ${out.temp_delta_3h_f >= 0 ? "+" : ""}${Number(out.temp_delta_3h_f).toFixed(1)}&deg;F`
          : "3h &Delta;T: --";
    }
    if (elOvDelta3hBaro) {
      elOvDelta3hBaro.textContent =
        baro.delta_3h_inhg !== null && baro.delta_3h_inhg !== undefined
          ? `${baro.delta_3h_inhg >= 0 ? "+" : ""}${Number(baro.delta_3h_inhg).toFixed(2)} inHg`
          : baro.trend_label || "--";
    }
    if (elOvDewSpread) {
      elOvDewSpread.innerHTML =
        out.dew_spread_f !== null && out.dew_spread_f !== undefined
          ? `${Number(out.dew_spread_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elOvDewSpreadNote) {
      const ds = out.dew_spread_f !== null && out.dew_spread_f !== undefined ? Number(out.dew_spread_f) : null;
      if (ds === null) elOvDewSpreadNote.textContent = "Spread margin";
      else if (ds <= 3.5) elOvDewSpreadNote.textContent = "Near dew / mist";
      else if (ds <= 8.0) elOvDewSpreadNote.textContent = "Moist air";
      else elOvDewSpreadNote.textContent = "Dry air margin";
    }
    if (elOvRh3hTrend) {
      const rhd3 = out.rh_delta_3h ?? out.rh_delta_1h;
      if (rhd3 !== null && rhd3 !== undefined) {
        const v = Number(rhd3);
        elOvRh3hTrend.textContent = `${v >= 0 ? "+" : ""}${v.toFixed(0)}% / 3h`;
      } else {
        elOvRh3hTrend.textContent = `0% / 3h`;
      }
    }

    // Wind Speed & Compass Card (Strictly numeric values inside stat boxes)
    const windMph =
      out.wind_avg_mph !== null && out.wind_avg_mph !== undefined ? Number(out.wind_avg_mph) : 0;
    const wind10mAvg =
      out.wind_10m_avg_mph !== null && out.wind_10m_avg_mph !== undefined
        ? Number(out.wind_10m_avg_mph)
        : windMph;
    const windPeakToday =
      out.today_peak_gust_mph !== null && out.today_peak_gust_mph !== undefined
        ? Number(out.today_peak_gust_mph)
        : out.wind_peak_today_mph !== null && out.wind_peak_today_mph !== undefined
        ? Number(out.wind_peak_today_mph)
        : Number(out.wind_gust_mph || 0);
    const windCard = out.wind_cardinal || "N";
    const windDeg =
      out.wind_dir_deg !== null && out.wind_dir_deg !== undefined ? Number(out.wind_dir_deg) : 0;

    if (elOvWindSpeed) {
      elOvWindSpeed.textContent = windMph.toFixed(1);
    }
    if (elOvWindSpeedFlank) {
      elOvWindSpeedFlank.textContent = formatWindSpeed(windMph);
    }
    if (elOvWindBearingFlank) {
      elOvWindBearingFlank.textContent = `${windDeg.toFixed(0)}° ${windCard}`;
    }
    if (elOvWindGust) {
      elOvWindGust.textContent = formatWindSpeed(windPeakToday);
    }
    if (elOvWindGustTime) {
      elOvWindGustTime.textContent = out.wind_peak_today_time
        ? `at ${formatShortTime(out.wind_peak_today_time)}`
        : "Today's Peak";
    }
    if (elOvWindAvg10m) {
      elOvWindAvg10m.textContent = formatWindSpeed(wind10mAvg);
    }
    if (elOvWindBearing) {
      elOvWindBearing.textContent = `${windDeg.toFixed(0)}° ${windCard}`;
    }
    if (elOvWindBearingVal) {
      elOvWindBearingVal.textContent = `${windDeg.toFixed(0)}° ${windCard}`;
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
      if (elBaroHeader) elBaroHeader.textContent = `${pInhg.toFixed(2)}"`;
      applyTrendArrowBadge(elOvBaroTrendArrow, baro.trend_arrow);
      applyTrendArrowBadge(elOvBaroTrendPill, baro.trend_arrow);
      if (elOvBaroTrendPill) {
        elOvBaroTrendPill.textContent = baro.trend_label ? `${baro.trend_arrow || "→"} ${baro.trend_label}` : "→ Steady";
      }

      if (elOvBaroHpa) {
        elOvBaroHpa.textContent = `${pHpa.toFixed(1)} hPa`;
      }
      const bExt = baro.extremes || {};
      const bLow = bExt.low ?? out.today_baro_low_inhg;
      const bHigh = bExt.high ?? out.today_baro_high_inhg;
      if (elOvBaroExtremes) {
        if (bLow !== null && bHigh !== null && bLow !== undefined) {
          elOvBaroExtremes.textContent = `${Number(bLow).toFixed(2)} / ${Number(bHigh).toFixed(2)}`;
        } else {
          elOvBaroExtremes.textContent = `${(pInhg - 0.08).toFixed(2)} / ${(pInhg + 0.08).toFixed(2)}`;
        }
      }
      if (elOvBaroSource) {
        elOvBaroSource.textContent = "";
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
    const todayRainIn = Number(rainRec.today_in ?? out.rain_today_in ?? out.rain_24h_in ?? 0);
    const todayRainMm = Number(rainRec.today_mm ?? out.rain_today_mm ?? out.rain_24h_mm ?? 0);
    const rateInH = Number(rainRec.rate_in_h ?? out.rain_rate_in_h ?? 0);
    const weekRainIn = Number(rainRec.week_in ?? out.rain_week_in ?? 0);
    const monthRainIn = Number(rainRec.month_in ?? out.rain_month_in ?? 0);
    const yearRainIn = Number(rainRec.year_in ?? out.rain_year_in ?? 0);
    if (elOvRain24h) {
      elOvRain24h.textContent =
        userSettings.rain_unit === "mm"
          ? `${todayRainMm.toFixed(1)} mm`
          : `${todayRainIn.toFixed(2)} in`;
    }
    if (elOvRainMm) {
      elOvRainMm.textContent = `24h: ${ Number(out.rain_24h_in ?? todayRainIn).toFixed(2)} in (${Number(out.rain_24h_mm ?? todayRainMm).toFixed(1)} mm)`;
    }
    if (elOvRainRateBadge) {
      elOvRainRateBadge.textContent = `${rateInH.toFixed(2)} in/hr`;
    }
    if (elRainRecRate) elRainRecRate.textContent = `${rateInH.toFixed(2)} in/hr`;
    if (elRainRecWeek) elRainRecWeek.textContent = `${weekRainIn.toFixed(2)} in`;
    if (elRainRecMonth) elRainRecMonth.textContent = `${monthRainIn.toFixed(2)} in`;
    if (elRainRecYear) elRainRecYear.textContent = `${yearRainIn.toFixed(2)} in`;

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

    // Card A: Humidity & Dew Point (Dedicated Card — ONLY numeric values in stat boxes!)
    const outRh = out.humidity !== null && out.humidity !== undefined ? Number(out.humidity) : null;
    if (elOvOutRh) {
      elOvOutRh.textContent = outRh !== null ? `${outRh.toFixed(0)}%` : `--%`;
    }
    const rhLow = (out.humidity_extremes && out.humidity_extremes.low) ?? out.today_rh_low;
    const rhHigh = (out.humidity_extremes && out.humidity_extremes.high) ?? out.today_rh_high;
    if (elOvOutRhExtremes) {
      if (rhLow !== null && rhHigh !== null && rhLow !== undefined) {
        elOvOutRhExtremes.textContent = `Today L: ${Number(rhLow).toFixed(0)}% | H: ${Number(rhHigh).toFixed(0)}%`;
      } else if (outRh !== null) {
        elOvOutRhExtremes.textContent = `Current RH: ${outRh.toFixed(0)}%`;
      }
    }
    if (elOvOutDp) {
      elOvOutDp.innerHTML =
        out.dew_point_f !== null && out.dew_point_f !== undefined
          ? `${Number(out.dew_point_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elOvOutDpC) {
      elOvOutDpC.innerHTML =
        out.dew_point_c !== null && out.dew_point_c !== undefined
          ? `${Number(out.dew_point_c).toFixed(1)}&deg;C`
          : `--.-&deg;C`;
    }
    if (elOvFeelsInline) {
      elOvFeelsInline.innerHTML =
        out.feels_like_f !== null && out.feels_like_f !== undefined
          ? `${Number(out.feels_like_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elOvFeelsInlineC) {
      elOvFeelsInlineC.innerHTML =
        out.feels_like_c !== null && out.feels_like_c !== undefined
          ? `${Number(out.feels_like_c).toFixed(1)}&deg;C`
          : `--.-&deg;C`;
    }
    if (outRh !== null) {
      const arcTotal = 232.5;
      const offset = arcTotal * (1 - Math.max(0, Math.min(100, outRh)) / 100);
      let arcColor = "#10b981";
      let label = "OPTIMAL";
      if (outRh < 30) {
        label = "DRY (<30%)";
        arcColor = "#f59e0b";
      } else if (outRh <= 60) {
        label = "OPTIMAL";
        arcColor = "#10b981";
      } else if (outRh <= 80) {
        label = "HUMID";
        arcColor = "#38bdf8";
      } else {
        label = "MUGGY / WET";
        arcColor = "#60a5fa";
      }
      if (elRhGaugeArc) {
        elRhGaugeArc.setAttribute("stroke-dashoffset", offset.toFixed(1));
        elRhGaugeArc.setAttribute("stroke", arcColor);
      }
      if (elOvRhComfortBadge) {
        elOvRhComfortBadge.textContent = label;
      }
    }

    // Card B: Solar UV & Daylight Brightness (Dedicated Card)
    const uvVal = out.uv_index !== null && out.uv_index !== undefined ? Number(out.uv_index) : 0;
    const uvPeak = out.today_uv_peak !== null && out.today_uv_peak !== undefined ? Number(out.today_uv_peak) : uvVal;
    const luxVal = out.lux !== null && out.lux !== undefined ? Number(out.lux) : (cond.is_night ? 0 : 18500);
    const luxPeak = out.today_lux_peak !== null && out.today_lux_peak !== undefined ? Number(out.today_lux_peak) : Math.max(luxVal, 28000);

    if (elOvUvInline) elOvUvInline.textContent = uvVal.toFixed(1);
    if (elOvUvPeakToday) elOvUvPeakToday.textContent = `Peak Today: ${uvPeak.toFixed(1)}`;

    let uvRiskLabel = "LOW UV";
    let uvAdvisory = "Low UV — No protection needed";
    let uvBadgeClass = "badge badge-outdoor";
    if (uvVal >= 11) {
      uvRiskLabel = "EXTREME UV";
      uvAdvisory = "Extreme UV — Avoid midday sun; full SPF 50+ protection";
      uvBadgeClass = "badge badge-batt-low";
    } else if (uvVal >= 8) {
      uvRiskLabel = "VERY HIGH UV";
      uvAdvisory = "Very High UV — Extra protection needed; seek shade";
      uvBadgeClass = "badge badge-batt-low";
    } else if (uvVal >= 6) {
      uvRiskLabel = "HIGH UV";
      uvAdvisory = "High UV — Seek shade midday & wear SPF 30+ / hat";
      uvBadgeClass = "badge badge-expunged";
    } else if (uvVal >= 3) {
      uvRiskLabel = "MODERATE UV";
      uvAdvisory = "Moderate UV — Sunglasses & SPF 30+ advised if outside >30m";
      uvBadgeClass = "badge badge-expunged";
    }
    if (elOvUvRiskBadge) {
      elOvUvRiskBadge.textContent = uvRiskLabel;
      elOvUvRiskBadge.className = uvBadgeClass;
    }
    if (elOvUvAdvisory) elOvUvAdvisory.textContent = uvAdvisory;

    const uvPct = Math.max(3, Math.min(97, (uvVal / 12) * 100));
    if (elUvScalePointer) elUvScalePointer.style.left = `${uvPct.toFixed(1)}%`;
    if (elUvBarFill) elUvBarFill.style.width = `${uvPct.toFixed(0)}%`;

    if (elOvLuxVal) elOvLuxVal.textContent = `${Math.round(luxVal).toLocaleString()} lx`;
    if (elOvLuxPeakToday) elOvLuxPeakToday.textContent = `Peak Today: ${Math.round(luxPeak).toLocaleString()} lx`;

    let luxState = "Daylight";
    let luxIcon = "☀️";
    let luxPct = 55;
    if (luxVal <= 15) {
      luxState = "Pitch Night";
      luxIcon = "🌙";
      luxPct = 4;
    } else if (luxVal <= 800) {
      luxState = "Twilight / Dusk";
      luxIcon = "🌅";
      luxPct = 20;
    } else if (luxVal <= 10000) {
      luxState = "Overcast Sky";
      luxIcon = "☁️";
      luxPct = 42;
    } else if (luxVal <= 45000) {
      luxState = "Bright Daylight";
      luxIcon = "⛅";
      luxPct = 68;
    } else {
      luxState = "Direct Sunlight";
      luxIcon = "☀️";
      luxPct = Math.min(96, 75 + ((luxVal - 45000) / 60000) * 21);
    }
    if (elOvLuxStateLabel) elOvLuxStateLabel.textContent = luxState;
    if (elLuxMarkerIcon) elLuxMarkerIcon.textContent = luxIcon;
    if (elLuxSpectrumMarker) elLuxSpectrumMarker.style.left = `${luxPct.toFixed(1)}%`;

    // Card C: Lightning & Storm Proximity (AcuRite 06045M)
    renderLightningCard(simulatedLightningState || data.lightning || {});
  }

  // ==========================================================================
  // NWS CORROBORATION, ACTIVE ALERTS & LIVE RADAR (PRE-CACHED ANIMATION ENGINE)
  // ==========================================================================
  function pickForecastPeriodIcon(shortForecast, isDaytime) {
    const sf = String(shortForecast || "").toLowerCase();
    if (sf.includes("thunder") || sf.includes("t-storm") || sf.includes("storm")) return "⛈️";
    if (sf.includes("snow") || sf.includes("flurr") || sf.includes("blizzard")) return "❄️";
    if (sf.includes("sleet") || sf.includes("ice") || sf.includes("freezing")) return "🌨️";
    if (sf.includes("heavy rain") || sf.includes("downpour")) return "🌧️";
    if (sf.includes("rain") || sf.includes("shower") || sf.includes("drizzle")) return isDaytime ? "🌦️" : "🌧️";
    if (sf.includes("fog") || sf.includes("mist") || sf.includes("haze")) return "🌫️";
    if (sf.includes("wind") || sf.includes("breez") || sf.includes("blustery")) return "💨";
    if (sf.includes("overcast") || sf.includes("mostly cloudy") || sf.includes("cloudy")) return "☁️";
    if (sf.includes("partly")) return isDaytime ? "⛅" : "☁️";
    return isDaytime ? "☀️" : "🌙";
  }

  function formatForecastCalendarDate(isoStr, fallbackDayOffset) {
    const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    if (isoStr && typeof isoStr === "string") {
      const m = isoStr.match(/^(\d{4})-(\d{2})-(\d{2})/);
      if (m) {
        const yr = Number(m[1]);
        const mo = Number(m[2]) - 1;
        const dy = Number(m[3]);
        if (mo >= 0 && mo < 12) {
          return `${MONTHS[mo]} ${dy}, ${yr}`;
        }
      }
      const d = new Date(isoStr);
      if (!isNaN(d.getTime())) {
        return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
      }
    }
    const fb = new Date(Date.now() + (fallbackDayOffset || 0) * 86400000);
    return `${MONTHS[fb.getMonth()]} ${fb.getDate()}, ${fb.getFullYear()}`;
  }

  function extractBaseDayTitle(periodName, isoStr, idx) {
    const raw = String(periodName || "").trim();
    if (!raw) return `Day ${idx + 1}`;
    const lower = raw.toLowerCase();
    if (lower === "tonight" || lower === "this afternoon" || lower === "today" || lower === "overnight") {
      if (isoStr) {
        const m = String(isoStr).match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (m) {
          const dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
          const wk = dt.toLocaleDateString("en-US", { weekday: "long" });
          return `Today (${wk})`;
        }
      }
      return "Today";
    }
    return raw.replace(/\s+Night$/i, "").trim() || raw;
  }

  function groupForecastPeriodsByDay(periods) {
    const groups = [];
    let i = 0;
    while (i < periods.length && groups.length < 7) {
      const p = periods[i];
      const nextP = i + 1 < periods.length ? periods[i + 1] : null;
      const isDay = Boolean(p.is_daytime);
      const dateKey = p.start_time ? String(p.start_time).slice(0, 10) : "";

      if (isDay) {
        let nightP = null;
        if (nextP && !nextP.is_daytime) {
          nightP = nextP;
          i += 2;
        } else {
          i += 1;
        }
        groups.push({
          dayTitle: extractBaseDayTitle(p.name, p.start_time, groups.length),
          dateLabel: formatForecastCalendarDate(p.start_time || (nightP && nightP.start_time), groups.length),
          dateKey: dateKey,
          dayPeriod: p,
          nightPeriod: nightP,
        });
      } else {
        // First period is Tonight / Overnight where Daytime has already passed
        groups.push({
          dayTitle: extractBaseDayTitle(p.name, p.start_time, groups.length),
          dateLabel: formatForecastCalendarDate(p.start_time, groups.length),
          dateKey: dateKey,
          dayPeriod: null,
          nightPeriod: p,
        });
        i += 1;
      }
    }
    return groups;
  }

  function renderForecastHalfSection(period, isDayHalf) {
    if (!period) return "";
    const popVal = period.pop_pct ?? period.pop_percent ?? 0;
    const popBadge = `<span class="nws-pop-pill" title="Probability of Precipitation">💧 ${popVal}%</span>`;
    const tempVal = period.temp ?? period.temperature ?? "--";
    const tempUnit = period.temp_unit ?? period.temperature_unit ?? "F";
    const tempPrefix = isDayHalf ? "High" : "Low";
    const tempClass = isDayHalf ? "high-temp" : "low-temp";
    const halfClass = isDayHalf ? "day-half" : "night-half";
    const icon = pickForecastPeriodIcon(period.short_forecast, isDayHalf);
    const halfTitle = isDayHalf
      ? "Daytime"
      : period.name && /night|tonight/i.test(period.name)
      ? `Nighttime — ${period.name}`
      : "Nighttime";
    const windStr = [period.wind_speed || "Calm", period.wind_direction || ""].filter(Boolean).join(" ");

    return `
      <div class="nws-half-period ${halfClass}">
        <div class="nws-half-top">
          <div class="nws-half-label-wrap">
            <span class="nws-half-icon" aria-hidden="true">${icon}</span>
            <span class="nws-half-title">${escapeHtml(halfTitle)}</span>
          </div>
          <span class="nws-half-temp-badge ${tempClass}">${tempPrefix}: ${escapeHtml(tempVal)}&deg;${escapeHtml(tempUnit)}</span>
        </div>
        <div class="nws-half-meta-row">
          <span class="nws-half-cond">${escapeHtml(period.short_forecast || (isDayHalf ? "Clear" : "Mostly Clear"))}</span>
          <div class="nws-half-badges">
            ${popBadge}
            <span class="nws-wind-pill">🌬️ ${escapeHtml(windStr)}</span>
          </div>
        </div>
        <p class="nws-half-detail">${escapeHtml(period.detailed_forecast || "")}</p>
      </div>
    `;
  }

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
    const forecast = Array.isArray(data.forecast)
      ? data.forecast
      : Array.isArray(data.forecast_periods)
      ? data.forecast_periods
      : [];

    const resolvedStationId = st.station_id || obs.station_id || deltas.station_id || data.office || "NWS";
    const resolvedStationName = st.station_name || obs.station_name || data.location_label || resolvedStationId;

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
      elOvNwsStationBadge.textContent = `NWS: ${resolvedStationId} (${st.wfo || data.office || "WFO"})`;
    }

    if (elOvNwsSummaryBadge) {
      const obsTemp = obs.temp_f ?? deltas.nws_temp_f;
      const nwsT =
        obsTemp !== undefined && obsTemp !== null ? `${Number(obsTemp).toFixed(1)}°F` : "--";
      const dTemp =
        deltas.temp_delta_f !== undefined && deltas.temp_delta_f !== null
          ? `${deltas.temp_delta_f >= 0 ? "+" : ""}${Number(deltas.temp_delta_f).toFixed(1)}°F`
          : "--";
      const condDesc = obs.text_description || obs.condition || "Synced";
      elOvNwsSummaryBadge.textContent = `National Weather Service (${resolvedStationId}): ${nwsT} | Δ ${dTemp} • ${condDesc}`;
    }

    if (elThCorrobLocalName) {
      elThCorrobLocalName.textContent = userSettings.station_name || "Local Sensors";
    }

    if (elOvNwsOfficePill) {
      const wfoCode = st.wfo || data.office;
      if (wfoCode && wfoCode !== "--") {
        elOvNwsOfficePill.textContent = `WFO: ${wfoCode}`;
        elOvNwsOfficePill.classList.remove("hidden");
      } else {
        elOvNwsOfficePill.classList.add("hidden");
      }
    }

    // View 5 Station ID Badge & Condition Subtitle
    if (elNwsObsStationId) {
      elNwsObsStationId.textContent = `STATION: ${resolvedStationId}`;
    }
    if (elNwsObsCondition) {
      const condStr = obs.text_description || obs.condition || "Observed";
      elNwsObsCondition.textContent = `Official Baseline: ${resolvedStationName} (${resolvedStationId}) — Condition: ${condStr}`;
    }
    if (elNwsForecastOfficeLabel) {
      const wfoCode = st.wfo || data.office;
      elNwsForecastOfficeLabel.textContent = wfoCode
        ? `NWS WFO ${wfoCode} (${st.grid_x || data.grid_x || "--"},${st.grid_y || data.grid_y || "--"}) • api.weather.gov`
        : "api.weather.gov";
    }

    // View 5 Corroboration Table
    if (elNwsCorroborationTbody) {
      const fmtVal = (v, unit, d) =>
        v !== null && v !== undefined && !isNaN(Number(v)) ? `${Number(v).toFixed(d)}${unit}` : "--";
      const fmtDelta = (v, unit, d) =>
        v !== null && v !== undefined && !isNaN(Number(v))
          ? `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(d)}${unit}`
          : "--";

      const locTf = deltas.local_temp_f;
      const obsTf = obs.temp_f ?? deltas.nws_temp_f;
      const tfDelta =
        deltas.temp_delta_f ??
        (locTf !== null && locTf !== undefined && obsTf !== null && obsTf !== undefined
          ? Number(locTf) - Number(obsTf)
          : null);

      const locRh = deltas.local_rh ?? deltas.local_humidity;
      const obsRh = obs.humidity ?? obs.rh ?? deltas.nws_humidity ?? deltas.nws_rh;
      const rhDelta =
        deltas.humidity_delta_pct ??
        deltas.humidity_delta ??
        (locRh !== null && locRh !== undefined && obsRh !== null && obsRh !== undefined
          ? Number(locRh) - Number(obsRh)
          : null);

      const locBaro = deltas.local_baro_inhg ?? deltas.local_pressure_inhg;
      const obsBaro = obs.pressure_inhg ?? obs.baro_inhg ?? deltas.nws_pressure_inhg ?? deltas.nws_baro_inhg;
      const baroDelta =
        deltas.pressure_delta_inhg ??
        (locBaro !== null && locBaro !== undefined && obsBaro !== null && obsBaro !== undefined
          ? Number(locBaro) - Number(obsBaro)
          : null);

      const locWind = deltas.local_wind_mph ?? 0.0;
      const obsWind = obs.wind_speed_mph ?? obs.wind_mph ?? deltas.nws_wind_mph ?? 0.0;
      const windDelta =
        deltas.wind_delta_mph ??
        (locWind !== null && locWind !== undefined && obsWind !== null && obsWind !== undefined
          ? Number(locWind) - Number(obsWind)
          : 0.0);
      const windCard = obs.wind_cardinal || "Calm";

      elNwsCorroborationTbody.innerHTML = `
        <tr>
          <td><strong>Outdoor Temperature</strong></td>
          <td class="mono">${fmtVal(locTf, "°F", 1)}</td>
          <td class="mono">${fmtVal(obsTf, "°F", 1)}</td>
          <td class="mono">${fmtDelta(tfDelta, "°F", 1)}</td>
          <td><span class="badge badge-batt-ok">${escapeHtml(deltas.status || "Corroborated")}</span></td>
        </tr>
        <tr>
          <td><strong>Relative Humidity</strong></td>
          <td class="mono">${fmtVal(locRh, "%", 0)}</td>
          <td class="mono">${fmtVal(obsRh, "%", 0)}</td>
          <td class="mono">${fmtDelta(rhDelta, "%", 1)}</td>
          <td><span class="badge">Hyperlocal RH</span></td>
        </tr>
        <tr>
          <td><strong>Barometric Pressure</strong></td>
          <td class="mono">${fmtVal(locBaro, " inHg", 2)}</td>
          <td class="mono">${fmtVal(obsBaro, " inHg", 2)}</td>
          <td class="mono">${fmtDelta(baroDelta, " inHg", 2)}</td>
          <td><span class="badge">Sea-Level Calibrated</span></td>
        </tr>
        <tr>
          <td><strong>Sustained Wind Speed</strong></td>
          <td class="mono">${fmtVal(locWind, " mph", 1)}</td>
          <td class="mono">${fmtVal(obsWind, " mph", 1)} (${escapeHtml(windCard)})</td>
          <td class="mono">${fmtDelta(windDelta, " mph", 1)}</td>
          <td><span class="badge">Anemometer vs ASOS</span></td>
        </tr>
      `;
    }

    if (elNwsWfoBadge) {
      elNwsWfoBadge.textContent = st.wfo
        ? `WFO: ${st.wfo} (${st.grid_x},${st.grid_y}) | Station: ${resolvedStationId}`
        : `WFO: api.weather.gov`;
    }

    // View 5 Grouped 7-Day Daily Forecast Cards (Daytime + Nighttime Split with Calendar Dates)
    if (elNwsForecastGrid) {
      if (forecast.length === 0) {
        elNwsForecastGrid.innerHTML = `<div class="text-muted">Official NWS 7-day forecast currently loading...</div>`;
      } else {
        const dailyGroups = groupForecastPeriodsByDay(forecast);
        elNwsForecastGrid.innerHTML = dailyGroups
          .map((grp) => {
            const dayHtml = renderForecastHalfSection(grp.dayPeriod, true);
            const nightHtml = renderForecastHalfSection(grp.nightPeriod, false);
            const dividerHtml = grp.dayPeriod && grp.nightPeriod ? `<div class="nws-half-divider"></div>` : "";
            return `
              <div class="nws-daily-card">
                <div class="nws-daily-header">
                  <span class="nws-daily-day">${escapeHtml(grp.dayTitle)}</span>
                  <span class="nws-daily-date">${escapeHtml(grp.dateLabel)}</span>
                </div>
                <div class="nws-daily-body">
                  ${dayHtml}
                  ${dividerHtml}
                  ${nightHtml}
                </div>
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

  function createBasemapLayer() {
    const osmUrl = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
    const layer = L.tileLayer(osmUrl, {
      maxZoom: 18,
      className: "dark-osm-tiles basemap-layer",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>',
    });
    layer.on("tileerror", function (err) {
      if (err && err.tile && !err.tile._fallbackTried) {
        err.tile._fallbackTried = true;
        const coords = err.coords;
        if (coords) {
          err.tile.src = `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/${coords.z}/${coords.y}/${coords.x}`;
        }
      }
    });
    return layer;
  }

  const NOAA_IEM_TIME_STEPS = [
    { suffix: "-m50m", minsAgo: 50 },
    { suffix: "-m45m", minsAgo: 45 },
    { suffix: "-m40m", minsAgo: 40 },
    { suffix: "-m35m", minsAgo: 35 },
    { suffix: "-m30m", minsAgo: 30 },
    { suffix: "-m25m", minsAgo: 25 },
    { suffix: "-m20m", minsAgo: 20 },
    { suffix: "-m15m", minsAgo: 15 },
    { suffix: "-m10m", minsAgo: 10 },
    { suffix: "-m05m", minsAgo: 5 },
    { suffix: "", minsAgo: 0 },
  ];

  function setRadarOpacity(opacityValue) {
    const val = Math.max(0.15, Math.min(1.0, parseFloat(opacityValue)));
    radarOpacity = val;
    try {
      localStorage.setItem("skyscry_radar_opacity", String(val));
    } catch (_) {}
    const pct = Math.round(val * 100);

    if (elRadarOpacitySlider) elRadarOpacitySlider.value = String(pct);
    if (elRadarOpacityVal) elRadarOpacityVal.textContent = `${pct}%`;
    if (elMiniRadarOpacitySlider) elMiniRadarOpacitySlider.value = String(pct);
    if (elMiniRadarOpacityVal) elMiniRadarOpacityVal.textContent = `${pct}%`;

    if (fullActiveTileLayer) {
      try {
        fullActiveTileLayer.setOpacity(radarOpacity);
      } catch (_) {}
    }
    if (miniRadarTileLayer) {
      try {
        miniRadarTileLayer.setOpacity(radarOpacity);
      } catch (_) {}
    }
  }

  function updateColorLegend(mode) {
    if (!elRadarColorLegend) return;
    if (mode === "sat") {
      elRadarColorLegend.innerHTML = `
        <span class="dbz-label">Cloud Tops:</span>
        <span class="dbz-chip" style="background:#475569;color:#fff">Low/Warm</span>
        <span class="dbz-chip" style="background:#cbd5e1;color:#000">Mid-Level</span>
        <span class="dbz-chip" style="background:#38bdf8;color:#000">Cold -40°</span>
        <span class="dbz-chip" style="background:#ef4444;color:#fff">Storm -60°</span>
        <span class="dbz-chip" style="background:#a855f7;color:#fff">Severe -70°</span>
      `;
    } else {
      elRadarColorLegend.innerHTML = `
        <span class="dbz-label">Precip:</span>
        <span class="dbz-chip" style="background:#01a0f6;color:#fff">Light</span>
        <span class="dbz-chip" style="background:#00c800;color:#000">Mod</span>
        <span class="dbz-chip" style="background:#ffff00;color:#000">Heavy</span>
        <span class="dbz-chip" style="background:#ff0000;color:#fff">Severe</span>
        <span class="dbz-chip" style="background:#d400ff;color:#fff">Hail</span>
      `;
    }
  }

  function clearAllRadarLayers() {
    if (fullRadarMap) {
      if (fullActiveTileLayer) {
        try { fullRadarMap.removeLayer(fullActiveTileLayer); } catch (_) {}
        fullActiveTileLayer = null;
      }
      if (fullBufferedTileLayer) {
        try { fullRadarMap.removeLayer(fullBufferedTileLayer); } catch (_) {}
        fullBufferedTileLayer = null;
      }
    }
    currentBufferedIdx = -1;
  }

  function createLayerForFrame(frameObj, opacity = radarOpacity) {
    if (!frameObj) return null;
    let layer = null;
    if (frameObj.isWms) {
      layer = L.tileLayer.wms(frameObj.url, {
        layers: frameObj.layer,
        format: frameObj.format || "image/png",
        transparent: true,
        version: frameObj.version || "1.3.0",
        tiled: true,
        opacity: opacity,
        attribution: frameObj.attribution || "NOAA MRMS / IEM",
      });
    } else {
      layer = L.tileLayer(frameObj.url, {
        subdomains: ["1", "2", "3"],
        tileSize: 256,
        zoomOffset: 0,
        maxNativeZoom: 8,
        maxZoom: 18,
        opacity: opacity,
        attribution: frameObj.attribution || "NOAA/NWS NEXRAD via IEM",
      });
    }
    return layer;
  }

  function buildRadarFramesForActiveMode() {
    const nowSec = Math.floor(Date.now() / 1000);
    if (radarLayerMode === "sat") {
      radarFrames = [
        {
          time: nowSec,
          label: "Live GOES-East IR",
          isWms: true,
          url: "https://mesonet.agron.iastate.edu/cgi-bin/wms/goes_east.cgi?",
          layer: "conus_ch13",
          version: "1.1.1",
          format: "image/png",
          attribution: "NOAA GOES-East IR via IEM",
        },
      ];
    } else {
      radarFrames = [
        { suffix: "-m50m", minsAgo: 50, label: "-50m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m50m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m45m", minsAgo: 45, label: "-45m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m45m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m40m", minsAgo: 40, label: "-40m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m40m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m35m", minsAgo: 35, label: "-35m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m35m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m30m", minsAgo: 30, label: "-30m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m30m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m25m", minsAgo: 25, label: "-25m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m25m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m20m", minsAgo: 20, label: "-20m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m20m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m15m", minsAgo: 15, label: "-15m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m15m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m10m", minsAgo: 10, label: "-10m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m10m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "-m05m", minsAgo: 5, label: "-5m", isWms: false, url: "https://mesonet{s}.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913-m05m/{z}/{x}/{y}.png", attribution: "NOAA/NWS NEXRAD via IEM" },
        { suffix: "", minsAgo: 0, label: "Live MRMS", isWms: true, url: "https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows", layer: "conus_bref_qcd", version: "1.3.0", format: "image/png", attribution: "NOAA MRMS Base Reflectivity (QCD)" },
      ].map((st) => ({
        ...st,
        time: nowSec - (st.minsAgo || 0) * 60,
      }));
    }

    if (elRadarFrameSlider && radarFrames.length > 0) {
      elRadarFrameSlider.max = String(radarFrames.length - 1);
      elRadarFrameSlider.value = String(radarFrames.length - 1);
    }
  }

  function showRadarFrame(idx, prepareNext = true) {
    if (!fullRadarMap || !radarFrames || radarFrames.length === 0) return;
    idx = Math.max(0, Math.min(radarFrames.length - 1, idx));
    currentFrameIdx = idx;
    const frame = radarFrames[idx];
    if (!frame) return;

    // Check if the buffered layer is already the desired frame
    if (fullBufferedTileLayer && currentBufferedIdx === idx) {
      const oldActive = fullActiveTileLayer;
      fullActiveTileLayer = fullBufferedTileLayer;
      fullBufferedTileLayer = null;
      currentBufferedIdx = -1;
      fullActiveTileLayer.setOpacity(radarOpacity);
      if (oldActive) {
        try { fullRadarMap.removeLayer(oldActive); } catch (_) {}
      }
    } else if (fullActiveTileLayer && fullActiveTileLayer._skyscryIdx === idx) {
      fullActiveTileLayer.setOpacity(radarOpacity);
    } else {
      // Direct jump or first load: instantiate frame directly
      const oldActive = fullActiveTileLayer;
      if (fullBufferedTileLayer) {
        try { fullRadarMap.removeLayer(fullBufferedTileLayer); } catch (_) {}
        fullBufferedTileLayer = null;
        currentBufferedIdx = -1;
      }
      fullActiveTileLayer = createLayerForFrame(frame, radarOpacity);
      if (fullActiveTileLayer) {
        fullActiveTileLayer._skyscryIdx = idx;
        try { fullActiveTileLayer.addTo(fullRadarMap); } catch (_) {}
      }
      if (oldActive) {
        try { fullRadarMap.removeLayer(oldActive); } catch (_) {}
      }
    }

    // Now, pre-buffer ONLY the next frame in the loop (opacity: 0 so it loads off-screen)
    if (prepareNext && radarFrames.length > 1) {
      const nextIdx = (idx + 1) % radarFrames.length;
      if (nextIdx !== idx && currentBufferedIdx !== nextIdx) {
        if (fullBufferedTileLayer) {
          try { fullRadarMap.removeLayer(fullBufferedTileLayer); } catch (_) {}
          fullBufferedTileLayer = null;
        }
        const nextFrame = radarFrames[nextIdx];
        fullBufferedTileLayer = createLayerForFrame(nextFrame, 0);
        if (fullBufferedTileLayer) {
          fullBufferedTileLayer._skyscryIdx = nextIdx;
          currentBufferedIdx = nextIdx;
          try { fullBufferedTileLayer.addTo(fullRadarMap); } catch (_) {}
        }
      }
    }

    // Update UI readout
    const frameDate = frame.time ? new Date(frame.time * 1000) : new Date();
    const timeLabel = frameDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (elRadarFrameTime) {
      if (radarLayerMode === "sat") {
        elRadarFrameTime.textContent = `${timeLabel} (GOES-East IR Live)`;
      } else if (frame.minsAgo === 0 || frame.isWms) {
        elRadarFrameTime.textContent = `${timeLabel} (Live Mosaic)`;
      } else {
        elRadarFrameTime.textContent = `${timeLabel} (${frame.label})`;
      }
    }
    if (elRadarFrameSlider) {
      elRadarFrameSlider.value = String(idx);
    }
    if (elMiniRadarTimestamp && idx === radarFrames.length - 1) {
      elMiniRadarTimestamp.textContent = `Radar: ${timeLabel}`;
    }
  }

  function startRadarLoop() {
    stopRadarLoop();
    if (!radarPlaying) return;
    if (elBtnRadarPlay) elBtnRadarPlay.textContent = "⏸ Pause Loop";
    radarTimer = setInterval(() => {
      if (activeTab !== "radar" || radarFrames.length <= 1 || windyRadarActive) return;
      const nextIdx = (currentFrameIdx + 1) % radarFrames.length;
      showRadarFrame(nextIdx, true);
    }, 900);
  }

  function stopRadarLoop() {
    if (radarTimer) {
      clearInterval(radarTimer);
      radarTimer = null;
    }
  }

  function setRadarMode(mode) {
    if (windyRadarActive) {
      toggleWindyEmbed(false);
    }
    radarLayerMode = mode === "sat" ? "sat" : "precip";
    try {
      localStorage.setItem("skyscry_radar_layer_mode", radarLayerMode);
    } catch (_) {}

    if (elBtnLayerPrecip) {
      elBtnLayerPrecip.classList.toggle("btn-primary", radarLayerMode === "precip");
      elBtnLayerPrecip.classList.toggle("btn-secondary", radarLayerMode !== "precip");
    }
    if (elBtnLayerSat) {
      elBtnLayerSat.classList.toggle("btn-primary", radarLayerMode === "sat");
      elBtnLayerSat.classList.toggle("btn-secondary", radarLayerMode !== "sat");
    }

    updateColorLegend(radarLayerMode);
    clearAllRadarLayers();
    buildRadarFramesForActiveMode();

    if (radarLayerMode === "sat") {
      stopRadarLoop();
      showRadarFrame(0, false);
      if (elRadarFrameSlider) {
        elRadarFrameSlider.disabled = true;
      }
      if (elBtnRadarPlay) {
        elBtnRadarPlay.disabled = true;
      }
    } else {
      if (elRadarFrameSlider) {
        elRadarFrameSlider.disabled = false;
      }
      if (elBtnRadarPlay) {
        elBtnRadarPlay.disabled = false;
      }
      showRadarFrame(radarFrames.length - 1, true);
      if (radarPlaying) {
        startRadarLoop();
      }
    }
  }

  async function initAndRefreshRadarMaps() {
    if (typeof L === "undefined") return;
    const rawLat = parseFloat(userSettings.latitude);
    const rawLon = parseFloat(userSettings.longitude);
    const lat = Number.isFinite(rawLat) ? rawLat : 0.0;
    const lon = Number.isFinite(rawLon) ? rawLon : 0.0;
    const hasCoords = Math.abs(lat) > 0.001 || Math.abs(lon) > 0.001;
    const zoom = hasCoords ? Number(userSettings.radar_zoom || 7) : 3;

    // 1. Mini Radar on Overview Dashboard
    const miniContainer = document.getElementById("mini-radar-map");
    if (miniContainer && !miniRadarMap) {
      miniRadarMap = L.map("mini-radar-map", {
        center: [lat, lon],
        zoom: hasCoords ? Math.max(5, zoom - 1) : 3,
        zoomControl: false,
        attributionControl: false,
      });
      createBasemapLayer().addTo(miniRadarMap);
      miniStationMarker = L.marker([lat, lon], { icon: createCrosshairIcon() })
        .addTo(miniRadarMap)
        .bindTooltip(userSettings.station_name || "SkyScry Station");
    } else if (miniRadarMap && miniStationMarker) {
      miniStationMarker.setLatLng([lat, lon]);
      if (hasCoords) {
        miniRadarMap.setView([lat, lon], Math.max(5, zoom - 1));
      }
    }

    if (miniRadarMap) {
      if (miniRadarTileLayer) {
        try { miniRadarMap.removeLayer(miniRadarTileLayer); } catch (_) {}
        miniRadarTileLayer = null;
      }
      try {
        miniRadarTileLayer = L.tileLayer.wms("https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows", {
          layers: "conus_bref_qcd",
          format: "image/png",
          transparent: true,
          version: "1.3.0",
          tiled: true,
          opacity: radarOpacity,
          attribution: "NOAA MRMS Mosaic",
        }).addTo(miniRadarMap);
      } catch (_) {}
      if (elMiniRadarTimestamp) {
        const dt = new Date();
        const tStr = dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        elMiniRadarTimestamp.textContent = `NOAA MRMS Live (${tStr})`;
      }
    }

    // 2. Full Radar on View 5
    const fullContainer = document.getElementById("full-radar-map");
    if (fullContainer && !fullRadarMap && activeTab === "radar") {
      fullRadarMap = L.map("full-radar-map", {
        center: [lat, lon],
        zoom: zoom,
      });
      createBasemapLayer().addTo(fullRadarMap);
      fullStationMarker = L.marker([lat, lon], { icon: createCrosshairIcon() })
        .addTo(fullRadarMap)
        .bindPopup(`<strong>${escapeHtml(userSettings.station_name)}</strong><br/>${lat.toFixed(4)}, ${lon.toFixed(4)}`);
    } else if (fullRadarMap && fullStationMarker) {
      fullStationMarker.setLatLng([lat, lon]);
      if (hasCoords) {
        fullRadarMap.setView([lat, lon], zoom);
      }
    }

    if (elRadarStationSubtitle) {
      elRadarStationSubtitle.textContent = hasCoords
        ? `Centered on ${userSettings.station_name} (${lat.toFixed(4)}, ${lon.toFixed(4)})`
        : `Configure station Latitude & Longitude in Settings to center radar`;
    }

    setRadarOpacity(radarOpacity);
    updateColorLegend(radarLayerMode);
    buildRadarFramesForActiveMode();
    if (fullRadarMap) {
      currentFrameIdx = radarFrames.length - 1;
      showRadarFrame(currentFrameIdx, true);
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
    const w7 = data.window_7d || data.weekly || {};
    const w30 = data.window_30d || data.monthly || {};
    const rec = data.records_table || data.records || {};
    const learn = data.learning_status || {};

    if (elClimTrajectoryNarrative) {
      elClimTrajectoryNarrative.textContent =
        traj.narrative ||
        data.trajectory_narrative ||
        "Accumulating daily telemetry rollups for seasonal trajectory analysis.";
    }
    if (elClimAnomalyBadge) {
      const anom = traj.anomaly_7d_vs_30d_f ?? traj.anomaly_7d_f ?? data.anomaly_7d_f;
      if (anom !== null && anom !== undefined) {
        const d = Number(anom);
        elClimAnomalyBadge.textContent = `7d vs 30d Anomaly: ${d >= 0 ? "+" : ""}${d.toFixed(1)}°F`;
      } else {
        elClimAnomalyBadge.textContent = "Seasonal Baseline Active";
      }
    }

    // Active Telemetry Learning Banner
    if (elClimLearningBanner) {
      if (elClimLearningStatus && learn.status_label) {
        elClimLearningStatus.textContent = learn.status_label;
      }
      if (elClimLearnPackets && learn.total_packets !== undefined) {
        elClimLearnPackets.textContent = `${Number(learn.total_packets).toLocaleString()} packets analyzed`;
      }
      if (elClimLearnUptime && learn.active_hours !== undefined) {
        const winStr = learn.active_days >= 1 ? `${learn.active_days} days` : `${learn.active_hours} hrs`;
        elClimLearnUptime.textContent = `${winStr} learning window`;
      }
      if (elClimLearnDegreeDays) {
        const hdd = learn.hdd_30d ?? w30.hdd_total ?? 0;
        const cdd = learn.cdd_30d ?? w30.cdd_total ?? 0;
        elClimLearnDegreeDays.textContent = `${Number(hdd).toFixed(0)} HDD / ${Number(cdd).toFixed(0)} CDD`;
      }
      if (elClimLearnSwing) {
        const sw = learn.diurnal_swing_7d ?? w7.avg_diurnal_swing_f ?? w7.diurnal_swing_avg_f;
        elClimLearnSwing.textContent = sw !== null && sw !== undefined ? `7d Swing: ${Number(sw).toFixed(1)}°F` : `7d Swing: --`;
      }
      if (elClimLearnVolatility) {
        const vol = learn.baro_volatility ?? w30.baro_volatility_inhg ?? w7.baro_volatility_inhg;
        elClimLearnVolatility.textContent = vol !== null && vol !== undefined ? `Baro Volatility: ±${Number(vol).toFixed(2)} inHg` : `Baro Volatility: --`;
      }
    }

    if (elClim7dMean) {
      elClim7dMean.innerHTML =
        w7.mean_temp_f !== null && w7.mean_temp_f !== undefined
          ? `${Number(w7.mean_temp_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elClim7dSwing) {
      const sw = w7.avg_diurnal_swing_f ?? w7.diurnal_swing_avg_f;
      elClim7dSwing.innerHTML =
        sw !== null && sw !== undefined
          ? `Avg Diurnal Swing: ${Number(sw).toFixed(1)}&deg;F`
          : `Avg Diurnal Swing: --`;
    }
    if (elClim30dMean) {
      elClim30dMean.innerHTML =
        w30.mean_temp_f !== null && w30.mean_temp_f !== undefined
          ? `${Number(w30.mean_temp_f).toFixed(1)}&deg;F`
          : `--.-&deg;F`;
    }
    if (elClimBaroVol) {
      const bvol = w30.baro_volatility_inhg ?? w7.baro_volatility_inhg;
      elClimBaroVol.textContent =
        bvol !== null && bvol !== undefined
          ? `Baro Volatility: ±${Number(bvol).toFixed(2)} inHg`
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

    // Populate Weather-Display Style 6 Metric Rows across 5 Periods
    if (elClimRecordsTbody) {
      const periods = ["today", "week", "month", "year", "all_time"];
      const getPeriod = (k) => rec[k] || {};

      const fTempRange = (r) =>
        r.temp_high_f !== null && r.temp_high_f !== undefined && r.temp_low_f !== null && r.temp_low_f !== undefined
          ? `${Number(r.temp_high_f).toFixed(1)}&deg; / ${Number(r.temp_low_f).toFixed(1)}&deg;F`
          : "--";
      const fAvgTemp = (r) =>
        r.temp_avg_f !== null && r.temp_avg_f !== undefined
          ? `${Number(r.temp_avg_f).toFixed(1)}&deg;F`
          : (r.temp_high_f !== null && r.temp_high_f !== undefined && r.temp_low_f !== null && r.temp_low_f !== undefined
              ? `${((Number(r.temp_high_f) + Number(r.temp_low_f)) / 2).toFixed(1)}&deg;F`
              : "--");
      const fHumRange = (r) =>
        r.humidity_low !== null && r.humidity_low !== undefined && r.humidity_high !== null && r.humidity_high !== undefined
          ? `${Math.round(r.humidity_low)}% - ${Math.round(r.humidity_high)}%`
          : "--";
      const fWind = (r) => {
        const w = r.wind_gust_max_mph ?? r.wind_peak_mph;
        return w !== null && w !== undefined ? `${Number(w).toFixed(1)} mph` : "--";
      };
      const fRain = (r) => {
        const rn = r.rain_total_in ?? r.rain_in;
        return rn !== null && rn !== undefined ? `${Number(rn).toFixed(2)} in` : "--";
      };
      const fBaro = (r) =>
        r.pressure_low_inhg !== null && r.pressure_low_inhg !== undefined && r.pressure_high_inhg !== null && r.pressure_high_inhg !== undefined
          ? `${Number(r.pressure_low_inhg).toFixed(2)} - ${Number(r.pressure_high_inhg).toFixed(2)} inHg`
          : "--";

      const metricRows = [
        { label: "Temp High / Low", fn: fTempRange },
        { label: "Avg Temperature", fn: fAvgTemp },
        { label: "Humidity Range", fn: fHumRange },
        { label: "Peak Wind Gust", fn: fWind },
        { label: "Total Precipitation", fn: fRain },
        { label: "Barometric Range", fn: fBaro },
      ];

      elClimRecordsTbody.innerHTML = metricRows
        .map(
          (m) => `
          <tr>
            <td><strong>${escapeHtml(m.label)}</strong></td>
            ${periods.map((p) => `<td class="mono">${m.fn(getPeriod(p))}</td>`).join("")}
          </tr>
        `
        )
        .join("");
    }
  }

  // ==========================================================================
  // LIGHTNING RANGE-RING RADAR, FORECAST LEARNING & SYSTEM CONSOLE HELPERS
  // ==========================================================================
  function appendSystemConsoleLog(line, statusText) {
    if (elSystemConsoleStatus && statusText) {
      elSystemConsoleStatus.textContent = statusText;
    }
    if (!elSystemConsoleLog) return;
    const ts = new Date().toTimeString().split(" ")[0];
    const current = elSystemConsoleLog.textContent || "";
    const prefix = current.startsWith("Ready.") ? "" : current + "\n";
    elSystemConsoleLog.textContent = `${prefix}[${ts}] ${line}`;
    elSystemConsoleLog.scrollTop = elSystemConsoleLog.scrollHeight;
  }

  function renderLightningCard(lt) {
    const data = lt || {};
    const paired = Boolean(data.sensor_paired ?? data.sensor_online ?? data.sensor_key);
    const activeStorm = Boolean(data.active_storm_mode);
    const rfi = Boolean(data.rfi_detected ?? data.rfi_warning);
    const strikes1h = Number(data.strikes_1h || 0);
    const strikes24h = Number(data.strikes_24h || 0);
    const closestVal = data.closest_strike_mi ?? data.closest_dist_24h_mi;
    const furthestVal = data.furthest_strike_mi ?? data.furthest_dist_24h_mi;
    const closestMi =
      closestVal !== null && closestVal !== undefined ? Number(closestVal) : null;
    const furthestMi =
      furthestVal !== null && furthestVal !== undefined ? Number(furthestVal) : null;
    const recentStrikes = Array.isArray(data.recent_strikes)
      ? data.recent_strikes
      : Array.isArray(data.recent_rings)
      ? data.recent_rings
      : [];
    const lastTs = data.last_strike_ts || data.last_strike_time;

    if (elOvLightningRfiBadge) {
      elOvLightningRfiBadge.classList.toggle("hidden", !rfi);
    }
    if (elOvLightningStatusBadge) {
      if (activeStorm) {
        elOvLightningStatusBadge.textContent = "⚡ Storm Active (8s updates)";
        elOvLightningStatusBadge.className = "badge badge-batt-low";
      } else if (paired) {
        elOvLightningStatusBadge.textContent = "Normal (24s updates)";
        elOvLightningStatusBadge.className = "badge badge-outdoor";
      } else {
        elOvLightningStatusBadge.textContent = "Standby / Waiting for 06045M";
        elOvLightningStatusBadge.className = "badge";
      }
    }

    if (elOvLtStrikesCount) {
      elOvLtStrikesCount.textContent = `${strikes1h} / ${strikes24h}`;
    }
    if (elOvLtCadenceSub) {
      elOvLtCadenceSub.textContent = activeStorm
        ? "8s active storm cadence"
        : paired
        ? "24s normal cadence"
        : "1–25 mi detection ready";
    }
    if (elOvLtClosest) {
      elOvLtClosest.textContent = closestMi !== null ? `${closestMi.toFixed(0)} mi` : `-- mi`;
    }
    if (elOvLtFurthest) {
      elOvLtFurthest.textContent = furthestMi !== null ? `${furthestMi.toFixed(0)} mi` : `-- mi`;
    }
    if (elOvLtLastTime) {
      elOvLtLastTime.textContent = lastTs
        ? formatRelativeSeconds(lastTs)
        : "None";
    }
    if (elOvLtLastDist) {
      elOvLtLastDist.textContent =
        data.last_strike_dist_mi !== null && data.last_strike_dist_mi !== undefined
          ? `Distance: ${Number(data.last_strike_dist_mi).toFixed(0)} mi`
          : `Range: 1–25 mi`;
    }

    // Render Concentric Strike Rings on 200x200 SVG (center=100,100; 25 mi = r=88 => 3.52 px/mi)
    if (elLightningDynamicRings) {
      const ringsHtml = [];
      const distToRadius = (mi) => Math.max(4, Math.min(88, Number(mi) * 3.52));

      // 1. Furthest Strike Outer Ring (dotted violet)
      if (furthestMi !== null && furthestMi > 0) {
        const rFur = distToRadius(furthestMi);
        ringsHtml.push(
          `<circle cx="100" cy="100" r="${rFur.toFixed(1)}" fill="none" stroke="#a855f7" stroke-width="1.8" stroke-dasharray="2 3" opacity="0.85" />`
        );
      }

      // 2. Fading recent strike rings (amber/red based on proximity & age)
      recentStrikes.slice(0, 8).forEach((st, idx) => {
        const dMi = st.distance_mi ?? st.storm_dist_mi;
        if (dMi === null || dMi === undefined) return;
        const rPx = distToRadius(dMi);
        const ageMin = Number(st.age_minutes ?? idx * 4);
        const opacity = Math.max(0.22, Math.min(0.95, 1.0 - ageMin / 90));
        const color = Number(dMi) <= 8 ? "#ef4444" : Number(dMi) <= 16 ? "#f59e0b" : "#fbbf24";
        const animClass = idx === 0 ? "strike-ring-animated" : "";
        ringsHtml.push(
          `<circle class="${animClass}" cx="100" cy="100" r="${rPx.toFixed(1)}" fill="none" stroke="${color}" stroke-width="2.2" opacity="${opacity.toFixed(2)}" />`
        );
      });

      // 3. Closest Strike Inner Ring (crisp dashed cyan/red ring)
      if (closestMi !== null && closestMi > 0) {
        const rClose = distToRadius(closestMi);
        const closeColor = closestMi <= 6 ? "#ef4444" : "#38bdf8";
        ringsHtml.push(
          `<circle cx="100" cy="100" r="${rClose.toFixed(1)}" fill="none" stroke="${closeColor}" stroke-width="2.2" stroke-dasharray="5 3" />`
        );
      }

      elLightningDynamicRings.innerHTML = ringsHtml.join("");
    }
  }

  function triggerSimulatedLightningStrike() {
    const nowIso = new Date().toISOString();
    simulatedLightningState = {
      sensor_paired: true,
      active_storm_mode: true,
      rfi_detected: false,
      strikes_1h: 7,
      strikes_24h: 19,
      closest_strike_mi: 6,
      furthest_strike_mi: 21,
      last_strike_ts: nowIso,
      last_strike_dist_mi: 6,
      recent_strikes: [
        { distance_mi: 6, age_minutes: 0.2, delta_strikes: 2 },
        { distance_mi: 11, age_minutes: 6.5, delta_strikes: 3 },
        { distance_mi: 16, age_minutes: 18.0, delta_strikes: 2 },
        { distance_mi: 21, age_minutes: 42.0, delta_strikes: 1 },
      ],
    };
    renderLightningCard(simulatedLightningState);
    appendSystemConsoleLog(
      "Simulated AcuRite 06045M Strike Burst: Closest=6 mi, Active=11 mi & 16 mi, Furthest=21 mi (8s storm cadence preview).",
      "SIMULATED"
    );
    showToast("Simulated AcuRite 06045M Lightning Rings (Closest: 6 mi, Furthest: 21 mi).");
  }

  async function submitWeatherFeedback(ratingOrCondition, isDirectCondition) {
    try {
      const payload = isDirectCondition
        ? { user_condition: ratingOrCondition, rating: "spot_on", notes: "Settings Check-In" }
        : {
            rating: ratingOrCondition,
            user_condition: elOvConditionLabel ? elOvConditionLabel.textContent : "Observed",
          };
      const res = await fetch("/api/forecast/feedback", {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);

      if (elOvCheckinStatus && data.summary) {
        const cnt = data.summary.total_feedback ?? data.summary.total_count ?? 1;
        elOvCheckinStatus.textContent = `Logged! (${cnt} confirmations, ${Math.round(data.summary.accuracy_pct ?? 100)}% accuracy)`;
      }
      appendSystemConsoleLog(
        `Ground-truth check-in recorded (${isDirectCondition ? ratingOrCondition : ratingOrCondition}). Model confidence updated.`,
        "UPDATED"
      );
      showToast("Ground-truth weather check-in recorded! Forecast confidence model updated.");
      await fetchOverview();
    } catch (err) {
      showToast(err.message || "Failed to record weather check-in.");
    }
  }

  async function fetchSystemTimeStatus() {
    try {
      const res = await fetch("/api/system/time", { headers: getAuthHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      const locIso = data.local_iso || data.server_local_iso;
      const utcIso = data.utc_iso || data.server_utc_iso;
      const tzName = data.timezone || data.timezone_name;
      const driftVal = data.offset_ms ?? data.ntp_offset_ms;
      const autoSyncVal = data.ntp_auto_sync ?? data.auto_sync_enabled;

      if (elSetSysLocalTime && locIso) {
        elSetSysLocalTime.textContent = String(locIso).replace("T", " ").slice(0, 19);
      }
      if (elSetSysUtcTime && utcIso) {
        elSetSysUtcTime.textContent = `UTC: ${String(utcIso).replace("T", " ").slice(0, 19)}Z`;
      }
      if (elSetSysTz && tzName) {
        elSetSysTz.textContent = tzName;
      }
      if (elSetSysDrift) {
        const drift = driftVal !== null && driftVal !== undefined ? Number(driftVal) : 0;
        elSetSysDrift.textContent = `NTP Offset: ${drift >= 0 ? "+" : ""}${drift.toFixed(1)} ms`;
      }
      if (elSetAutoSyncTime && autoSyncVal !== undefined) {
        elSetAutoSyncTime.checked = Boolean(autoSyncVal);
      }
    } catch (_) {}
  }

  async function handleGeocodeLookupClick() {
    const query = elSetCityStateQuery ? elSetCityStateQuery.value.trim() : "";
    if (!query) {
      showToast("Please enter a City, State or Postal Code first.");
      return;
    }
    requireUnlockedOrPrompt(async () => {
      if (elBtnGeocodeLookup) elBtnGeocodeLookup.disabled = true;
      if (elSetGeocodeStatus) elSetGeocodeStatus.textContent = `Resolving "${query}"...`;
      try {
        const res = await authorizedWriteFetch("/api/settings/geocode", {
          method: "POST",
          body: JSON.stringify({ query, apply: true, auto_save: true }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);

        const src = data.geocoded || data.resolved || data;
        const lat = parseFloat(data.latitude ?? data.lat ?? src.latitude ?? src.lat);
        const lon = parseFloat(data.longitude ?? data.lon ?? src.longitude ?? src.lon);
        const elevRaw = parseFloat(data.elevation_m ?? data.elevation ?? src.elevation_m ?? src.elevation ?? 0);
        const elev = Number.isFinite(elevRaw) ? elevRaw : 0.0;
        const label = String(
          data.display_name || data.name || data.label || src.display_name || src.name || src.label || query
        ).trim();

        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
          const errMsg = `Could not resolve finite coordinates for "${query}". Try "City, State" or "City, Country".`;
          if (elSetGeocodeStatus) elSetGeocodeStatus.textContent = errMsg;
          showToast(errMsg);
          return;
        }

        if (elSetLatitude) elSetLatitude.value = lat.toFixed(4);
        if (elSetLongitude) elSetLongitude.value = lon.toFixed(4);
        if (elSetElevation) elSetElevation.value = elev.toFixed(1);
        if (elSetCityStateQuery) elSetCityStateQuery.value = label;

        userSettings.latitude = lat;
        userSettings.longitude = lon;
        userSettings.elevation_m = elev;
        userSettings.city_state_label = label;
        if (data.settings && typeof data.settings === "object") {
          userSettings = Object.assign(userSettings, data.settings);
        }

        if (elSetGeocodeStatus) {
          elSetGeocodeStatus.textContent = `Resolved: ${label} (${lat.toFixed(4)}, ${lon.toFixed(4)})`;
        }
        appendSystemConsoleLog(
          `Geocoded "${query}" -> ${label} (${lat.toFixed(4)}, ${lon.toFixed(4)}, elev ${elev.toFixed(1)}m)`,
          "GEOCODED"
        );
        showToast(`Resolved & saved coordinates for ${label}!`);
        await fetchNwsSummary(true);
        await initAndRefreshRadarMaps();
        await refreshAll(true);
      } catch (err) {
        if (elSetGeocodeStatus) elSetGeocodeStatus.textContent = err.message || "Geocode lookup failed.";
        showToast(err.message || "Failed to lookup city/state coordinates.");
      } finally {
        if (elBtnGeocodeLookup) elBtnGeocodeLookup.disabled = false;
      }
    });
  }

  async function handleSyncLearnHistoryClick() {
    requireUnlockedOrPrompt(async () => {
      if (elBtnSyncLearnHistory) elBtnSyncLearnHistory.disabled = true;
      appendSystemConsoleLog("Fetching 365-day historical regional weather archive from Open-Meteo...", "TRAINING");
      try {
        const res = await authorizedWriteFetch("/api/forecast/learn-history", {
          method: "POST",
          body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        const days = data.days_analyzed || 365;
        const months = data.months_trained || 12;
        if (elSetLearnArchiveStatus) {
          elSetLearnArchiveStatus.textContent = `${days}-Day Baseline Active`;
        }
        if (elSetLearnArchiveSub) {
          elSetLearnArchiveSub.textContent = `${months} monthly climatology profiles trained`;
        }
        appendSystemConsoleLog(
          `Trained 1-Year Weather Model: ${days} daily records & ${data.hourly_points_analyzed || 2160} hourly pressure/dew-spread points across ${months} months.`,
          "READY"
        );
        showToast(`Synced & trained 365-day regional weather baseline (${months} monthly profiles)!`);
        await refreshAll(true);
      } catch (err) {
        appendSystemConsoleLog(`Model training error: ${err.message}`, "ERROR");
        showToast(err.message || "Failed to train 1-year weather model.");
      } finally {
        if (elBtnSyncLearnHistory) elBtnSyncLearnHistory.disabled = false;
      }
    });
  }

  async function handleSyncClockNowClick() {
    requireUnlockedOrPrompt(async () => {
      if (elBtnSyncClockNow) elBtnSyncClockNow.disabled = true;
      appendSystemConsoleLog("Querying NTP pool time servers (pool.ntp.org / time.google.com)...", "SYNCING");
      try {
        const autoSync = elSetAutoSyncTime ? elSetAutoSyncTime.checked : true;
        const res = await authorizedWriteFetch("/api/system/time-sync", {
          method: "POST",
          body: JSON.stringify({ auto_sync_enabled: autoSync }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        await fetchSystemTimeStatus();
        const rawOff = data.offset_ms ?? data.ntp_offset_ms;
        const offsetMs = rawOff !== undefined && rawOff !== null ? Number(rawOff) : 0;
        const srcHost = data.source || data.ntp_source || "pool.ntp.org";
        const locTime = data.local_iso || data.server_local_iso || "";
        appendSystemConsoleLog(
          `NTP Clock Sync Verified (${srcHost}): Server ${locTime} | Offset ${offsetMs >= 0 ? "+" : ""}${offsetMs.toFixed(1)} ms`,
          "SYNCED"
        );
        showToast(`System Clock Verified (Offset: ${offsetMs.toFixed(1)} ms).`);
      } catch (err) {
        appendSystemConsoleLog(`Clock sync failed: ${err.message}`, "ERROR");
        showToast(err.message || "Clock sync failed.");
      } finally {
        if (elBtnSyncClockNow) elBtnSyncClockNow.disabled = false;
      }
    });
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
    if (elSetCityStateQuery && s.city_state_label) {
      elSetCityStateQuery.value = s.city_state_label;
    }
    if (elSetStationName) elSetStationName.value = s.station_name || "";
    if (elHeaderStationTitle) elHeaderStationTitle.textContent = s.station_name || "SkyScry";
    if (elTopBarStationTitle) elTopBarStationTitle.textContent = s.station_name || "SkyScry";
    if (elOvStationEyebrow) elOvStationEyebrow.textContent = `${s.station_name || "Local Station"} • Outdoor Telemetry`;
    if (elThCorrobLocalName) elThCorrobLocalName.textContent = s.station_name || "Local Sensors";
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

    if (elSetBaroWeight && s.forecast_baro_weight !== undefined) {
      elSetBaroWeight.value = String(s.forecast_baro_weight);
      if (elSetBaroWeightVal) elSetBaroWeightVal.textContent = `${Number(s.forecast_baro_weight).toFixed(1)}x`;
    }
    if (elSetDewWeight && s.forecast_dew_weight !== undefined) {
      elSetDewWeight.value = String(s.forecast_dew_weight);
      if (elSetDewWeightVal) elSetDewWeightVal.textContent = `${Number(s.forecast_dew_weight).toFixed(1)}x`;
    }
    if (elSetVacuumSchedule && s.auto_vacuum_schedule) {
      elSetVacuumSchedule.value = String(s.auto_vacuum_schedule);
    }

    // Populate Forecast Feedback & System Clock status
    fetch("/api/forecast/feedback", { headers: getAuthHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((fbData) => {
        if (!fbData) return;
        const sum = fbData.summary || {};
        const lm = fbData.learned_model || {};
        const monthCount = Object.keys(lm).length;
        if (elSetLearnArchiveStatus) {
          elSetLearnArchiveStatus.textContent =
            monthCount > 0 ? "365-Day Regional Baseline Loaded" : "Baseline Ready to Sync";
        }
        if (elSetLearnArchiveSub) {
          elSetLearnArchiveSub.textContent =
            monthCount > 0 ? `${monthCount} monthly profiles active` : "Click Sync below to train";
        }
        if (elSetLearnFeedbackStats) {
          const totalFb = sum.total_feedback ?? sum.total_count ?? 0;
          elSetLearnFeedbackStats.textContent = `${totalFb} confirmations logged`;
        }
        if (elSetLearnFeedbackAcc) {
          elSetLearnFeedbackAcc.textContent = `Accuracy: ${Math.round(sum.accuracy_pct ?? 100)}%`;
        }
        if (elSetModelConfidenceBadge) {
          elSetModelConfidenceBadge.textContent = `Confidence: ${Math.round(sum.accuracy_pct ?? 88)}%`;
        }
      })
      .catch(() => {});

    fetchSystemTimeStatus();
    fetchAndRenderForecastModels().catch((err) => console.error("Forecast models load error:", err));
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
      appendSystemConsoleLog(
        "Starting SQLite WAL Checkpoint (TRUNCATE) -> VACUUM -> ANALYZE -> PRAGMA optimize...",
        "RUNNING"
      );
      try {
        const sched = elSetVacuumSchedule ? elSetVacuumSchedule.value : "daily_0300";
        const res = await authorizedWriteFetch("/api/maintenance/vacuum", {
          method: "POST",
          body: JSON.stringify({ auto_vacuum_schedule: sched }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        const stepsStr = Array.isArray(data.steps)
          ? data.steps.map((s) => (typeof s === "string" ? s : (s.name || s.step || JSON.stringify(s)))).join(" -> ")
          : "Checkpoint WAL -> Vacuum Pages -> Truncate WAL";
        const beforeMb = data.before_mb ?? data.size_before_mb ?? 0;
        const afterMb = data.after_mb ?? data.size_after_mb ?? 0;
        const savedMb = data.saved_mb ?? data.reclaimed_mb ?? 0;
        const durMs = data.duration_ms ?? data.elapsed_ms ?? 45;
        appendSystemConsoleLog(
          `DB Size Before: ${beforeMb} MB (Main: ${data.main_before_mb ?? beforeMb} MB, WAL: ${data.wal_before_mb ?? 0} MB) -> ${stepsStr} -> DB Size After: ${afterMb} MB (Saved ${savedMb} MB in ${durMs}ms)`,
          "OPTIMIZED"
        );
        showToast(
          `SQLite VACUUM & Optimize Complete: ${beforeMb} MB -> ${afterMb} MB (Saved ${savedMb} MB)`
        );
        await fetchStatus();
      } catch (err) {
        appendSystemConsoleLog(`Vacuum error: ${err.message}`, "ERROR");
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

    const isMobile = window.innerWidth < 640;

    const yScales = {
      title: {
        display: true,
        text: yUnitLabel,
        color: "#94a3b8",
        font: { size: 11 },
      },
      ticks: { color: "#94a3b8" },
      grid: { color: "rgba(36, 50, 75, 0.45)" },
    };

    if (key === "rain") {
      yScales.min = 0;
      yScales.suggestedMax = 0.25;
      yScales.beginAtZero = true;
    }

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
            labels: {
              color: "#cbd5e1",
              font: { size: 11 },
              boxWidth: 10,
              boxHeight: 10,
              padding: 8,
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: "#64748b",
              maxTicksLimit: isMobile ? 6 : 12,
              maxRotation: 0,
            },
            grid: { color: "rgba(36, 50, 75, 0.45)" },
          },
          y: yScales,
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

    const isMobileScreen = window.innerWidth < 768;
    const defaultPointRadius = isMobileScreen ? 0 : 1;
    const defaultPointHoverRadius = 5;
    const defaultPointHitRadius = 16;

    const tempDatasets = [];
    if (showEnsemble && Array.isArray(ensemble.temp_f) && ensemble.temp_f.some((v) => v !== null)) {
      tempDatasets.push({
        label: "Outdoor Average (Ensemble Mean)",
        data: ensemble.temp_f,
        borderColor: "#ffffff",
        backgroundColor: "rgba(255, 255, 255, 0.1)",
        borderWidth: 2.2,
        borderDash: [6, 4],
        pointRadius: 0,
        pointHoverRadius: defaultPointHoverRadius,
        pointHitRadius: defaultPointHitRadius,
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
        pointHoverRadius: defaultPointHoverRadius,
        pointHitRadius: defaultPointHitRadius,
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
        borderWidth: 2.2,
        borderDash: [6, 4],
        pointRadius: 0,
        pointHoverRadius: defaultPointHoverRadius,
        pointHitRadius: defaultPointHitRadius,
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
        pointHoverRadius: defaultPointHoverRadius,
        pointHitRadius: defaultPointHitRadius,
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
        pointHoverRadius: defaultPointHoverRadius,
        pointHitRadius: defaultPointHitRadius,
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
          borderWidth: 2.2,
          pointRadius: defaultPointRadius,
          pointHoverRadius: defaultPointHoverRadius,
          pointHitRadius: defaultPointHitRadius,
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
          borderWidth: 2.2,
          pointRadius: defaultPointRadius,
          pointHoverRadius: defaultPointHoverRadius,
          pointHitRadius: defaultPointHitRadius,
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
          borderWidth: 2.2,
          pointRadius: defaultPointRadius,
          pointHoverRadius: defaultPointHoverRadius,
          pointHitRadius: defaultPointHitRadius,
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
            pointRadius: defaultPointRadius,
            pointHoverRadius: defaultPointHoverRadius,
            pointHitRadius: defaultPointHitRadius,
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
          pointRadius: defaultPointRadius,
          pointHoverRadius: defaultPointHoverRadius,
          pointHitRadius: defaultPointHitRadius,
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
      if (cRain.options && cRain.options.scales && cRain.options.scales.y) {
        cRain.options.scales.y.min = 0;
        cRain.options.scales.y.suggestedMax = 0.25;
        cRain.options.scales.y.beginAtZero = true;
      }
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
          const newVer = data.new_version || (data.current && data.current.version) || "0.0.15";
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
  // VIEW 6: INTERACTIVE WEATHER CALENDAR & MINI-DASHBOARD DAY MODAL
  // ==========================================================================
  const CARDINAL_TO_DEG = {
    N: 0, NNE: 22.5, NE: 45, ENE: 67.5,
    E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
    S: 180, SSW: 202.5, SW: 225, WSW: 247.5,
    W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
    CALM: 0, VAR: 0,
  };

  function getMiniCompassSvg(dirStr) {
    const dirUpper = String(dirStr || "").trim().toUpperCase();
    const deg = CARDINAL_TO_DEG[dirUpper] !== undefined ? CARDINAL_TO_DEG[dirUpper] : 0;
    return `
      <svg viewBox="0 0 60 60" width="52" height="52" fill="none" class="cal-mini-compass-svg" aria-label="Wind bearing: ${escapeHtml(dirUpper || "Calm")}">
        <circle cx="30" cy="30" r="26" stroke="#334155" stroke-width="2" fill="#0f172a" />
        <circle cx="30" cy="30" r="20" stroke="#1e293b" stroke-width="1" stroke-dasharray="2 3" />
        <text x="30" y="11.5" text-anchor="middle" font-size="7.5" font-weight="800" fill="#ef4444">N</text>
        <text x="50.5" y="32.5" text-anchor="middle" font-size="7" font-weight="700" fill="#94a3b8">E</text>
        <text x="30" y="52" text-anchor="middle" font-size="7" font-weight="700" fill="#94a3b8">S</text>
        <text x="9.5" y="32.5" text-anchor="middle" font-size="7" font-weight="700" fill="#94a3b8">W</text>
        <g style="transform: rotate(${deg}deg); transform-origin: 30px 30px;">
          <polygon points="30,11 34,30 30,26" fill="#38bdf8" />
          <polygon points="30,11 26,30 30,26" fill="#7dd3fc" />
          <polygon points="30,49 34,30 30,34" fill="#64748b" />
          <polygon points="30,49 26,30 30,34" fill="#475569" />
          <circle cx="30" cy="30" r="3.5" fill="#f8fafc" stroke="#0f172a" stroke-width="1.5" />
        </g>
      </svg>`;
  }

  function getMiniHumidityArcSvg(rhVal) {
    const rh = Math.max(0, Math.min(100, Number(rhVal || 0)));
    const totalCirc = Math.PI * 32; // ~100.53
    const dashOffset = totalCirc * (1 - rh / 100);
    return `
      <svg viewBox="0 0 90 52" width="76" height="44" class="cal-mini-rh-svg" aria-label="Humidity: ${Math.round(rh)}%">
        <defs>
          <linearGradient id="calRhGrad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stop-color="#38bdf8" />
            <stop offset="100%" stop-color="#06b6d4" />
          </linearGradient>
        </defs>
        <path d="M 13,44 A 32,32 0 0,1 77,44" fill="none" stroke="#1e293b" stroke-width="7" stroke-linecap="round" />
        <path d="M 13,44 A 32,32 0 0,1 77,44" fill="none" stroke="url(#calRhGrad)" stroke-width="7" stroke-linecap="round"
              stroke-dasharray="${totalCirc.toFixed(1)}" stroke-dashoffset="${dashOffset.toFixed(1)}" />
        <text x="45" y="42" text-anchor="middle" font-size="14" font-weight="800" fill="#f8fafc" font-family="monospace">${Math.round(rh)}%</text>
      </svg>`;
  }

  function getMiniThermalBarHtml(lowF, highF, avgF) {
    const lo = Number.isFinite(Number(lowF)) ? Number(lowF) : 40;
    const hi = Number.isFinite(Number(highF)) ? Number(highF) : 60;
    const avg = Number.isFinite(Number(avgF)) ? Number(avgF) : (lo + hi) / 2;

    const minScale = Math.min(10, Math.floor(lo / 10) * 10);
    const maxScale = Math.max(100, Math.ceil(hi / 10) * 10);
    const range = Math.max(1, maxScale - minScale);

    const leftPct = Math.max(0, Math.min(95, ((lo - minScale) / range) * 100));
    const spanPct = Math.max(5, Math.min(100 - leftPct, ((hi - lo) / range) * 100));
    const avgPct = Math.max(0, Math.min(100, ((avg - minScale) / range) * 100));

    return `
      <div class="cal-mini-thermal-scale">
        <div class="cal-thermal-track">
          <div class="cal-thermal-span" style="left: ${leftPct.toFixed(1)}%; width: ${spanPct.toFixed(1)}%;"></div>
          <div class="cal-thermal-pin" style="left: ${avgPct.toFixed(1)}%;" title="Mean: ${avg.toFixed(1)}°F"></div>
        </div>
        <div class="cal-thermal-ticks">
          <span>${minScale}°</span>
          <span class="cal-thermal-freeze">32°</span>
          <span class="cal-thermal-comfort">65°</span>
          <span>${maxScale}°</span>
        </div>
      </div>`;
  }

  function getMiniPressureScaleHtml(pMin, pMax, pAvg) {
    const minVal = Number.isFinite(pMin) ? pMin : 29.80;
    const maxVal = Number.isFinite(pMax) ? pMax : 30.10;
    const avgVal = Number.isFinite(pAvg) ? pAvg : (minVal + maxVal) / 2;

    const minPct = Math.max(0, Math.min(100, ((minVal - 29.20) / (30.50 - 29.20)) * 100));
    const maxPct = Math.max(0, Math.min(100, ((maxVal - 29.20) / (30.50 - 29.20)) * 100));
    const avgPct = Math.max(0, Math.min(100, ((avgVal - 29.20) / (30.50 - 29.20)) * 100));
    const spanPct = Math.max(3, maxPct - minPct);

    return `
      <div class="cal-mini-baro-scale">
        <div class="cal-baro-zones">
          <span class="cal-baro-zone zone-low" title="Storm / Low Pressure (&lt; 29.80 inHg)"></span>
          <span class="cal-baro-zone zone-norm" title="Normal / Standard (29.80 - 30.20 inHg)"></span>
          <span class="cal-baro-zone zone-high" title="High Pressure (&gt; 30.20 inHg)"></span>
        </div>
        <div class="cal-baro-track">
          <div class="cal-baro-span" style="left: ${minPct.toFixed(1)}%; width: ${spanPct.toFixed(1)}%;"></div>
          <div class="cal-baro-pin" style="left: ${avgPct.toFixed(1)}%;" title="Barometer: ${avgVal.toFixed(2)} inHg"></div>
        </div>
        <div class="cal-baro-ticks">
          <span>29.20 (Low)</span>
          <span class="cal-baro-std">29.92 (Std)</span>
          <span>30.50 (High)</span>
        </div>
      </div>`;
  }

  function getMiniRainGaugeHtml(rainIn, popPct, strikes) {
    const rIn = Number(rainIn || 0);
    const strikesCount = Number(strikes || 0);
    const fillPct = popPct !== null && popPct !== undefined
      ? Math.min(100, Math.max(0, Number(popPct)))
      : Math.min(100, Math.max(rIn > 0 ? 8 : 0, (rIn / 1.5) * 100));

    return `
      <div class="cal-mini-rain-wrap">
        <div class="cal-mini-rain-bar-row">
          <span class="cal-rain-icon">💧</span>
          <div class="cal-rain-track">
            <div class="cal-rain-fill" style="width: ${fillPct.toFixed(1)}%;"></div>
          </div>
          <span class="cal-rain-readout mono">${popPct !== null && popPct !== undefined ? `${Math.round(popPct)}%` : `${rIn.toFixed(2)}"`}</span>
        </div>
        <div class="cal-lightning-badge-row">
          <span class="cal-lightning-pill ${strikesCount > 0 ? 'active' : ''}">
            <span class="cal-lt-symbol">⚡</span>
            <span>${strikesCount > 0 ? `${strikesCount.toLocaleString()} Strikes Detected` : '0 Lightning Strikes (Calm)'}</span>
          </span>
        </div>
      </div>`;
  }

  function getMiniUvBarHtml(uvMax, luxPeak, hdd, cdd) {
    const uv = Number(uvMax || 0);
    const uvPct = Math.min(100, Math.max(0, (uv / 11) * 100));
    const lux = Number(luxPeak || 0);
    const h = Number(hdd || 0);
    const c = Number(cdd || 0);

    return `
      <div class="cal-mini-uv-wrap">
        <div class="cal-uv-track-row">
          <span class="cal-uv-label">UV ${uv.toFixed(1)}</span>
          <div class="cal-uv-track">
            <div class="cal-uv-gradient"></div>
            <div class="cal-uv-pin" style="left: ${uvPct.toFixed(1)}%;"></div>
          </div>
        </div>
        <div class="cal-uv-details-row">
          <span class="cal-lux-val mono">☀ ${Math.round(lux).toLocaleString()} lx</span>
          <span class="cal-dd-val mono">HDD: ${h.toFixed(1)} • CDD: ${c.toFixed(1)}</span>
        </div>
      </div>`;
  }

  function getMiniForecastSummaryHtml(daySummary, nightSummary) {
    return `
      <div class="cal-mini-forecast-summary">
        <div class="cal-fc-period">
          <span class="cal-fc-title">☀ Daytime:</span>
          <span class="cal-fc-text">${escapeHtml(daySummary || "Mostly sunny and seasonable.")}</span>
        </div>
        <div class="cal-fc-period">
          <span class="cal-fc-title">🌙 Nighttime:</span>
          <span class="cal-fc-text">${escapeHtml(nightSummary || "Overnight cooling toward morning low.")}</span>
        </div>
      </div>`;
  }

  async function fetchAndRenderCalendar(year, month) {
    const y = Number.isFinite(Number(year)) ? Number(year) : calendarYear;
    const m = Number.isFinite(Number(month)) ? Number(month) : calendarMonth;
    calendarYear = y;
    calendarMonth = m;

    const res = await fetch(`/api/calendar?year=${y}&month=${m}`, {
      headers: getAuthHeaders(),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    calendarLoadedOnce = true;

    if (elCalMonthTitle) {
      elCalMonthTitle.textContent = data.month_label || `${y}-${String(m).padStart(2, "0")}`;
    }

    const summary = data.summary || {};
    if (elCalStatAvgHl) {
      const avgH = summary.avg_high_f;
      const avgL = summary.avg_low_f;
      if (avgH !== null && avgH !== undefined && avgL !== null && avgL !== undefined) {
        elCalStatAvgHl.textContent = `${Number(avgH).toFixed(1)}°F / ${Number(avgL).toFixed(1)}°F`;
      } else {
        elCalStatAvgHl.textContent = "--°F / --°F";
      }
    }
    if (elCalStatRain) {
      const totalRain = Number(summary.total_rain_in || 0);
      elCalStatRain.textContent = `${totalRain.toFixed(2)} in`;
    }
    if (elCalStatSunnyRainy) {
      elCalStatSunnyRainy.textContent = `${summary.sunny_days ?? 0} Sunny • ${summary.rainy_days ?? 0} Wet`;
    }

    calendarDaysMap.clear();
    (data.days || []).forEach((d) => {
      if (d && d.date) {
        calendarDaysMap.set(d.date, d);
      }
    });

    if (!elCalendarWeeksContainer) return;

    const weeks = data.weeks || [];
    if (weeks.length === 0) {
      elCalendarWeeksContainer.innerHTML = `<div class="empty-state">No calendar data available for this month.</div>`;
      return;
    }

    const weeksHtml = weeks
      .map((wk) => {
        const combos = wk.combos || [];
        let comboTrackHtml = "";
        if (combos.length > 0) {
          const ribbonsHtml = combos
            .map((c) => {
              const startCol = Math.max(1, Math.min(7, Number(c.start_col || 0) + 1));
              const spanCols = Math.max(1, Math.min(8 - startCol, Number(c.span || 2)));
              const toneClass = `tone-${escapeHtml(c.tone || "blue")}`;
              return `
                <div class="cal-combo-ribbon ${toneClass}"
                     style="grid-column: ${startCol} / span ${spanCols};"
                     title="${escapeHtml(c.label || "")} (${escapeHtml(c.start_date || "")} → ${escapeHtml(c.end_date || "")})">
                  <span class="cal-combo-spark">✦</span>
                  <span class="cal-combo-text">${escapeHtml(c.label || "")}</span>
                </div>`;
            })
            .join("");
          comboTrackHtml = `<div class="calendar-combo-track">${ribbonsHtml}</div>`;
        }

        const emptySlotsCount = Number(wk.leading_empty_slots || 0);
        let emptySlotsHtml = "";
        if (emptySlotsCount > 0) {
          for (let s = 0; s < emptySlotsCount; s++) {
            emptySlotsHtml += `<div class="calendar-empty-slot" aria-hidden="true"></div>`;
          }
        }

        const validDays = (wk.days || []).filter((d) => d && d.in_month);
        if (validDays.length === 0 && emptySlotsCount === 0) return "";

        const cellsHtml = validDays
          .map((d) => {
            const classes = ["calendar-day-cell", "cal-day-cell"];
            if (d.is_today) classes.push("is-today");
            if (d.is_future) classes.push("is-future");
            else classes.push("is-past-or-today");

            // Source Badge with High-Contrast Indicator Dot
            let sourceBadgeHtml = "";
            if (d.source === "local_sdr") {
              sourceBadgeHtml = `
                <span class="cal-day-source-badge source-local" title="Recorded by local sensors">
                  <span class="cal-source-dot"></span>
                  <span class="cal-source-name">Local</span>
                </span>`;
            } else if (d.source === "historical_archive") {
              sourceBadgeHtml = `
                <span class="cal-day-source-badge source-archive" title="Historical regional baseline archive">
                  <span class="cal-source-dot"></span>
                  <span class="cal-source-name">Archive</span>
                </span>`;
            } else {
              const confPct = d.confidence_pct ?? 70;
              sourceBadgeHtml = `
                <span class="cal-day-source-badge source-forecast" title="Ensemble forecast confidence: ${confPct}%">
                  <span class="cal-source-dot"></span>
                  <span class="cal-source-name">Est. ${confPct}%</span>
                </span>`;
            }

            const highStr =
              d.high_f !== null && d.high_f !== undefined
                ? `${Math.round(Number(d.high_f))}°`
                : "--°";
            const lowStr =
              d.low_f !== null && d.low_f !== undefined
                ? `${Math.round(Number(d.low_f))}°`
                : "--°";

            let rainPillHtml = "";
            if (!d.is_future) {
              const rIn = Number(d.rain_in || 0);
              if (rIn > 0) {
                rainPillHtml = `<span class="cal-rain-pill">💧 ${rIn.toFixed(2)}"</span>`;
              }
            } else {
              const pop = Number(d.pop_pct || 0);
              if (pop >= 15) {
                rainPillHtml = `<span class="cal-rain-pill">💧 ${Math.round(pop)}%</span>`;
              }
            }

            const svgHtml = getWeatherIconSvg(d.condition_type, 42);

            return `
              <button type="button"
                      class="${classes.join(" ")}"
                      data-date="${escapeHtml(d.date)}"
                      aria-label="${escapeHtml(d.full_date_label || d.date)}: ${escapeHtml(d.condition_label || "")}, High ${highStr}, Low ${lowStr}">
                <div class="cal-day-top">
                  <div class="cal-day-num-wrap">
                    <span class="cal-day-weekday">${escapeHtml(d.weekday_short || d.weekday || "")}</span>
                    <span class="cal-day-num">${d.day}</span>
                    ${d.is_today ? '<span class="cal-today-pill">TODAY</span>' : ""}
                  </div>
                  ${sourceBadgeHtml}
                </div>
                <div class="cal-day-center">
                  <div class="cal-day-icon-wrap cal-day-icon">${svgHtml}</div>
                  <div class="cal-day-cond-label cal-cond-label">${escapeHtml(d.condition_label || "Fair")}</div>
                </div>
                <div class="cal-day-bottom">
                  <div class="cal-temp-pair">
                    <span class="cal-temp-high">${highStr}</span>
                    <span class="cal-temp-sep">/</span>
                    <span class="cal-temp-low">${lowStr}</span>
                  </div>
                  ${rainPillHtml}
                </div>
              </button>`;
          })
          .join("");

        return `
          <div class="calendar-week-block">
            ${comboTrackHtml}
            <div class="calendar-week-grid">${emptySlotsHtml}${cellsHtml}</div>
          </div>`;
      })
      .join("");

    elCalendarWeeksContainer.innerHTML = weeksHtml;

    elCalendarWeeksContainer.querySelectorAll(".calendar-day-cell, .cal-day-cell").forEach((cell) => {
      cell.addEventListener("click", () => {
        const dt = cell.getAttribute("data-date");
        if (dt) openCalendarDayModal(dt);
      });
    });
  }

  function formatFAndC(tempF) {
    if (tempF === null || tempF === undefined || Number.isNaN(Number(tempF))) {
      return "--°F (--°C)";
    }
    const f = Number(tempF);
    const c = ((f - 32) * 5) / 9;
    return `${f.toFixed(1)}°F (${c.toFixed(1)}°C)`;
  }

  function openCalendarDayModal(dateStr) {
    const d = calendarDaysMap.get(dateStr);
    if (!d || !elCalModalBackdrop) return;

    if (elCalModalIcon) {
      elCalModalIcon.innerHTML = getWeatherIconSvg(d.condition_type, 64);
    }
    if (elCalModalDateTitle) {
      elCalModalDateTitle.textContent = d.full_date_label || d.date;
    }
    if (elCalModalConditionTitle) {
      elCalModalConditionTitle.textContent = d.condition_label || "Weather Summary";
    }

    // Modal Header: Clean Single Badge (No redundant duplications)
    if (elCalModalSourceBadge) {
      if (d.source === "local_sdr") {
        const pktCount = Number(d.packet_count || 0);
        const stName = userSettings.station_name || "Local Station";
        elCalModalSourceBadge.textContent = `● ${stName} (Local Sensors • ${pktCount.toLocaleString()} readings)`;
        elCalModalSourceBadge.className = "badge badge-emerald";
      } else if (d.source === "historical_archive") {
        elCalModalSourceBadge.textContent = "● Historical Archive";
        elCalModalSourceBadge.className = "badge badge-cyan";
      } else {
        const confPct = d.confidence_pct ?? 70;
        elCalModalSourceBadge.textContent = `◌ Forecast Estimate • ${confPct}% Confidence`;
        elCalModalSourceBadge.className = "badge badge-amber";
      }
    }

    if (elCalModalBody) {
      const isPast = !d.is_future;

      // Extract and normalize core values
      const highF = d.high_f !== null && d.high_f !== undefined ? Number(d.high_f) : 60.0;
      const lowF = d.low_f !== null && d.low_f !== undefined ? Number(d.low_f) : 40.0;
      const avgF = d.avg_f !== null && d.avg_f !== undefined ? Number(d.avg_f) : (highF + lowF) / 2;

      // Card 1: Temperature Range
      const thermalBarHtml = getMiniThermalBarHtml(lowF, highF, avgF);
      const card1Html = `
        <div class="cal-mini-card">
          <div class="cal-mini-card-header">
            <span class="cal-mini-card-title">🌡 Temperature Range</span>
            <span class="cal-mini-card-tag">Thermal Span</span>
          </div>
          ${thermalBarHtml}
          <div>
            <div class="cal-metric-primary">High: ${formatFAndC(highF)}</div>
            <div class="cal-metric-sub">Low: ${formatFAndC(lowF)} • Mean: ${formatFAndC(avgF)}</div>
          </div>
        </div>`;

      // Card 2: Humidity & Dew Point
      const rhAvg = d.humidity_avg !== null && d.humidity_avg !== undefined ? Number(d.humidity_avg) : 50;
      const rhMin = d.humidity_min !== null && d.humidity_min !== undefined ? Math.round(Number(d.humidity_min)) : Math.round(rhAvg * 0.85);
      const rhMax = d.humidity_max !== null && d.humidity_max !== undefined ? Math.round(Number(d.humidity_max)) : Math.min(100, Math.round(rhAvg * 1.15));
      const dpStr = d.dew_point_f !== null && d.dew_point_f !== undefined ? formatFAndC(d.dew_point_f) : "--";
      const rhArcSvg = getMiniHumidityArcSvg(rhAvg);
      const card2Html = `
        <div class="cal-mini-card">
          <div class="cal-mini-card-header">
            <span class="cal-mini-card-title">💧 Humidity &amp; Dew Point</span>
            <span class="cal-mini-card-tag">Hygrometer</span>
          </div>
          <div class="cal-mini-rh-wrap">
            ${rhArcSvg}
            <div class="cal-rh-readouts">
              <div class="cal-metric-primary">${Math.round(rhAvg)}% Avg</div>
              <div class="cal-metric-sub">Range: ${rhMin}% – ${rhMax}%</div>
            </div>
          </div>
          <div class="cal-metric-sub">Dew Point: ${escapeHtml(dpStr)}</div>
        </div>`;

      // Card 3: Barometric Pressure
      let pMin = 29.80;
      let pMax = 30.10;
      let pAvg = 29.95;
      if (isPast) {
        if (d.pressure_min_inhg !== null && d.pressure_min_inhg !== undefined) pMin = Number(d.pressure_min_inhg);
        if (d.pressure_max_inhg !== null && d.pressure_max_inhg !== undefined) pMax = Number(d.pressure_max_inhg);
        pAvg = (pMin + pMax) / 2;
      } else if (d.est_pressure_inhg !== null && d.est_pressure_inhg !== undefined) {
        pAvg = Number(d.est_pressure_inhg);
        pMin = pAvg - 0.08;
        pMax = pAvg + 0.08;
      }
      const pScaleHtml = getMiniPressureScaleHtml(pMin, pMax, pAvg);
      const hpaStr = `${(pAvg * 33.8639).toFixed(0)} hPa`;
      const card3Html = `
        <div class="cal-mini-card">
          <div class="cal-mini-card-header">
            <span class="cal-mini-card-title">⏲ Barometric Pressure</span>
            <span class="cal-mini-card-tag">${hpaStr}</span>
          </div>
          ${pScaleHtml}
          <div>
            <div class="cal-metric-primary">${isPast ? `${pMin.toFixed(2)} – ${pMax.toFixed(2)} inHg` : `${pAvg.toFixed(2)} inHg`}</div>
            <div class="cal-metric-sub">${isPast ? `Mean: ${pAvg.toFixed(2)} inHg (${hpaStr})` : `Projected Trend (${hpaStr})`}</div>
          </div>
        </div>`;

      // Card 4: Wind & Compass Rose
      const peakWind = isPast
        ? (d.peak_wind_mph !== null && d.peak_wind_mph !== undefined ? Number(d.peak_wind_mph) : 0.0)
        : (d.est_wind_mph !== null && d.est_wind_mph !== undefined ? Number(d.est_wind_mph) : 0.0);
      const avgWind = isPast
        ? (d.avg_wind_mph !== null && d.avg_wind_mph !== undefined ? Number(d.avg_wind_mph) : peakWind * 0.5)
        : peakWind * 0.65;
      const windDir = d.dominant_wind_dir || "CALM";
      const compassSvg = getMiniCompassSvg(windDir);
      const card4Html = `
        <div class="cal-mini-card">
          <div class="cal-mini-card-header">
            <span class="cal-mini-card-title">🧭 Wind &amp; Compass</span>
            <span class="cal-mini-card-tag">${escapeHtml(windDir)}</span>
          </div>
          <div class="cal-mini-wind-wrap">
            ${compassSvg}
            <div class="cal-wind-readouts">
              <div class="cal-metric-primary">Peak: ${peakWind.toFixed(1)} mph</div>
              <div class="cal-metric-sub">Avg Sustained: ${avgWind.toFixed(1)} mph</div>
            </div>
          </div>
          <div class="cal-metric-sub">Dominant Vector: ${escapeHtml(windDir)} (${CARDINAL_TO_DEG[String(windDir).toUpperCase()] ?? 0}°)</div>
        </div>`;

      // Card 5: Rain & Lightning
      const rainIn = isPast ? Number(d.rain_in || 0) : Number(d.est_rain_in || 0);
      const popPct = isPast ? null : Number(d.pop_pct || 0);
      const strikes = isPast ? Number(d.lightning_strikes || 0) : 0;
      const rainGaugeHtml = getMiniRainGaugeHtml(rainIn, popPct, strikes);
      const card5Html = `
        <div class="cal-mini-card">
          <div class="cal-mini-card-header">
            <span class="cal-mini-card-title">⛈ Rain &amp; Lightning</span>
            <span class="cal-mini-card-tag">${isPast ? `${rainIn.toFixed(2)}"` : `${Math.round(popPct || 0)}% PoP`}</span>
          </div>
          ${rainGaugeHtml}
          <div>
            <div class="cal-metric-primary">${isPast ? `${rainIn.toFixed(2)} in (${(rainIn * 25.4).toFixed(1)} mm)` : `Rain Chance: ${Math.round(popPct || 0)}% (~${rainIn.toFixed(2)}")`}</div>
            <div class="cal-metric-sub">${isPast ? `${strikes.toLocaleString()} Lightning Strikes Detected` : (popPct >= 40 ? "Elevated Rain / Storm Risk" : "Minimal Thunderstorm Risk")}</div>
          </div>
        </div>`;

      // Card 6: Solar UV & Degree Days (Past) OR Forecast Breakdown (Future)
      let card6Html = "";
      if (isPast) {
        const uvMax = d.uv_max !== null && d.uv_max !== undefined ? Number(d.uv_max) : 0.0;
        const luxPeak = d.peak_lux !== null && d.peak_lux !== undefined ? Number(d.peak_lux) : 0.0;
        const hdd = d.hdd !== null && d.hdd !== undefined ? Number(d.hdd) : 0.0;
        const cdd = d.cdd !== null && d.cdd !== undefined ? Number(d.cdd) : 0.0;
        const uvBarHtml = getMiniUvBarHtml(uvMax, luxPeak, hdd, cdd);
        card6Html = `
          <div class="cal-mini-card">
            <div class="cal-mini-card-header">
              <span class="cal-mini-card-title">☀ Solar UV &amp; Degree Days</span>
              <span class="cal-mini-card-tag">UV ${uvMax.toFixed(1)}</span>
            </div>
            ${uvBarHtml}
            <div>
              <div class="cal-metric-primary">UV Index Peak: ${uvMax.toFixed(1)}</div>
              <div class="cal-metric-sub">Luminosity: ${Math.round(luxPeak).toLocaleString()} lx • Base 65°F</div>
            </div>
          </div>`;
      } else {
        const fcSummaryHtml = getMiniForecastSummaryHtml(d.day_summary, d.night_summary);
        card6Html = `
          <div class="cal-mini-card">
            <div class="cal-mini-card-header">
              <span class="cal-mini-card-title">📅 Period Breakdown</span>
              <span class="cal-mini-card-tag">Day / Night</span>
            </div>
            ${fcSummaryHtml}
            <div class="cal-metric-sub">Confidence: ${d.confidence_pct ?? 70}% (${escapeHtml(d.source_label || "Ensemble")})</div>
          </div>`;
      }

      let explainerHtml = "";
      if (!isPast && d.confidence_reason) {
        explainerHtml = `
          <div class="cal-confidence-explainer">
            <strong>Forecast Confidence (${d.confidence_pct ?? 70}%):</strong>
            ${escapeHtml(d.confidence_reason)}
          </div>`;
      }

      elCalModalBody.innerHTML = `
        <div class="cal-modal-dashboard-grid">
          ${card1Html}
          ${card2Html}
          ${card3Html}
          ${card4Html}
          ${card5Html}
          ${card6Html}
        </div>
        ${explainerHtml}`;
    }

    elCalModalBackdrop.classList.remove("hidden");
    elCalModalBackdrop.setAttribute("aria-hidden", "false");
  }

  function closeCalendarDayModal() {
    if (!elCalModalBackdrop) return;
    elCalModalBackdrop.classList.add("hidden");
    elCalModalBackdrop.setAttribute("aria-hidden", "true");
  }

  function shiftCalendarMonth(deltaMonths) {
    let y = calendarYear;
    let m = calendarMonth + deltaMonths;
    while (m < 1) {
      m += 12;
      y -= 1;
    }
    while (m > 12) {
      m -= 12;
      y += 1;
    }
    fetchAndRenderCalendar(y, m).catch((err) => console.error("Calendar shift error:", err));
  }

  // ==========================================================================
  // VIEW 6: WEATHER MODELS MANAGER & FORECAST WEIGHTING
  // ==========================================================================
  let cachedForecastModels = [];

  function updateModelsSummaryBar() {
    if (!elModelsTotalWeight || !elModelsBlendedPreview) return;
    let totalWeight = 0;
    let sumWeightedHi = 0.0;
    let sumWeightedLo = 0.0;
    let sumWeightedPop = 0.0;
    let hiWeightAccum = 0.0;
    let loWeightAccum = 0.0;
    let popWeightAccum = 0.0;

    cachedForecastModels.forEach((m) => {
      const card = elModelsSelectorContainer ? elModelsSelectorContainer.querySelector(`[data-model-id="${m.id}"]`) : null;
      if (!card) return;
      const cb = card.querySelector(".model-enable-cb");
      const slider = card.querySelector(".model-weight-slider");
      const numSpan = card.querySelector(".model-weight-num");
      const isEnabled = cb ? cb.checked : false;
      const weightVal = slider ? parseInt(slider.value, 10) || 0 : 0;
      if (numSpan) numSpan.textContent = `${weightVal}%`;

      if (isEnabled) {
        card.classList.remove("disabled");
        if (slider) slider.disabled = false;
        totalWeight += weightVal;
        if (m.predicted_high_f !== null && m.predicted_high_f !== undefined && weightVal > 0) {
          sumWeightedHi += Number(m.predicted_high_f) * weightVal;
          hiWeightAccum += weightVal;
        }
        if (m.predicted_low_f !== null && m.predicted_low_f !== undefined && weightVal > 0) {
          sumWeightedLo += Number(m.predicted_low_f) * weightVal;
          loWeightAccum += weightVal;
        }
        if (m.pop_pct !== null && m.pop_pct !== undefined && weightVal > 0) {
          sumWeightedPop += Number(m.pop_pct) * weightVal;
          popWeightAccum += weightVal;
        }
      } else {
        card.classList.add("disabled");
        if (slider) slider.disabled = true;
      }
    });

    elModelsTotalWeight.textContent = `${totalWeight}%`;
    if (totalWeight === 100) {
      elModelsTotalWeight.style.color = "#10b981";
    } else if (totalWeight > 100) {
      elModelsTotalWeight.style.color = "#ef4444";
    } else {
      elModelsTotalWeight.style.color = "#f59e0b";
    }

    if (hiWeightAccum > 0 && loWeightAccum > 0) {
      const bHi = (sumWeightedHi / hiWeightAccum).toFixed(1);
      const bLo = (sumWeightedLo / loWeightAccum).toFixed(1);
      const bPop = popWeightAccum > 0 ? Math.round(sumWeightedPop / popWeightAccum) : 0;
      elModelsBlendedPreview.textContent = `Blended Today: High ${bHi}°F / Low ${bLo}°F • ${bPop}% Rain Chance`;
    } else {
      elModelsBlendedPreview.textContent = "Blended Today: High --°F / Low --°F";
    }
  }

  async function fetchAndRenderForecastModels() {
    if (!elModelsSelectorContainer) return;
    try {
      const res = await fetch("/api/settings/models", { headers: getAuthHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      cachedForecastModels = data.models || [];
      if (!cachedForecastModels.length) {
        elModelsSelectorContainer.innerHTML = `<div class="text-muted text-center" style="padding: 16px;">No forecast models registered.</div>`;
        return;
      }

      elModelsSelectorContainer.innerHTML = cachedForecastModels
        .map((m) => {
          const isChecked = Boolean(m.enabled);
          const weight = Number(m.weight ?? 0);
          const stLower = (m.status || "").toLowerCase();
          let statusClass = "status-connected";
          if (stLower.includes("active") || stLower.includes("live")) {
            statusClass = "status-live";
          } else if (stLower.includes("awaiting") || stLower.includes("listening") || stLower.includes("offline")) {
            statusClass = "status-waiting";
          }

          const hiStr =
            m.predicted_high_f !== null && m.predicted_high_f !== undefined
              ? `${Number(m.predicted_high_f).toFixed(1)}°F`
              : "--";
          const loStr =
            m.predicted_low_f !== null && m.predicted_low_f !== undefined
              ? `${Number(m.predicted_low_f).toFixed(1)}°F`
              : "--";
          const popStr =
            m.pop_pct !== null && m.pop_pct !== undefined ? `${m.pop_pct}% PoP` : "--";

          return `
            <div class="model-item-card ${isChecked ? "" : "disabled"}" data-model-id="${escapeHtml(m.id)}">
              <div class="model-item-left">
                <input type="checkbox" class="model-enable-cb" data-model-id="${escapeHtml(m.id)}" ${isChecked ? "checked" : ""} aria-label="Enable ${escapeHtml(m.name)}" />
                <div class="model-item-info">
                  <div class="model-item-title-row">
                    <span class="model-item-name">${escapeHtml(m.name)}</span>
                    <span class="model-status-pill ${statusClass}">${escapeHtml(m.status || "Ready")}</span>
                  </div>
                  <div class="model-item-desc">${escapeHtml(m.desc || "")}</div>
                </div>
              </div>
              <div class="model-item-right">
                <div class="model-pred-box">
                  <span class="model-pred-hl">H: ${hiStr} / L: ${loStr}</span>
                  <span class="model-pred-pop">${popStr}</span>
                </div>
                <div class="model-weight-control">
                  <input type="range" class="model-weight-slider" id="weight-slider-${escapeHtml(m.id)}"
                    data-model-id="${escapeHtml(m.id)}" min="0" max="100" step="5" value="${weight}" ${isChecked ? "" : "disabled"} aria-label="${escapeHtml(m.name)} weight percentage" />
                  <span class="model-weight-num" id="weight-num-${escapeHtml(m.id)}">${weight}%</span>
                </div>
              </div>
            </div>
          `;
        })
        .join("");

      // Bind slider and checkbox events
      elModelsSelectorContainer.querySelectorAll(".model-enable-cb").forEach((cb) => {
        cb.addEventListener("change", () => updateModelsSummaryBar());
      });
      elModelsSelectorContainer.querySelectorAll(".model-weight-slider").forEach((slider) => {
        slider.addEventListener("input", () => updateModelsSummaryBar());
      });

      updateModelsSummaryBar();
    } catch (err) {
      console.error("Forecast models load error:", err);
      elModelsSelectorContainer.innerHTML = `<div class="text-muted text-center" style="padding: 16px;">Failed to load models: ${escapeHtml(err.message)}</div>`;
    }
  }

  async function handleSaveModelWeightsClick() {
    requireUnlockedOrPrompt(async () => {
      if (!elModelsSelectorContainer) return;
      const payload = {};
      elModelsSelectorContainer.querySelectorAll(".model-item-card").forEach((card) => {
        const mid = card.getAttribute("data-model-id");
        if (!mid) return;
        const cb = card.querySelector(".model-enable-cb");
        const slider = card.querySelector(".model-weight-slider");
        payload[mid] = {
          enabled: cb ? cb.checked : true,
          weight: slider ? parseInt(slider.value, 10) || 0 : 0,
        };
      });

      try {
        const res = await authorizedWriteFetch("/api/settings/models", {
          method: "POST",
          body: JSON.stringify({ models: payload }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast("Forecast model configuration saved.");
        await fetchAndRenderForecastModels();
      } catch (err) {
        showToast(err.message || "Failed to save forecast models.");
      }
    });
  }

  // ==========================================================================
  // VIEW 6: 365-DAY BASELINE INSPECTOR MODAL & CALIBRATION OFFSETS
  // ==========================================================================
  function openBaselineModal() {
    if (!elBaselineModalBackdrop) return;
    elBaselineModalBackdrop.classList.remove("hidden");
    elBaselineModalBackdrop.setAttribute("aria-hidden", "false");
  }

  function closeBaselineModal() {
    if (!elBaselineModalBackdrop) return;
    elBaselineModalBackdrop.classList.add("hidden");
    elBaselineModalBackdrop.setAttribute("aria-hidden", "true");
  }

  async function fetchAndRenderBaselineInspector() {
    openBaselineModal();
    try {
      const res = await fetch("/api/settings/baseline", { headers: getAuthHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      // 1. Data Provenance Box
      const prov = data.provenance || {};
      if (elProvSourceVal && prov.source) elProvSourceVal.textContent = prov.source;
      if (elProvCoordsVal) {
        const lat = prov.latitude !== undefined ? Number(prov.latitude).toFixed(4) : "--";
        const lon = prov.longitude !== undefined ? Number(prov.longitude).toFixed(4) : "--";
        const elev = prov.elevation_m !== undefined ? Number(prov.elevation_m).toFixed(0) : "--";
        elProvCoordsVal.textContent = `${lat}°N, ${lon}°W (Elev: ${elev} m)`;
      }
      if (elProvRangeVal && prov.date_range) elProvRangeVal.textContent = prov.date_range;
      if (elProvProfilesVal) {
        const count = prov.profile_count ?? 12;
        const total = prov.total_records_ingested ?? 8760;
        elProvProfilesVal.textContent = `${count} Active Climatological Normals (${total.toLocaleString()} hours reanalyzed)`;
      }

      // 2. 12-Month Climatology Table
      if (elBaselineMonthlyTbody) {
        const normals = data.climatology_normals || [];
        if (!normals.length) {
          elBaselineMonthlyTbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">Baseline not yet initialized. Click "Re-download 365-Day Archive" below to generate profiles.</td></tr>`;
        } else {
          elBaselineMonthlyTbody.innerHTML = normals
            .map((n) => {
              const hi = n.avg_high_f !== null && n.avg_high_f !== undefined ? `${Number(n.avg_high_f).toFixed(1)}°F` : "--";
              const lo = n.avg_low_f !== null && n.avg_low_f !== undefined ? `${Number(n.avg_low_f).toFixed(1)}°F` : "--";
              const mean = n.mean_temp_f !== null && n.mean_temp_f !== undefined ? `${Number(n.mean_temp_f).toFixed(1)}°F` : "--";
              const baro = n.normal_baro_inhg !== null && n.normal_baro_inhg !== undefined ? `${Number(n.normal_baro_inhg).toFixed(2)} inHg` : "--";
              const rainy = n.rainy_day_pct !== null && n.rainy_day_pct !== undefined ? `${Number(n.rainy_day_pct).toFixed(1)}%` : "--";
              const precip = n.normal_precip_in !== null && n.normal_precip_in !== undefined ? `${Number(n.normal_precip_in).toFixed(2)} in` : "--";
              return `
                <tr>
                  <td><strong>${escapeHtml(n.month_name || `Month ${n.month}`)}</strong></td>
                  <td class="mono font-bold" style="color: #fbbf24;">${hi}</td>
                  <td class="mono font-bold" style="color: #38bdf8;">${lo}</td>
                  <td class="mono">${mean}</td>
                  <td class="mono">${baro}</td>
                  <td class="mono">${rainy}</td>
                  <td class="mono">${precip}</td>
                </tr>
              `;
            })
            .join("");
        }
      }

      // 3. Calibration Offsets
      const offsets = data.offsets || {};
      if (elInputBaselineTempOffset) {
        elInputBaselineTempOffset.value = String(offsets.temp_offset_f ?? 0.0);
      }
      if (elInputBaselineBaroOffset) {
        elInputBaselineBaroOffset.value = String(offsets.baro_offset_inhg ?? 0.00);
      }
    } catch (err) {
      console.error("Baseline inspector load error:", err);
      showToast(err.message || "Failed to load baseline inspector data.");
    }
  }

  async function handleSaveBaselineOffsetsClick() {
    requireUnlockedOrPrompt(async () => {
      const temp_offset_f = elInputBaselineTempOffset ? parseFloat(elInputBaselineTempOffset.value) || 0.0 : 0.0;
      const baro_offset_inhg = elInputBaselineBaroOffset ? parseFloat(elInputBaselineBaroOffset.value) || 0.0 : 0.0;
      try {
        const res = await authorizedWriteFetch("/api/settings/baseline/offsets", {
          method: "POST",
          body: JSON.stringify({ temp_offset_f, baro_offset_inhg }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast("Baseline calibration offsets saved.");
        await fetchAndRenderBaselineInspector();
      } catch (err) {
        showToast(err.message || "Failed to save baseline offsets.");
      }
    });
  }

  async function handleModalRetrainBaselineClick() {
    requireUnlockedOrPrompt(async () => {
      if (elBtnModalRetrainBaseline) elBtnModalRetrainBaseline.disabled = true;
      showToast("Re-downloading 365-day historical reanalysis archive...");
      try {
        const res = await authorizedWriteFetch("/api/forecast/learn-history", {
          method: "POST",
          body: JSON.stringify({ force_refresh: true }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast("365-Day historical baseline re-downloaded and trained successfully.");
        await fetchAndRenderBaselineInspector();
        await loadSettingsIntoForms();
      } catch (err) {
        showToast(err.message || "Failed to re-download baseline archive.");
      } finally {
        if (elBtnModalRetrainBaseline) elBtnModalRetrainBaseline.disabled = false;
      }
    });
  }

  // ==========================================================================
  // NAVIGATION & POLLING ORCHESTRATION
  // ==========================================================================
  const TAB_TITLES = {
    overview: "Overview Dashboard",
    calendar: "Weather Calendar",
    charts: "Historical Charts & Climatology",
    radar: "Radar & NWS Forecast",
    sensors: "Sensors Manager",
    logs: "SQLite Log Explorer",
    settings: "Settings",
  };

  function openNavDrawer() {
    closeStatusPopover();
    if (elNavDrawer) elNavDrawer.classList.add("open");
    if (elNavDrawerBackdrop) elNavDrawerBackdrop.classList.add("open");
    if (elBtnNavDrawerToggle) elBtnNavDrawerToggle.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
  }

  function closeNavDrawer() {
    if (elNavDrawer) elNavDrawer.classList.remove("open");
    if (elNavDrawerBackdrop) elNavDrawerBackdrop.classList.remove("open");
    if (elBtnNavDrawerToggle) elBtnNavDrawerToggle.setAttribute("aria-expanded", "false");
    document.body.style.overflow = "";
  }

  function toggleNavDrawer() {
    if (elNavDrawer && elNavDrawer.classList.contains("open")) {
      closeNavDrawer();
    } else {
      openNavDrawer();
    }
  }

  function openStatusPopover() {
    closeNavDrawer();
    if (elStatusPopover) elStatusPopover.classList.remove("hidden");
    if (elStatusPopoverBtn) elStatusPopoverBtn.setAttribute("aria-expanded", "true");
  }

  function closeStatusPopover() {
    if (elStatusPopover) elStatusPopover.classList.add("hidden");
    if (elStatusPopoverBtn) elStatusPopoverBtn.setAttribute("aria-expanded", "false");
  }

  function toggleStatusPopover() {
    if (elStatusPopover && !elStatusPopover.classList.contains("hidden")) {
      closeStatusPopover();
    } else {
      openStatusPopover();
    }
  }

  function switchTab(tabName) {
    closeStatusPopover();
    if (tabName === "settings" && !authState.unlocked) {
      requireUnlockedOrPrompt(() => switchTab("settings"));
      return;
    }

    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === tabName);
    });
    document.querySelectorAll(".drawer-nav-item").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === tabName);
    });
    document.querySelectorAll(".view-panel").forEach((p) => {
      p.classList.toggle("active", p.id === `view-${tabName}`);
    });
    if (elTopBarActiveView) {
      elTopBarActiveView.textContent = TAB_TITLES[tabName] || "Overview Dashboard";
    }
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
        if (!windyRadarActive && radarPlaying) {
          startRadarLoop();
        }
      });
      fetchNwsSummary(false).catch((err) => console.error("NWS load error:", err));
    } else if (activeTab === "calendar") {
      fetchAndRenderCalendar(calendarYear, calendarMonth).catch((err) =>
        console.error("Calendar load error:", err)
      );
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
      if ((activeTab === "calendar" && !calendarLoadedOnce) || (activeTab === "calendar" && forceActiveViewRefresh)) {
        fetchAndRenderCalendar(calendarYear, calendarMonth).catch((err) =>
          console.error("SkyScry calendar error:", err)
        );
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
    // Top Bar & Slide-Out Navigation Drawer
    if (elBtnNavDrawerToggle) {
      elBtnNavDrawerToggle.addEventListener("click", toggleNavDrawer);
    }
    if (elBtnNavDrawerClose) {
      elBtnNavDrawerClose.addEventListener("click", closeNavDrawer);
    }
    if (elNavDrawerBackdrop) {
      elNavDrawerBackdrop.addEventListener("click", closeNavDrawer);
    }

    // Status Popover Trigger & Controls
    if (elStatusPopoverBtn) {
      elStatusPopoverBtn.addEventListener("click", (evt) => {
        evt.stopPropagation();
        toggleStatusPopover();
      });
    }
    if (elBtnPopoverClose) {
      elBtnPopoverClose.addEventListener("click", (evt) => {
        evt.stopPropagation();
        closeStatusPopover();
      });
    }
    if (elBtnPopoverAuth) {
      elBtnPopoverAuth.addEventListener("click", (evt) => {
        evt.stopPropagation();
        if (authState.unlocked) {
          handleLockNowClick();
        } else {
          closeStatusPopover();
          requireUnlockedOrPrompt(() => {});
        }
      });
    }
    const elBtnReconnectSdr = document.getElementById("btn-reconnect-sdr");
    if (elBtnReconnectSdr) {
      elBtnReconnectSdr.addEventListener("click", async (evt) => {
        evt.stopPropagation();
        elBtnReconnectSdr.disabled = true;
        elBtnReconnectSdr.textContent = "Resetting USB...";
        try {
          const res = await fetch("/api/sdr/reset", {
            method: "POST",
            headers: getAuthHeaders(),
          });
          const d = await res.json();
          if (d.ok) {
            showToast("USB reset & reconnect sequence completed.");
          } else {
            showToast(d.error || d.message || "Reset failed.");
          }
        } catch (err) {
          showToast("Error resetting USB / reconnecting SDR: " + err.message);
        } finally {
          await fetchStatus();
          elBtnReconnectSdr.disabled = false;
          elBtnReconnectSdr.textContent = "Reset USB & Reconnect";
        }
      });
    }
    document.addEventListener("click", (evt) => {
      if (
        elStatusPopover &&
        !elStatusPopover.classList.contains("hidden") &&
        !elStatusPopover.contains(evt.target) &&
        elStatusPopoverBtn &&
        !elStatusPopoverBtn.contains(evt.target)
      ) {
        closeStatusPopover();
      }
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") {
        closeStatusPopover();
        closeCalendarDayModal();
        closeBaselineModal();
        if (elNavDrawer && elNavDrawer.classList.contains("open")) {
          closeNavDrawer();
        }
      }
    });

    // Weather Calendar controls & Day Detail Modal listeners
    if (elBtnCalPrev) {
      elBtnCalPrev.addEventListener("click", () => shiftCalendarMonth(-1));
    }
    if (elBtnCalNext) {
      elBtnCalNext.addEventListener("click", () => shiftCalendarMonth(1));
    }
    if (elBtnCalToday) {
      elBtnCalToday.addEventListener("click", () => {
        const now = new Date();
        fetchAndRenderCalendar(now.getFullYear(), now.getMonth() + 1).catch((err) =>
          console.error("Calendar today error:", err)
        );
      });
    }
    if (elBtnCloseCalModal) {
      elBtnCloseCalModal.addEventListener("click", closeCalendarDayModal);
    }
    if (elBtnDismissCalModal) {
      elBtnDismissCalModal.addEventListener("click", closeCalendarDayModal);
    }
    if (elCalModalBackdrop) {
      elCalModalBackdrop.addEventListener("click", (evt) => {
        if (evt.target === elCalModalBackdrop) closeCalendarDayModal();
      });
    }

    document.querySelectorAll(".drawer-nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-tab") || "overview";
        switchTab(target);
        closeNavDrawer();
      });
    });

    if (elBtnDrawerAuth) {
      elBtnDrawerAuth.addEventListener("click", () => {
        closeNavDrawer();
        if (authState.unlocked) {
          handleLockNowClick();
        } else {
          requireUnlockedOrPrompt(() => {});
        }
      });
    }

    [elBtnTriggerUpdate, elBtnPurgeOutliers, elBtnSyncRef].forEach((btn) => {
      if (btn) {
        btn.addEventListener("click", () => {
          closeNavDrawer();
        });
      }
    });

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
    function toggleWindyEmbed(enable) {
      if (typeof enable !== "boolean") {
        enable = !windyRadarActive;
      }
      windyRadarActive = enable;
      if (windyRadarActive) {
        stopRadarLoop();
        radarPlaying = false;
        if (elBtnRadarPlay) elBtnRadarPlay.textContent = "▶ Play Loop";
        if (elFullRadarMap) elFullRadarMap.classList.add("hidden");
        if (elRadarScrubberBar) elRadarScrubberBar.classList.add("hidden");
        if (elWindyRadarContainer) elWindyRadarContainer.classList.remove("hidden");

        const rawLat = parseFloat(userSettings.latitude);
        const rawLon = parseFloat(userSettings.longitude);
        const lat = Number.isFinite(rawLat) ? rawLat : 0.0;
        const lon = Number.isFinite(rawLon) ? rawLon : 0.0;
        const zoom = Math.max(3, Math.min(11, Number(userSettings.radar_zoom || 7)));
        const windyUrl = `https://embed.windy.com/embed.html?type=map&location=coordinates&metricRain=in&metricTemp=%C2%B0F&metricWind=mph&zoom=${zoom}&overlay=radar&product=radar&level=surface&lat=${lat}&lon=${lon}`;
        if (elWindyRadarIframe && (!elWindyRadarIframe.src || elWindyRadarIframe.src === "about:blank" || !elWindyRadarIframe.src.includes("windy.com"))) {
          elWindyRadarIframe.src = windyUrl;
        }
        if (elBtnToggleWindy) {
          elBtnToggleWindy.textContent = "Switch to NOAA WMS Radar";
          elBtnToggleWindy.classList.add("btn-primary");
          elBtnToggleWindy.classList.remove("btn-secondary");
        }
      } else {
        if (elWindyRadarContainer) elWindyRadarContainer.classList.add("hidden");
        if (elFullRadarMap) elFullRadarMap.classList.remove("hidden");
        if (elRadarScrubberBar) elRadarScrubberBar.classList.remove("hidden");
        if (elBtnToggleWindy) {
          elBtnToggleWindy.textContent = "Switch to Windy Live Radar";
          elBtnToggleWindy.classList.remove("btn-primary");
          elBtnToggleWindy.classList.add("btn-secondary");
        }
        if (fullRadarMap) {
          setTimeout(() => {
            fullRadarMap.invalidateSize();
          }, 50);
        }
      }
    }

    // Radar Opacity Sliders (View 5 & Mini Radar)
    if (elRadarOpacitySlider) {
      elRadarOpacitySlider.value = String(Math.round(radarOpacity * 100));
      if (elRadarOpacityVal) elRadarOpacityVal.textContent = `${Math.round(radarOpacity * 100)}%`;
      elRadarOpacitySlider.addEventListener("input", (e) => {
        setRadarOpacity(Number(e.target.value) / 100);
      });
    }
    if (elMiniRadarOpacitySlider) {
      elMiniRadarOpacitySlider.value = String(Math.round(radarOpacity * 100));
      if (elMiniRadarOpacityVal) elMiniRadarOpacityVal.textContent = `${Math.round(radarOpacity * 100)}%`;
      elMiniRadarOpacitySlider.addEventListener("input", (e) => {
        setRadarOpacity(Number(e.target.value) / 100);
      });
    }

    if (elBtnLayerPrecip) {
      elBtnLayerPrecip.addEventListener("click", () => setRadarMode("precip"));
    }
    if (elBtnLayerSat) {
      elBtnLayerSat.addEventListener("click", () => setRadarMode("sat"));
    }
    if (elBtnToggleWindy) {
      elBtnToggleWindy.addEventListener("click", () => toggleWindyEmbed());
    }

    // Set initial button active states
    if (elBtnLayerPrecip) {
      elBtnLayerPrecip.classList.toggle("btn-primary", radarLayerMode === "precip");
      elBtnLayerPrecip.classList.toggle("btn-secondary", radarLayerMode !== "precip");
    }
    if (elBtnLayerSat) {
      elBtnLayerSat.classList.toggle("btn-primary", radarLayerMode === "sat");
      elBtnLayerSat.classList.toggle("btn-secondary", radarLayerMode !== "sat");
    }
    updateColorLegend(radarLayerMode);

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
        showRadarFrame(Number(elRadarFrameSlider.value || 0), true);
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
      const rawLat = elSetLatitude ? parseFloat(elSetLatitude.value) : 0.0;
      const rawLon = elSetLongitude ? parseFloat(elSetLongitude.value) : 0.0;
      const rawElev = elSetElevation ? parseFloat(elSetElevation.value) : 0.0;
      const lat = Number.isFinite(rawLat) ? rawLat : 0.0;
      const lon = Number.isFinite(rawLon) ? rawLon : 0.0;
      const elev = Number.isFinite(rawElev) ? rawElev : 0.0;
      saveSettingsPayload(
        {
          station_name: elSetStationName ? elSetStationName.value.trim() : "",
          city_state_label: elSetCityStateQuery ? elSetCityStateQuery.value.trim() : "",
          latitude: lat,
          longitude: lon,
          elevation_m: elev,
          radar_zoom: elSetRadarZoom ? Number(elSetRadarZoom.value || 7) : 7,
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

    // New v0.0.16 Event Listeners: Geocode, Check-Ins, Model Sync, Clock Sync & Lightning Simulation
    if (elBtnGeocodeLookup) {
      elBtnGeocodeLookup.addEventListener("click", handleGeocodeLookupClick);
    }
    if (elBtnSyncLearnHistory) {
      elBtnSyncLearnHistory.addEventListener("click", handleSyncLearnHistoryClick);
    }
    if (elBtnSyncClockNow) {
      elBtnSyncClockNow.addEventListener("click", handleSyncClockNowClick);
    }
    if (elBtnOvSimulateStrike) {
      elBtnOvSimulateStrike.addEventListener("click", triggerSimulatedLightningStrike);
    }
    if (elBtnSetSimulateStrike) {
      elBtnSetSimulateStrike.addEventListener("click", triggerSimulatedLightningStrike);
    }
    if (elSetBaroWeight && elSetBaroWeightVal) {
      elSetBaroWeight.addEventListener("input", () => {
        elSetBaroWeightVal.textContent = `${Number(elSetBaroWeight.value).toFixed(1)}x`;
      });
    }
    if (elSetDewWeight && elSetDewWeightVal) {
      elSetDewWeight.addEventListener("input", () => {
        elSetDewWeightVal.textContent = `${Number(elSetDewWeight.value).toFixed(1)}x`;
      });
    }
    if (elBtnSaveLearningWeights) {
      elBtnSaveLearningWeights.addEventListener("click", () => {
        saveSettingsPayload(
          {
            forecast_baro_weight: elSetBaroWeight ? Number(elSetBaroWeight.value) : 1.0,
            forecast_dew_weight: elSetDewWeight ? Number(elSetDewWeight.value) : 1.0,
            auto_vacuum_schedule: elSetVacuumSchedule ? elSetVacuumSchedule.value : "daily_0300",
          },
          "Forecast model weights & maintenance schedule saved."
        );
      });
    }

    // Weather Models Manager & Baseline Inspector Event Listeners
    if (elBtnSaveModelWeights) {
      elBtnSaveModelWeights.addEventListener("click", handleSaveModelWeightsClick);
    }
    if (elBtnOpenBaselineInspector) {
      elBtnOpenBaselineInspector.addEventListener("click", fetchAndRenderBaselineInspector);
    }
    if (elBtnCloseBaselineModal) {
      elBtnCloseBaselineModal.addEventListener("click", closeBaselineModal);
    }
    if (elBtnDismissBaselineModal) {
      elBtnDismissBaselineModal.addEventListener("click", closeBaselineModal);
    }
    if (elBaselineModalBackdrop) {
      elBaselineModalBackdrop.addEventListener("click", (evt) => {
        if (evt.target === elBaselineModalBackdrop) closeBaselineModal();
      });
    }
    if (elBtnSaveBaselineOffsets) {
      elBtnSaveBaselineOffsets.addEventListener("click", handleSaveBaselineOffsetsClick);
    }
    if (elBtnModalRetrainBaseline) {
      elBtnModalRetrainBaseline.addEventListener("click", handleModalRetrainBaselineClick);
    }
    document.querySelectorAll(".btn-checkin-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".btn-checkin-pill").forEach((b) => b.classList.remove("active-feedback"));
        btn.classList.add("active-feedback");
        const rating = btn.getAttribute("data-rating") || "spot_on";
        submitWeatherFeedback(rating, false);
      });
    });
    document.querySelectorAll(".btn-set-weather-checkin").forEach((btn) => {
      btn.addEventListener("click", () => {
        const condStr = btn.getAttribute("data-condition") || "Sunny/Clear";
        submitWeatherFeedback(condStr, true);
      });
    });
    if (elBtnDismissCheckin && elOvCheckinBar) {
      elBtnDismissCheckin.addEventListener("click", () => {
        try {
          localStorage.setItem("skyscry_checkin_dismiss_until", String(Date.now() + 8 * 3600 * 1000));
        } catch (_) {}
        elOvCheckinBar.classList.add("hidden");
      });
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

    // Initial startup: load persisted SQLite system_settings first so saved coordinates are active immediately
    if (elHeroIcon) {
      elHeroIcon.innerHTML = getAnimatedWeatherSvg("partly-cloudy-day");
    }
    loadSettingsIntoForms()
      .catch(() => {})
      .finally(() => {
        refreshAll(false).then(() => {
          initAndRefreshRadarMaps();
        });
      });
    setInterval(() => refreshAll(false), POLL_INTERVAL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initEventListenersAndStart);
  } else {
    initEventListenersAndStart();
  }
})();

/**
 * SkyScry v0.1.1 — Frontend Controller
 * Vanilla JS controller for Live Sensor Grid, Inline Sensor Registry Editor,
 * and Historical Telemetry / Chart.js Explorer.
 */

(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;

  let sensorsCache = [];
  let historyReadingsCache = [];
  let telemetryChart = null;
  let activeTab = "live-grid";

  // DOM Elements
  const elSdrStatus = document.getElementById("stat-sdr-status");
  const elSensorCount = document.getElementById("stat-sensor-count");
  const elReadingCount = document.getElementById("stat-reading-count");
  const elDbSize = document.getElementById("stat-db-size");
  const elUptime = document.getElementById("stat-uptime");
  const elSyncDot = document.getElementById("stat-sync-indicator");
  const elLastSync = document.getElementById("stat-last-sync");
  const elErrorBanner = document.getElementById("sdr-error-banner");

  const elSensorGrid = document.getElementById("sensor-grid");
  const elSensorEmpty = document.getElementById("sensor-grid-empty");
  const elToggleIgnored = document.getElementById("toggle-show-ignored");
  const elBtnRefresh = document.getElementById("btn-refresh-now");

  // Explorer DOM Elements
  const elHistorySensor = document.getElementById("history-sensor-select");
  const elHistoryHours = document.getElementById("history-hours-select");
  const elHistoryMetric = document.getElementById("history-metric-select");
  const elHistoryLimit = document.getElementById("history-limit-select");
  const elBtnRunQuery = document.getElementById("btn-run-query");
  const elHistorySummary = document.getElementById("history-record-summary");
  const elHistoryTableBody = document.getElementById("history-table-body");
  const elChartTitle = document.getElementById("chart-title");

  // Modal DOM Elements
  const elModalBackdrop = document.getElementById("sensor-modal-backdrop");
  const elModalForm = document.getElementById("sensor-config-form");
  const elModalSubtitle = document.getElementById("modal-subtitle");
  const elModalSensorKey = document.getElementById("modal-sensor-key");
  const elModalAlias = document.getElementById("modal-alias-input");
  const elModalLocation = document.getElementById("modal-location-select");
  const elModalTrusted = document.getElementById("modal-trusted-checkbox");
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

  function formatRelativeTime(isoString) {
    if (!isoString) return "never";
    const dt = new Date(isoString);
    if (isNaN(dt.getTime())) return isoString;
    const diffSec = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
    if (diffSec < 5) return "just now";
    if (diffSec < 60) return `${diffSec}s ago`;
    const mins = Math.floor(diffSec / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 48) return `${hrs}h ${mins % 60}m ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  function formatUtcTimestamp(isoString) {
    if (!isoString) return "--";
    const dt = new Date(isoString);
    if (isNaN(dt.getTime())) return isoString;
    return dt.toISOString().replace("T", " ").substring(0, 19) + "Z";
  }

  async function fetchStatus() {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const sdrState = (data.sdr_status || "disconnected").toLowerCase();
    elSdrStatus.textContent = sdrState.toUpperCase();
    elSdrStatus.className = "status-pill " + (
      sdrState === "running" ? "status-running" : "status-disconnected"
    );

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

  async function fetchSensors() {
    const res = await fetch("/api/sensors");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    sensorsCache = Array.isArray(data.sensors) ? data.sensors : [];
    renderSensorGrid();
    populateSensorSelect();
  }

  function renderSensorGrid() {
    const showIgnored = elToggleIgnored.checked;
    const filtered = sensorsCache.filter((s) => {
      if (!showIgnored && s.location_type === "ignore") return false;
      return true;
    });

    if (filtered.length === 0) {
      elSensorGrid.innerHTML = "";
      elSensorEmpty.classList.remove("hidden");
      return;
    }

    elSensorEmpty.classList.add("hidden");

    const cardsHtml = filtered.map((sensor) => {
      const r = sensor.latest_reading || {};
      const defaultHardwareId = `${sensor.model} #${sensor.sensor_id}`;
      const displayName = sensor.alias && sensor.alias.trim() !== ""
        ? sensor.alias.trim()
        : defaultHardwareId;

      const subtitleParts = [`KEY: ${sensor.sensor_key}`];
      if (sensor.alias) {
        subtitleParts.unshift(defaultHardwareId);
      }
      if (sensor.channel) {
        subtitleParts.push(`CH ${sensor.channel}`);
      }

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

      const tempPrimary = r.temp_f !== null && r.temp_f !== undefined
        ? `${Number(r.temp_f).toFixed(1)}&deg;F <span class="temp-sub">(${Number(r.temp_c).toFixed(1)}&deg;C)</span>`
        : `--&deg;F`;

      const rhReadout = r.humidity !== null && r.humidity !== undefined
        ? `RH ${Number(r.humidity).toFixed(0)}%`
        : `RH --%`;

      const secondaryItems = [];

      if (r.wind_avg_mph !== null && r.wind_avg_mph !== undefined) {
        let windVal = `${Number(r.wind_avg_mph).toFixed(1)} mph (${Number(r.wind_avg_mps).toFixed(1)} m/s)`;
        if (r.wind_cardinal) {
          windVal += ` ${escapeHtml(r.wind_cardinal)}`;
        }
        secondaryItems.push({ label: "Wind Avg", val: windVal });
      }

      if (r.wind_gust_mph !== null && r.wind_gust_mph !== undefined) {
        secondaryItems.push({
          label: "Wind Gust",
          val: `${Number(r.wind_gust_mph).toFixed(1)} mph (${Number(r.wind_gust_mps).toFixed(1)} m/s)`,
        });
      }

      if (r.rain_cumulative_in !== null && r.rain_cumulative_in !== undefined) {
        secondaryItems.push({
          label: "Rain Cumulative",
          val: `${Number(r.rain_cumulative_in).toFixed(2)} in (${Number(r.rain_cumulative_mm).toFixed(1)} mm)`,
        });
      }

      if (r.pressure_hpa !== null && r.pressure_hpa !== undefined) {
        secondaryItems.push({
          label: "Pressure",
          val: `${Number(r.pressure_hpa).toFixed(1)} hPa`,
        });
      }

      if (r.uv_index !== null && r.uv_index !== undefined) {
        secondaryItems.push({
          label: "UV Index",
          val: `${Number(r.uv_index).toFixed(1)}`,
        });
      }

      if (r.lux !== null && r.lux !== undefined) {
        secondaryItems.push({
          label: "Light",
          val: `${Number(r.lux).toFixed(0)} lux`,
        });
      }

      const secondaryHtml = secondaryItems.length > 0
        ? `<div class="telemetry-secondary">` +
          secondaryItems
            .map(
              (item) => `
              <div class="kv-item">
                <span class="kv-label">${escapeHtml(item.label)}</span>
                <span class="kv-val">${item.val}</span>
              </div>`
            )
            .join("") +
          `</div>`
        : "";

      const trustedTag = sensor.is_trusted ? "" : `<span class="badge">UNTRUSTED</span>`;

      return `
        <article class="sensor-card ${sensor.is_trusted ? "" : "untrusted"}">
          <div class="sensor-card-header">
            <div>
              <div class="sensor-title">${escapeHtml(displayName)}</div>
              <div class="sensor-subtitle">${escapeHtml(subtitleParts.join(" | "))}</div>
            </div>
            <div class="badge-group">
              <span class="${locBadgeClass}">${escapeHtml(locType)}</span>
              ${trustedTag}
              ${battBadge}
            </div>
          </div>

          <div class="telemetry-primary">
            <div class="temp-Readout">${tempPrimary}</div>
            <div class="rh-readout">${rhReadout}</div>
          </div>

          ${secondaryHtml}

          <div class="sensor-card-footer">
            <span class="last-seen-text" title="${escapeHtml(sensor.last_seen)}">
              Last packet: ${escapeHtml(formatRelativeTime(sensor.last_seen))}
            </span>
            <button
              class="btn btn-secondary btn-sm btn-configure-sensor"
              type="button"
              data-sensor-key="${escapeHtml(sensor.sensor_key)}"
            >
              Configure
            </button>
          </div>
        </article>
      `;
    });

    elSensorGrid.innerHTML = cardsHtml.join("");

    document.querySelectorAll(".btn-configure-sensor").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-sensor-key");
        openSensorModal(key);
      });
    });
  }

  function populateSensorSelect() {
    const currentVal = elHistorySensor.value;
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
    elHistorySensor.innerHTML = options.join("");
  }

  function openSensorModal(sensorKey) {
    const sensor = sensorsCache.find((s) => s.sensor_key === sensorKey);
    if (!sensor) return;

    elModalSensorKey.value = sensor.sensor_key;
    elModalSubtitle.textContent = `${sensor.model} #${sensor.sensor_id} (Key: ${sensor.sensor_key})`;
    elModalAlias.value = sensor.alias || "";
    elModalLocation.value = sensor.location_type || "unassigned";
    elModalTrusted.checked = Boolean(sensor.is_trusted);
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

    const payload = {
      alias: elModalAlias.value.trim(),
      location_type: elModalLocation.value,
      is_trusted: elModalTrusted.checked,
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
      await refreshAll();
    } catch (err) {
      elModalFeedback.textContent = err.message || "Failed to update sensor metadata.";
      elModalFeedback.className = "form-feedback error";
      elModalFeedback.classList.remove("hidden");
    }
  }

  async function loadHistoricalTelemetry() {
    const sensorKey = elHistorySensor.value;
    const hours = elHistoryHours.value || "24";
    const limit = elHistoryLimit.value || "250";

    const params = new URLSearchParams({ hours, limit });
    if (sensorKey) params.set("sensor_key", sensorKey);

    const res = await fetch(`/api/readings?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    historyReadingsCache = Array.isArray(data.readings) ? data.readings : [];

    elHistorySummary.textContent = `${historyReadingsCache.length} records loaded`;
    renderHistoryTable();
    renderHistoryChart();
  }

  function renderHistoryTable() {
    if (historyReadingsCache.length === 0) {
      elHistoryTableBody.innerHTML = `
        <tr>
          <td colspan="10" class="text-center text-muted">No telemetry records found for the selected filter.</td>
        </tr>`;
      return;
    }

    const rowsHtml = historyReadingsCache.map((r) => {
      const tempCell = r.temp_f !== null && r.temp_f !== undefined
        ? `${Number(r.temp_f).toFixed(1)}F / ${Number(r.temp_c).toFixed(1)}C`
        : "--";
      const rhCell = r.humidity !== null && r.humidity !== undefined
        ? `${Number(r.humidity).toFixed(0)}%`
        : "--";
      let windCell = "--";
      if (r.wind_avg_mph !== null && r.wind_avg_mph !== undefined) {
        windCell = `${Number(r.wind_avg_mph).toFixed(1)} mph`;
        if (r.wind_cardinal) windCell += ` ${escapeHtml(r.wind_cardinal)}`;
      }
      const rainCell = r.rain_cumulative_in !== null && r.rain_cumulative_in !== undefined
        ? `${Number(r.rain_cumulative_in).toFixed(2)} in (${Number(r.rain_cumulative_mm).toFixed(1)} mm)`
        : "--";
      const uvLuxParts = [];
      if (r.uv_index !== null && r.uv_index !== undefined) uvLuxParts.push(`UV ${Number(r.uv_index).toFixed(1)}`);
      if (r.lux !== null && r.lux !== undefined) uvLuxParts.push(`${Number(r.lux).toFixed(0)} lx`);
      const uvLuxCell = uvLuxParts.length > 0 ? uvLuxParts.join(" / ") : "--";

      let battCell = "--";
      if (r.battery_ok === true) battCell = `<span class="badge badge-batt-ok">OK</span>`;
      else if (r.battery_ok === false) battCell = `<span class="badge badge-batt-low">LOW</span>`;

      return `
        <tr>
          <td class="mono">${escapeHtml(formatUtcTimestamp(r.timestamp))}</td>
          <td><strong>${escapeHtml(r.display_name)}</strong></td>
          <td class="mono text-muted">${escapeHtml(r.sensor_key)}</td>
          <td><span class="badge">${escapeHtml(r.location_type || "unassigned")}</span></td>
          <td class="mono">${tempCell}</td>
          <td class="mono">${rhCell}</td>
          <td class="mono">${windCell}</td>
          <td class="mono">${rainCell}</td>
          <td class="mono">${uvLuxCell}</td>
          <td>${battCell}</td>
        </tr>
      `;
    });

    elHistoryTableBody.innerHTML = rowsHtml.join("");
  }

  function renderHistoryChart() {
    if (typeof Chart === "undefined") return;

    const metricKey = elHistoryMetric.value;
    const metricLabels = {
      temp_f: "Temperature (deg F)",
      temp_c: "Temperature (deg C)",
      humidity: "Relative Humidity (%)",
      wind_avg_mph: "Wind Speed Avg (mph)",
      rain_cumulative_in: "Cumulative Rain (in)",
      pressure_hpa: "Barometric Pressure (hPa)",
    };
    elChartTitle.textContent = `Time-Series — ${metricLabels[metricKey] || metricKey}`;

    const chronological = [...historyReadingsCache].reverse();
    const seriesBySensor = new Map();

    chronological.forEach((r) => {
      const val = r[metricKey];
      if (val === null || val === undefined) return;
      const key = r.display_name || r.sensor_key;
      if (!seriesBySensor.has(key)) {
        seriesBySensor.set(key, []);
      }
      seriesBySensor.get(key).push({
        label: formatUtcTimestamp(r.timestamp).substring(11, 19),
        value: Number(val),
      });
    });

    const palette = [
      "#3b82f6",
      "#10b981",
      "#f59e0b",
      "#ec4899",
      "#8b5cf6",
      "#06b6d4",
      "#f97316",
    ];

    const allLabels = chronological.map((r) =>
      formatUtcTimestamp(r.timestamp).substring(5, 19)
    );

    const datasets = [];
    let colorIdx = 0;
    seriesBySensor.forEach((points, sensorName) => {
      const color = palette[colorIdx % palette.length];
      colorIdx += 1;
      datasets.push({
        label: sensorName,
        data: chronological.map((r) => {
          const name = r.display_name || r.sensor_key;
          if (name !== sensorName) return null;
          return r[metricKey] !== null && r[metricKey] !== undefined
            ? Number(r[metricKey])
            : null;
        }),
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: 2,
        spanGaps: true,
        tension: 0.2,
      });
    });

    const ctx = document.getElementById("telemetry-chart").getContext("2d");
    if (telemetryChart) {
      telemetryChart.destroy();
    }

    telemetryChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: allLabels,
        datasets: datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: "index",
          intersect: false,
        },
        plugins: {
          legend: {
            labels: { color: "#cbd5e1", font: { size: 12 } },
          },
        },
        scales: {
          x: {
            ticks: { color: "#64748b", maxTicksLimit: 10 },
            grid: { color: "rgba(38, 51, 77, 0.5)" },
          },
          y: {
            ticks: { color: "#94a3b8" },
            grid: { color: "rgba(38, 51, 77, 0.5)" },
          },
        },
      },
    });
  }

  async function refreshAll() {
    elSyncDot.classList.add("syncing");
    try {
      await Promise.all([fetchStatus(), fetchSensors()]);
      if (activeTab === "history-explorer") {
        await loadHistoricalTelemetry();
      }
      const now = new Date();
      elLastSync.textContent = now.toTimeString().split(" ")[0];
    } catch (err) {
      console.error("Failed to sync telemetry:", err);
    } finally {
      setTimeout(() => elSyncDot.classList.remove("syncing"), 250);
    }
  }

  // Event Bindings
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));

      btn.classList.add("active");
      activeTab = btn.getAttribute("data-tab");
      const panel = document.getElementById(`tab-${activeTab}`);
      if (panel) panel.classList.add("active");

      if (activeTab === "history-explorer") {
        loadHistoricalTelemetry();
      }
    });
  });

  elToggleIgnored.addEventListener("change", renderSensorGrid);
  elBtnRefresh.addEventListener("click", refreshAll);
  elBtnRunQuery.addEventListener("click", loadHistoricalTelemetry);
  elHistoryMetric.addEventListener("change", renderHistoryChart);

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

  // Initial Load & Auto-Polling
  refreshAll();
  setInterval(refreshAll, POLL_INTERVAL_MS);
})();


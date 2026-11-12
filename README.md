# SkyScry

![Version](https://img.shields.io/badge/version-0.0.32-blue.svg)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

**SkyScry** is an open-source, cross-platform **433.92 MHz microclimate telemetry and weather ingestion daemon** with a hosted 6-view dark-mode WebUI, AcuRite & Weather-Display (`grahamsbeach.htm`) inspired interactive SVG hardware gauges, US National Weather Service (`api.weather.gov`) official corroboration & animated Doppler radar, long-term seasonal/climatology analytics, hardened first-run encrypted Admin Password security, and turnkey **TrueNAS SCALE & Cloudflare Tunnel (`cloudflared`)** Docker deployment.

It captures raw ASK/OOK radio bursts from in-range 433.92 MHz weather stations via an RTL-SDR dongle and `rtl_433 -M time:utc -M level`, filters out non-meteorological RF chatter and kitchen/appliance probes, deduplicates redundant multi-frame RF bursts using a sliding window, normalizes all measurements into standardized SI units (with computed imperial, barometric `inHg`, dew point, apparent temperature, and 16-point compass properties), and auto-registers discovered stations in a persistent SQLite device registry.

---

## Key Features (`v0.0.32`)

- **Interactive Weather Calendar & High-Contrast Mini-Dashboard (`v0.0.19`):**
  - **High-Contrast Calendar Day Cells:** Dark slate cells (`#131c31`) with bright white day numbers, glowing data source indicators (`● Local`, `● Archive`, `◌ Est. XX%`), high-contrast High/Low temperatures (`#fbbf24` / `#38bdf8`), and centered 42px animated SVG weather icons.
  - **Consecutive Weather Streak Ribbons:** Spanning combo ribbons cleanly housed in dedicated 22px tracks grouping multi-day weather regimes (`Sunny Streak`, `Wet Pattern`, `Fair & Mild`).
  - **6-Card Mini-Dashboard Day Detail Modal:** Interactive popover modal featuring 6 confined cards with rich visualizers (thermal gradient range bar, semicircle humidity arc SVG, horizontal barometric pressure scale, compass rose SVG with direction needle, rain level & lightning gauge, and UV spectrum & degree days).
- **Ultra-Clean Sticky Header & Interactive Status Popover (`v0.0.15`):**
  - **Zero Mobile Overcrowding:** Streamlined the sticky 56px top bar down to strictly 2 sides and 4 elements (Hamburger `☰`, Active View Title, `[● Status ▾]` popover button, and `↻ Sync`).
  - **Interactive Status Popover Card:** Tap `[● Status ▾]` to reveal a floating dark-slate system summary card displaying live SDR stream status, active sensor ratio, barometric pressure, inline Admin Lock/Unlock controls, packets logged, WAL database size, system uptime, and build/version info.
  - **Slide-Out Drawer Branding:** Moved station mark (`SS`) and version tag directly inside the slide-out Hamburger Drawer header.
- **Climatology Layout Overhaul & Mobile Table Scrolling (`v0.0.15`):**
  - **Single-Line Learning Engine Banner:** Eliminated redundant duplicate headers in the active telemetry learning banner with a unified title row and metric summary row.
  - **Flexible Anomaly Pill:** Moved the 7-day vs 30-day anomaly pill onto its own responsive flex row.
  - **Horizontally Scrollable Records Table:** Wrapped historical extremes (Today, 7d, Month, Year, All-Time) in a touch-friendly overflow container with non-wrapping cells.
- **Cleaned NWS Baseline Strip & 24h Trajectory Header (`v0.0.15`):**
  - Streamlined NWS corroboration strip (`NWS Official (ASOS): 53.6°F | Δ -0.1°F • Partly Cloudy`), auto-hiding empty WFO badges and aligning the radar link cleanly.
  - Formatted the 24-hour Hero Sparkline header so High/Low range pills never awkwardly line-wrap on mobile viewports.
- **Crisp Clutter-Free Mobile Charts & Non-Negative Rainfall Axis (`v0.0.14`):**
  - **Zero Point Clutter:** Eliminated caterpillar overlapping dots (`pointRadius: 0` on mobile, `1` on desktop) with responsive hover snaps (`pointHoverRadius: 5`, `pointHitRadius: 16`), silky cubic curves, and dedicated `260px` mobile wrapper height.
  - **Flat X-Axis Timestamps & Non-Negative Rain:** Clamped X-axis ticks to 6 on mobile with `maxRotation: 0` (no diagonal overlap), and strictly enforced non-negative rainfall (`min: 0`, `suggestedMax: 0.25 in`).
- **High-Density Side-by-Side SVG Gauges & 2x2 Stat Grids (`v0.0.14`):**
  - Side-by-side grid layouts across all 4 primary gauge cards (`Wind`, `Barometer`, `Rain`, `Humidity & UV`) pairing the visual SVG gauge on the left with a compact 2x2 stat matrix on the right on both mobile and desktop.
  - Eliminated redundant filler subtitles and trimmed nowcast text to 1 concise sentence.
  - Moved High/Low extremes to the sparkline header bar, leaving the 24-hour SVG trajectory canvas completely uncluttered.
- **High-Definition Live Doppler Radar, NOAA WMS & 1-Click Windy Embed (`v0.0.13`):**
  - **Zero Watermarks & Zero API Keys:** Swapped Carto/RainViewer watermarked basemaps for standard OpenStreetMap tiles styled dynamically into dark mode (`.dark-osm-tiles`), with automatic tile-error fallback to Esri World Dark Gray.
  - **Official NOAA WMS Integration:** High-resolution CONUS Base Reflectivity (`conus_bref_qcd`) directly from NOAA OpenGeo NCEP servers.
  - **1-Click Windy Live Doppler Radar Embed:** One-click toggle between Leaflet NOAA WMS and dark-mode interactive Windy Doppler radar centered on station coordinates.
- **Mobile Responsive Layout & Dynamic Cache-Busting (`v0.0.13`):**
  - Automatic `asset_version` query parameter injection and HTTP `Cache-Control: no-store` headers to eliminate mobile/Cloudflare caching issues.
  - Full mobile screen scaling (`<= 640px` & `<= 520px`) with zero horizontal overflow, wrapped header badges, fluid hero typography, 2-column quick grid, and vertically stacked gauge cards.
  - Localized 12-hour timestamps (`7:53 PM`) for daily high/low temperature pills and robust dual-trace 24-hour area sparklines.
- **Enlarged Split Hero Condition Card & Dual-Trace 24h Area Sparklines:**
  - Full-width split hero view featuring enlarged 115px weather SVGs, 4.2rem primary temperature typography, high/low extreme pills, and trend arrows.
  - 4-Box Hyperlocal Quick Grid (Feels Like, Dew Point Spread, Today's Range, Ensemble Spread) and dual-trace 24h temperature + dew point area trend chart.
- **AcuRite & Weather-Display (`grahamsbeach.htm`) Interactive SVG Gauges (`v0.0.12`):**
  - Roomy 2x2 grid layout with enlarged 175px radial AcuRite wind dial, rotating needle, center console readout, dynamic glow rings, and rolling 10-minute average / peak burst flanks.
  - 165px tall vertical barometric pressure mercury tower (`29.20–30.50 inHg`) with dynamic pointer, trend arrows, and sea-level pressure calibration.
  - 165px graduated rain cylinder gauge with meniscus level and AcuRite rain records table (`Rate`, `Week`, `Month`, `Year`).
  - High-visibility semi-circular humidity comfort arc and solar/UV intensity gauges.
- **Hardened Security Lockdown, Encrypted First-Run Password & Settings Tab (`v0.0.12`):**
  - **Encrypted `system_settings` Table:** Stores all instance-specific configuration (`admin_password_hash`, 32-byte `secret_signing_key`, `inactivity_timeout_minutes`, `station_name`, coordinates, units, and NWS grid metadata) in `data/skyscry.db` with a **Zero-PII Guarantee** (zero secrets or personal coordinates in git-tracked files).
  - **First-Run Password Setup & Auto-Lock:** Prompts to create an encrypted Admin Password (`scrypt` / `PBKDF2-HMAC-SHA256`) on first launch, protects all mutating endpoints with `@require_admin_auth`, enforces an IP brute-force rate limiter (`5 attempts / 5m`), dual Cookie + `X-SkyScry-Token` authentication, and auto-locks admin sessions after configurable inactivity (`5m–60m`).
  - **Dedicated `6. Settings 🔒` View:** Configure Security & Access Control, Station Coordinates & NWS Baseline, Ingestion & Data Hygiene (`VACUUM`), and Display Units (`°F`/`°C`, `mph`/`m/s`/`knots`, `inHg`/`hPa`, `in`/`mm`).
- **National Weather Service (`api.weather.gov`) Corroboration & Live Radar (`v0.0.12`):**
  - Resolves `/points/{lat},{lon}` to pull nearest official NWS station observations, active weather alerts (`/alerts/active`), and the 7-day official gridpoint forecast with 15-minute caching.
  - Includes an at-a-glance **Mini Live Radar Card** on the Overview Dashboard and a dedicated **`5. Radar & NWS Forecast`** view with animated RainViewer / NOAA NEXRAD Doppler radar, timeline scrubber, dBZ reflectivity scale, and hyperlocal microclimate delta table.
- **Upgraded Nowcast & Long-Term Seasonal / Climatology Engine (`v0.0.12`):**
  - Multi-factor 1–24h nowcast combining 3h/6h barometric velocity, dew point depression, wind backing/veering, solar attenuation, and NWS PoP, plus `daily_summaries` rollups, HDD/CDD degree days, wet/dry streaks, and a Weather-Display Records Summary Table (`Today`, `Week`, `Month`, `Year`, `All-Time`).
  - Multi-factor 1–24h nowcast combining 3h/6h barometric velocity, dew point depression, wind backing/veering, solar attenuation, and NWS PoP, plus `daily_summaries` rollups, HDD/CDD degree days, wet/dry streaks, and a Weather-Display Records Summary Table (`Today`, `Week`, `Month`, `Year`, `All-Time`).

- **Opt-In Sensor Trust & TPMS / Soil / Barometer Gate (`v0.0.8+`):**
  - **Opt-In Trust Model:** All newly discovered 433.92 MHz sensors default strictly to `location_type = 'ignore'` and `is_trusted = 0`. Only sensors explicitly configured by the user as **Outdoor** or **Indoor** in the WebUI participate in dashboard weather synthesis and historical charts.
  - **Vehicle TPMS & Soil Probe Hard-Drop:** Passing cars (`Ford`, `Citroen`, `Renault`, `Schrader`, `"type": "TPMS"`, `pressure_kPa`/`pressure_PSI`) and soil/appliance probes (`RainPoint`, `EcoWitt-WH51`, `Acurite-986`, `Maverick`, `ThermoPro`, `Inkbird`) are dropped at the normalizer gate and purged from SQLite.
  - **Barometric Pressure Plausibility Clamp (`870–1085 hPa`):** Atmospheric pressure is strictly clamped to `870.0–1085.0 hPa` (`25.69–32.04 inHg`) so tire inflation pressures (`~213 kPa`) can never skew the barometer.
  - **Multi-Checkbox Chart Stream Filter:** Mix and match `Outdoor Sensors (Trusted)`, `Indoor Sensors (Trusted)`, `Reference / Regional Weather Baseline`, and individual station checkboxes on the Historical Charts view.
- **Turnkey TrueNAS SCALE, GHCR & Cloudflare Tunnel Stack (`v0.0.7+`):**
  - **Automated GHCR Builds:** GitHub Actions (`.github/workflows/docker-publish.yml`) automatically builds and publishes `ghcr.io/cave404/skyscry:latest` on every push to `main`.
  - **Smart Container Entrypoint (`docker/entrypoint.sh`):** Automatically unloads conflicting Linux DVB-T kernel modules (`dvb_usb_rtl28xxu`, `rtl2832`), verifies `/dev/bus/usb` passthrough, and pulls latest GitHub updates on container restart when `AUTO_UPDATE_ON_START=true`.
  - **TrueNAS SCALE 1-Click YAML (`docker-compose.truenas.yml`):** Copy-pasteable stack bundling `skyscry`, `cloudflared` (zero-port HTTPS tunnel to `http://skyscry:8080`), and `watchtower` (hourly automated container image updates). See **[`docs/TRUENAS_SETUP.md`](docs/TRUENAS_SETUP.md)**.
  - **Reverse Proxy & Public Security (`ProxyFix` + `ADMIN_PIN`):** Wrapped with Werkzeug `ProxyFix` for Cloudflare HTTPS headers and supports optional `ADMIN_PIN` / `READ_ONLY_PUBLIC` protection so external visitors can view live dashboards and charts while write/maintenance endpoints require your PIN.
- **Automated Startup Bootstrapper & Self-Updater (`src/updater.py`):**
  - **Git Self-Update:** Checks `origin/main` on startup (8s timeout) and pulls updates via `git pull --rebase --autostash` while keeping `data/skyscry.db`, `config/config.yaml`, and local binaries untouched.
  - **Python Dependency Auto-Upgrader:** Verifies and upgrades `pip` and `requirements.txt` automatically on first launch, after a Git update, or every 24 hours (`data/.last_dep_check`), seamlessly re-executing the Python process (`os.execv`) when updated.
  - **Windows `rtl_433` Binary Auto-Downloader:** Checks the official [`merbanan/rtl_433` GitHub Releases API](https://github.com/merbanan/rtl_433/releases/latest) on Windows and automatically downloads/extracts `rtl_433-rtlsdr.exe` and companion DLLs (`rtlsdr.dll`, `libusb-1.0.dll`, etc.) into `./bin/` if missing or outdated.
  - **Double-Click Windows Launchers & Desktop Shortcuts:** Includes `Start_SkyScry.bat`, `Launch_SkyScry_Background.vbs` (windowless background service), `Stop_SkyScry.bat`, and `Create_Desktop_Shortcut.bat`.
- **Windows WinUSB Stall Recovery & Non-Blocking Queued I/O:** Dedicated `stdout` and `stderr` daemon threads push `rtl_433` frames into a non-blocking `queue.Queue()` so OS pipes never stall bulk USB transfers (`cb transfer status: 4` / `3221225477`). Automatically suppresses repetitive `-9` (`LIBUSB_ERROR_PIPE`) register write spam, cleans up zombie `rtl_433` processes, and applies progressive USB recovery delays.
- **Automated Outlier & Appliance Expunger:**
  - Automatically classifies fridge/freezer and meat-probe protocols (`Acurite-986`, `Maverick-ET73x`, `ThermoPro`, `Inkbird`, etc.) as `ignore` / untrusted.
  - Rejects bit-flip RF corruption using hard physical bounds (`-25°F` to `130°F`), rate-of-change spike detection (`>15°F` jump in `<10m`), and dynamic outdoor ensemble median consensus checks (`±22°F`).
  - Automatically purges skewed historical rows and 0-packet phantom sensors on startup (`db.expunge_skewed_history()`) or via `POST /api/maintenance/purge-outliers`.
- **Barometric Pressure (`inHg` & `hPa`) + 48h Regional Backfill:** Integrates no-key Open-Meteo surface/MSL pressure and 48-hour historical weather backfill (`src/ingest/reference_weather.py`) to display live Barometric Pressure (`inHg` & `hPa`), 3-hour pressure tendencies, and a 24-hour AcuRite/Zambretti-style barometric forecast.
- **4-View WebUI Architecture (`0.0.0.0:8080`):**
  1. **Overview Dashboard:** High-clarity CSS-animated SVG weather condition icons, interactive **Animated SVG Wind Compass Rose**, Barometric Pressure (`inHg` & `hPa`) + 24h forecast, Outdoor/Indoor ensemble summaries, and 1h/3h atmospheric deltas.
  2. **Historical Charts:** 4 stacked cross-analysis charts (Temperature, Relative Humidity, Wind Speed & Peak Gusts, Rainfall Accumulation) with time-bucketed smoothing and a dashed **Outdoor Average (Ensemble Mean)** line.
  3. **Sensors Manager:** Deterministic alphabetical card ordering with in-place DOM patching, RF signal bars (`RSSI`, `SNR`, `Noise`), battery lifecycle tracking, expunged outlier badges, and per-sensor **Granular Metric Masking (`hidden_metrics`)**.
  4. **SQLite Log Explorer:** Raw packet table inspector with sensor/time filters and one-click CSV/JSON export.

---

## Repository Structure

```text
SkyScry/
├── .github/workflows/
│   └── docker-publish.yml          # GitHub Actions CI/CD to build & push ghcr.io/cave404/skyscry:latest
├── .env.example                    # Environment variables template (TZ, paths, Cloudflare token, ADMIN_PIN)
├── .gitignore                      # Excludes *.db, *.db-wal, *.db-shm, *.exe, *.dll, bin/, data/, config/config.yaml
├── Start_SkyScry.bat               # Double-click Windows launcher (venv, git pull, pip upgrade, WebUI launch)
├── Launch_SkyScry_Background.vbs   # Silent windowless Windows background launcher
├── Stop_SkyScry.bat                # Cleanly terminates SkyScry daemon & rtl_433 processes
├── Create_Desktop_Shortcut.bat     # Generates Desktop shortcuts for normal & silent background launch
├── Dockerfile                      # Python 3.12-slim image with rtl-433, usbutils, kmod, & smart entrypoint
├── docker/
│   └── entrypoint.sh               # Pre-flight DVB-T kernel detach, USB check, & Git auto-update on start
├── docker-compose.yml              # Local Docker deployment with USB passthrough & persistent volumes
├── docker-compose.truenas.yml      # Turnkey TrueNAS SCALE stack (skyscry + cloudflared + watchtower)
├── docs/
│   └── TRUENAS_SETUP.md            # Step-by-step TrueNAS SCALE & Cloudflare Tunnel setup guide
├── README.md                       # Documentation, hardware setup, REST API reference, and Docker guide
├── requirements.txt                # Runtime dependencies (PyYAML, Flask, waitress, paho-mqtt)
├── config/
│   └── config.example.yaml         # Documented configuration template (no hardcoded sensor names)
├── src/
│   ├── __init__.py                 # Package version (__version__ = "0.0.19")
│   ├── security.py                 # Encrypted password hashing, brute-force limiter, & session auto-lock
│   ├── updater.py                  # Stdlib-only startup bootstrapper, Git/pip updater, & rtl_433 downloader
│   ├── config.py                   # YAML/env loader & cross-platform rtl_433 binary resolver
│   ├── models.py                   # NormalizedReading dataclass with SI & imperial properties
│   ├── db.py                       # SQLite WAL engine (`sensors`, `sensor_readings`, `system_settings`, `daily_summaries`)
│   ├── analytics/
│   │   ├── __init__.py
│   │   └── climatology.py          # Multi-factor 24h nowcast & seasonal/climatology insights engine
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── sdr_stream.py           # Non-blocking queued subprocess lifecycle manager for rtl_433
│   │   ├── reference_weather.py    # Open-Meteo barometric pressure & 48h regional weather backfill
│   │   └── nws_service.py          # Official US National Weather Service (`api.weather.gov`) client & cache
│   ├── pipeline/
│   │   ├── __init__.py
│   │   └── normalizer.py           # JSON validation, unit conversion, outlier filter, & burst deduplication
│   └── web/
│       ├── __init__.py
│       ├── app.py                  # Flask app factory, ProxyFix, ADMIN_PIN auth, & REST API endpoints
│       ├── templates/
│       │   └── index.html          # Single-page 4-view dark-mode WebUI dashboard
│       └── static/
│           ├── css/
│           │   └── style.css
│           └── js/
│               └── app.js
└── run.py                          # Concurrent daemon entrypoint (bootstrapper + SDR thread + HTTP WebUI)
```

---

## TrueNAS SCALE & Cloudflare Tunnel Deployment

For a complete step-by-step guide to running SkyScry 24/7 on **TrueNAS SCALE** with **NooElec RTL-SDR USB passthrough**, **Cloudflare Tunnel (`cloudflared`)** HTTPS access, and **Watchtower** auto-updates, see:

- **[TrueNAS SCALE & Cloudflare Tunnel Setup Guide (`docs/TRUENAS_SETUP.md`)](docs/TRUENAS_SETUP.md)**
- **[`docker-compose.truenas.yml`](docker-compose.truenas.yml)**

```bash
# Or start locally with Docker Compose:
docker compose up -d --build
docker compose logs -f skyscry
```

---

## Quick Start (Windows One-Click Launchers)

On Windows, you do not need to manually open Command Prompt or PowerShell:

1. **Create Desktop Shortcuts (One-Time):** Double-click **`Create_Desktop_Shortcut.bat`** to create two shortcuts on your Windows Desktop:
   - **SkyScry Weather**: Opens a status console, updates Git + Python packages + `rtl_433`, starts the daemon, and opens `http://127.0.0.1:8080`.
   - **SkyScry Weather (Silent Background)**: Runs the daemon completely silently in the background with no console window and opens the WebUI.
2. **Stop Background Service:** Double-click **`Stop_SkyScry.bat`** at any time to cleanly terminate the background Python daemon and `rtl_433-rtlsdr.exe` and release the RTL-SDR USB port.

---

## Manual CLI Installation & Usage

```bash
git clone https://github.com/Cave404/SkyScry.git
cd SkyScry
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1 | Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python run.py --open-browser
```

---

## REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/status` | Returns daemon version (`0.0.19`), Git commit hash, `rtl_433` version, uptime, SDR stream status, SQLite WAL size in MB, `auth` lock status, active/total sensors, and total stored packets. |
| `GET/POST` | `/api/auth/*` | First-run admin password setup (`/api/auth/setup`), session unlock (`/api/auth/unlock`), manual lock (`/api/auth/lock`), and password rotation (`/api/auth/change-password`). |
| `GET/POST` | `/api/settings` | Reads or updates persistent instance settings in SQLite (`system_settings` table). |
| `GET` | `/api/nws` | Returns official US National Weather Service (`api.weather.gov`) observations, active alerts, 7-day gridpoint forecast, and hyperlocal corroboration deltas. |
| `GET` | `/api/climatology` | Returns 7d/30d climatology rollups, HDD/CDD degree days, wet/dry streaks, seasonal trajectory narrative, and Weather-Display records table. |
| `GET` | `/api/update-status` | Returns Git commit/branch status, `rtl_433` binary version & path, and last dependency check timestamp. |
| `POST` | `/api/trigger-update` | Manually triggers Git pull, `pip install --upgrade -r requirements.txt`, and `rtl_433` Windows binary check from the WebUI (protected by `ADMIN_PIN` if set). |
| `GET` | `/api/overview` | Returns synthesized outdoor/indoor ensemble metrics, barometric pressure (`inHg`/`hPa`), 24h barometric forecast, animated weather condition code, and 1h/3h trends. |
| `GET` | `/api/charts` | Returns time-bucketed, smoothed chart series per sensor plus Outdoor Ensemble Average lines (`?hours=24&bucket_minutes=auto&location=outdoor`). |
| `GET` | `/api/sensors` | Lists all auto-discovered sensors sorted deterministically by display name with RF signal levels, battery lifecycle stats, and `hidden_metrics`. |
| `PATCH` | `/api/sensors/<sensor_key>` | Updates `alias`, `location_type`, `is_trusted`, `hidden_metrics`, `mark_battery_replaced`, and `purge_sensor_outliers` (protected by `ADMIN_PIN` if set). |
| `POST` | `/api/maintenance/purge-outliers` | Purges corrupted historical bit-flip outliers, fridge/appliance readings, and 0-packet phantom sensors from SQLite (protected by `ADMIN_PIN` if set). |
| `POST` | `/api/maintenance/sync-reference` | Pulls past 48h regional barometric pressure (`inHg`/`hPa`) and weather history from Open-Meteo (protected by `ADMIN_PIN` if set). |
| `GET` | `/api/readings` | Queries historical time-series telemetry (`?sensor_key=Model_ID&hours=24&limit=250&format=csv`). |

---

## License

Released under the [MIT License](LICENSE).

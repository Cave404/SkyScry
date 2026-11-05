# SkyScry

![Version](https://img.shields.io/badge/version-0.0.6-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

**SkyScry** is an open-source, cross-platform **433.92 MHz microclimate telemetry and weather ingestion daemon** with a hosted 4-view dark-mode WebUI, automated startup bootstrapper & GitHub self-updater, barometric pressure (`inHg`/`hPa`) & 48-hour regional backfill engine, automated RF outlier expunger, REST API, and SQLite Write-Ahead Logging (`WAL`) storage engine.

It captures raw ASK/OOK radio bursts from in-range 433.92 MHz weather stations via an RTL-SDR dongle and `rtl_433 -M time:utc -M level`, filters out non-meteorological RF chatter and kitchen/appliance probes, deduplicates redundant multi-frame RF bursts using a sliding window, normalizes all measurements into standardized SI units (with computed imperial, barometric `inHg`, dew point, apparent temperature, and 16-point compass properties), and auto-registers discovered stations in a persistent SQLite device registry.

---

## Key Features (`v0.0.6`)

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
- **Sliding-Window Burst Deduplication:** Suppresses redundant 433 MHz burst frames (default `4.0s` window) keyed on `(model, sensor_id, msg_type)` with periodic stale-key pruning.
- **Cross-Platform & Container-Ready:** Runs natively on Windows, Linux, and macOS, or in Docker / TrueNAS SCALE with direct USB hardware passthrough (`/dev/bus/usb`).

---

## Repository Structure

```text
SkyScry/
├── .gitignore                      # Excludes *.db, *.db-wal, *.db-shm, *.exe, *.dll, bin/, data/, config/config.yaml
├── Start_SkyScry.bat               # Double-click Windows launcher (venv, git pull, pip upgrade, WebUI launch)
├── Launch_SkyScry_Background.vbs   # Silent windowless Windows background launcher
├── Stop_SkyScry.bat                # Cleanly terminates SkyScry daemon & rtl_433 processes
├── Create_Desktop_Shortcut.bat     # Generates Desktop shortcuts for normal & silent background launch
├── Dockerfile                      # Python 3.12-slim image with rtl-433 and libusb-1.0-0
├── docker-compose.yml              # NAS / Docker deployment with USB passthrough & persistent volumes
├── README.md                       # Documentation, hardware setup, REST API reference, and Docker guide
├── requirements.txt                # Runtime dependencies (PyYAML, Flask, waitress, paho-mqtt)
├── config/
│   └── config.example.yaml         # Documented configuration template (no hardcoded sensor names)
├── src/
│   ├── __init__.py                 # Package version (__version__ = "0.0.6")
│   ├── updater.py                  # Stdlib-only startup bootstrapper, Git/pip updater, & rtl_433 downloader
│   ├── config.py                   # YAML loader & cross-platform rtl_433 binary resolver
│   ├── models.py                   # NormalizedReading dataclass with SI & imperial properties
│   ├── db.py                       # SQLite WAL engine (`sensors`, `sensor_readings`, `reference_weather`)
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── sdr_stream.py           # Non-blocking queued subprocess lifecycle manager for rtl_433
│   │   └── reference_weather.py    # Open-Meteo barometric pressure & 48h regional weather backfill
│   ├── pipeline/
│   │   ├── __init__.py
│   │   └── normalizer.py           # JSON validation, unit conversion, outlier filter, & burst deduplication
│   └── web/
│       ├── __init__.py
│       ├── app.py                  # Flask application factory & REST API endpoints
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

## Hardware Prerequisites

You need an **RTL2832U / R820T2 RTL-SDR USB dongle** and the [`rtl_433`](https://github.com/merbanan/rtl_433) binary (automatically downloaded on Windows by `src/updater.py` or installed automatically when using Docker).

### Windows Setup (Zadig WinUSB Driver)
1. Plug in your RTL-SDR USB dongle.
2. Open **[Zadig](https://zadig.akeo.ie/)**, select **Options -> List All Devices**, and select **Bulk-In, Interface (Interface 0)** (`0BDA:2838`).
3. Install the **WinUSB** driver.
4. Double-click **`Start_SkyScry.bat`** (or run `Create_Desktop_Shortcut.bat` to place shortcuts on your Desktop). If `rtl_433-rtlsdr.exe` is not present locally, SkyScry will automatically download and extract the latest official Windows build from GitHub into `./bin/`.

### Linux / NAS Host Setup (Kernel DVB Blacklist)
Before running natively or passing the USB dongle into Docker on Linux / TrueNAS SCALE, ensure the host kernel does not claim the RTL-SDR as a DVB-T TV tuner. Create `/etc/modprobe.d/blacklist-rtlsdr.conf`:

```text
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
```

Then unload the module if currently active:
```bash
sudo modprobe -r dvb_usb_rtl28xxu
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

1. **Clone the repository and create a virtual environment:**
   ```bash
   git clone https://github.com/Cave404/SkyScry.git
   cd SkyScry
   python -m venv .venv
   ```

2. **Activate the virtual environment and install dependencies:**
   - **Windows (PowerShell):**
     ```powershell
     .\.venv\Scripts\Activate.ps1
     pip install -r requirements.txt
     ```
   - **Linux / macOS:**
     ```bash
     source .venv/bin/activate
     pip install -r requirements.txt
     ```

3. **Optional Configuration:**
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```

4. **Start the Daemon + WebUI:**
   ```bash
   python run.py --open-browser
   ```
   Open **`http://localhost:8080`** in your browser to view the live sensor grid, configure sensor aliases/locations, and explore historical telemetry.

### CLI Options

```bash
# Start SDR ingestion daemon + WebUI on default port 8080 (with startup auto-updater)
python run.py

# Automatically open http://127.0.0.1:8080 in the default browser once ready
python run.py --open-browser

# Skip startup Git / pip / rtl_433 binary update checks (offline / development mode)
python run.py --no-update

# Run on a custom HTTP port or bind address
python run.py --host 0.0.0.0 --port 9090

# Run headless (SDR ingestion to SQLite without starting the WebUI server)
python run.py --no-web

# Dry-run mode (transient in-memory DB; does not write to disk)
python run.py --dry-run
```

---

## Docker & NAS Deployment (TrueNAS SCALE / Linux)

SkyScry ships with a production `Dockerfile` (`python:3.12-slim` + `rtl-433` + `libusb-1.0-0`) and `docker-compose.yml`.

```bash
# Build and start in detached mode
docker compose up -d --build

# View live daemon logs
docker compose logs -f skyscry
```

- **USB Hardware Passthrough:** Maps `/dev/bus/usb:/dev/bus/usb` so `rtl_433` inside the container can claim the RTL-SDR USB receiver directly.
- **Persistent Volumes:** Mounts `./data:/app/data` (SQLite WAL database + sensor registry) and `./config:/app/config` so all historical readings and user-configured sensor aliases survive container rebuilds.
- **WebUI Access:** Published on `http://<host-ip>:8080`.

---

## REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/status` | Returns daemon version (`0.0.6`), Git commit hash, `rtl_433` version, uptime, SDR stream status, SQLite WAL size in MB, active/total sensors, and total stored packets. |
| `GET` | `/api/update-status` | Returns Git commit/branch status, `rtl_433` binary version & path, and last dependency check timestamp. |
| `POST` | `/api/trigger-update` | Manually triggers Git pull, `pip install --upgrade -r requirements.txt`, and `rtl_433` Windows binary check from the WebUI. |
| `GET` | `/api/overview` | Returns synthesized outdoor/indoor ensemble metrics, barometric pressure (`inHg`/`hPa`), 24h barometric forecast, animated weather condition code, and 1h/3h trends. |
| `GET` | `/api/charts` | Returns time-bucketed, smoothed chart series per sensor plus Outdoor Ensemble Average lines (`?hours=24&bucket_minutes=auto&location=outdoor`). |
| `GET` | `/api/sensors` | Lists all auto-discovered sensors sorted deterministically by display name with RF signal levels, battery lifecycle stats, and `hidden_metrics`. |
| `PATCH` | `/api/sensors/<sensor_key>` | Updates `alias`, `location_type`, `is_trusted`, `hidden_metrics`, `mark_battery_replaced`, and `purge_sensor_outliers`. |
| `POST` | `/api/maintenance/purge-outliers` | Purges corrupted historical bit-flip outliers, fridge/appliance readings, and 0-packet phantom sensors from SQLite. |
| `POST` | `/api/maintenance/sync-reference` | Pulls past 48h regional barometric pressure (`inHg`/`hPa`) and weather history from Open-Meteo. |
| `GET` | `/api/readings` | Queries historical time-series telemetry (`?sensor_key=Model_ID&hours=24&limit=250&format=csv`). |

---

## License

Released under the [MIT License](LICENSE).

# SkyScry

![Version](https://img.shields.io/badge/version-0.1.1-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

**SkyScry** is an open-source, cross-platform **433.92 MHz microclimate telemetry and weather ingestion daemon** with a hosted dark-mode WebUI, REST API, and SQLite Write-Ahead Logging (`WAL`) storage engine.

It captures raw ASK/OOK radio bursts from in-range 433.92 MHz weather stations via an RTL-SDR dongle and `rtl_433`, filters out non-meteorological RF chatter (such as tire pressure sensors and keyfobs), deduplicates redundant multi-frame RF bursts using a sliding window, normalizes all measurements into standardized SI units (with computed imperial and 16-point compass properties), and auto-registers discovered stations in a persistent SQLite device registry.

---

## Key Features (`v0.1.1`)

- **Zero Hardcoded Station Assumptions:** Discovered 433.92 MHz sensors are automatically registered in the SQLite `sensors` table upon first packet reception and default strictly to `{model} #{sensor_id}` (e.g., `Acurite-Tower #11873`) until assigned a custom alias in the WebUI.
- **Hosted Dark-Mode WebUI & REST API (`0.0.0.0:8080`):**
  - **System Status Header:** Real-time SDR daemon status, active sensor count, total logged packets, SQLite WAL database size, and daemon uptime.
  - **Live Sensor Grid:** Responsive cards for each discovered station displaying temperature (`°F` & `°C`), relative humidity, wind speed/gust/cardinal direction, cumulative rainfall, UV/light, battery health badge (`OK` / `LOW`), and time since last packet.
  - **Inline Sensor Management Modal:** Assign or reset custom station aliases, classify location types (`indoor`, `outdoor`, `ignore`, `unassigned`), and toggle `is_trusted` for local weather modeling directly via `PATCH /api/sensors/<sensor_key>`.
  - **Historical Telemetry & DB Explorer:** Interactive Chart.js time-series visualization and tabular log explorer filterable by sensor, metric, and time window (`1h` to `7d`).
- **Sliding-Window Burst Deduplication:** Suppresses redundant 433 MHz burst frames (default `4.0s` window) keyed on `(model, sensor_id, msg_type)` with periodic stale-key pruning.
- **Cross-Platform & Container-Ready:** Runs natively on Windows, Linux, and macOS, or in Docker / TrueNAS SCALE with direct USB hardware passthrough (`/dev/bus/usb`).

---

## Repository Structure

```text
SkyScry/
├── .gitignore               # Excludes *.db, *.db-wal, *.db-shm, *.exe, *.dll, bin/, data/, config/config.yaml
├── Dockerfile               # Python 3.12-slim image with rtl-433 and libusb-1.0-0
├── docker-compose.yml       # NAS / Docker deployment with USB passthrough & persistent volumes
├── README.md                # Documentation, hardware setup, REST API reference, and Docker guide
├── requirements.txt         # Runtime dependencies (PyYAML, Flask, waitress, paho-mqtt)
├── config/
│   └── config.example.yaml  # Documented configuration template (no hardcoded sensor names)
├── src/
│   ├── __init__.py          # Package version (__version__ = "0.1.1")
│   ├── config.py            # YAML loader & cross-platform rtl_433 binary resolver
│   ├── models.py            # NormalizedReading dataclass with SI & imperial properties
│   ├── db.py                # SQLite WAL engine (`sensors` registry + `sensor_readings` time-series)
│   ├── ingest/
│   │   ├── __init__.py
│   │   └── sdr_stream.py    # Non-blocking subprocess lifecycle manager for rtl_433
│   ├── pipeline/
│   │   ├── __init__.py
│   │   └── normalizer.py    # JSON validation, unit conversion, & burst deduplication
│   └── web/
│       ├── __init__.py
│       ├── app.py           # Flask application factory & REST API endpoints
│       ├── templates/
│       │   └── index.html   # Single-page dark-mode WebUI dashboard
│       └── static/
│           ├── css/
│           │   └── style.css
│           └── js/
│               └── app.js
└── run.py                   # Concurrent daemon entrypoint (SDR ingestion thread + HTTP WebUI)
```

---

## Hardware Prerequisites

You need an **RTL2832U / R820T2 RTL-SDR USB dongle** and the [`rtl_433`](https://github.com/merbanan/rtl_433) binary (installed automatically when using Docker).

### Windows Setup (Zadig WinUSB Driver)
1. Plug in your RTL-SDR USB dongle.
2. Open **[Zadig](https://zadig.akeo.ie/)**, select **Options -> List All Devices**, and select **Bulk-In, Interface (Interface 0)** (`0BDA:2838`).
3. Install the **WinUSB** driver.
4. Place `rtl_433-rtlsdr.exe` (or `rtl_433.exe`) and its companion `.dll` files on your system `PATH` or inside `./bin/` (git-ignored).

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

## Local Installation & Usage

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
   python run.py
   ```
   Open **`http://localhost:8080`** in your browser to view the live sensor grid, configure sensor aliases/locations, and explore historical telemetry.

### CLI Options

```bash
# Start SDR ingestion daemon + WebUI on default port 8080
python run.py

# Run on a custom HTTP port or bind address
python run.py --host 0.0.0.0 --port 9090

# Run headless (SDR ingestion to SQLite without starting the WebUI server)
python run.py --no-web

# Dry-run mode (transient in-memory DB; does not write to disk)
python run.py --dry-run
```

### Example Console Output

```text
2026-11-04 23:20:10 | INFO    | SkyScry v0.1.1 starting up...
2026-11-04 23:20:10 | INFO    | SQLite WAL storage active at data/skyscry.db
2026-11-04 23:20:10 | INFO    | Hosted WebUI & REST API listening on http://0.0.0.0:8080
2026-11-04 23:20:10 | INFO    | Starting RTL-SDR stream: rtl_433 -f 433.92M -d 0 -F json
2026-11-04 23:20:12 | INFO    | Acurite-Tower #8364 (CH A)                 -> Temp: 71.2F (21.8C) | RH: 44% | Batt: OK
2026-11-04 23:20:16 | INFO    | Cotech-367959 #218                         -> Temp: 54.0F (12.2C) | RH: 68% | Wind: 6.5 mph (2.9 m/s) [Gust 10.1 mph] WSW (248 deg) | Rain: 1.25 in (31.8 mm) | UV: 1.0 | Light: 14200 lux | Batt: OK
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
| `GET` | `/api/status` | Returns daemon version, uptime, SDR stream status (`running` / `disconnected`), SQLite WAL size in MB, active/total sensors, and total stored packets. |
| `GET` | `/api/sensors` | Lists all auto-discovered sensors in the `sensors` registry with their latest telemetry reading and user metadata (`alias`, `location_type`, `is_trusted`). |
| `PATCH` | `/api/sensors/<sensor_key>` | Updates `alias` (string or empty to reset), `location_type` (`indoor`, `outdoor`, `ignore`, `unassigned`), and `is_trusted` (boolean) for `<sensor_key>`. |
| `GET` | `/api/readings` | Queries historical time-series telemetry (`?sensor_key=Model_ID&hours=24&limit=200`). |

---

## License

Released under the [MIT License](LICENSE).

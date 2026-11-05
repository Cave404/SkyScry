# SkyScry

![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

**SkyScry** is an open-source, cross-platform **433.92 MHz microclimate telemetry and weather ingestion daemon** powered by RTL-SDR and `rtl_433`.

It captures raw ASK/OOK radio bursts from heterogeneous consumer and prosumer weather stations, filters out non-meteorological RF chatter (such as tire pressure sensors and keyfobs), deduplicates redundant multi-frame RF bursts in real time, normalizes all metrics into standardized SI units (with computed imperial and 16-point compass properties), and persists observations into a high-concurrency SQLite Write-Ahead Logging (`WAL`) database.

---

## Key Features

- **Cross-Platform `rtl_433` Binary Resolution:** Dynamically locates `rtl_433` (Linux/macOS/Docker) or `rtl_433-rtlsdr.exe` / `rtl_433.exe` (Windows) via config, environment variables (`SKYSCRY_RTL433_BIN`), system `PATH`, or a local `./bin/` directory.
- **Heterogeneous Sensor Grid Support:** Built for mixed 433.92 MHz environments out-of-the-box:
  - **Acurite-Tower** (`temperature_C`, `humidity`, `battery_ok`, `channel`)
  - **Cotech-367959 / 7-in-1 Weather Mast** (`temperature_F` / `temperature_C`, `humidity`, `wind_avg_m_s`, `wind_max_m_s`, `wind_dir_deg`, `rain_mm`, `light_lux`, `uv`, `battery_ok`)
  - **Oregon-THGR810** (`temperature_C`, `humidity`, `battery_ok`, `channel`)
  - **Acurite-606TX** (`temperature_C`, `battery_ok`)
  - Any other `rtl_433`-compatible weather sensor emitting standard temperature, humidity, barometric pressure, wind, rain, UV, or light fields.
- **Sliding-Window Burst Deduplication:** 433 MHz transmitters typically broadcast 3 identical frames within 100–300 ms per burst. SkyScry deduplicates bursts using a configurable sliding time window (default `4.0s`) keyed on `(model, sensor_id, msg_type)` with automatic stale-key pruning.
- **Standardized SI Storage + Imperial Helpers:** Persists canonical SI measurements (`°C`, `m/s`, `mm`, `hPa`) while providing instant `@property` accessors for `temp_f`, `wind_avg_mph`, `wind_gust_mph`, `rain_cumulative_in`, and 16-point `wind_cardinal` directions (`N`, `NNE`, ..., `WSW`, `NNW`).
- **Concurrent SQLite WAL Engine:** Uses `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;` with composite indexes on `(sensor_id, timestamp)` and `(timestamp)` so downstream dashboards or exporters can query the database concurrently without locking ingestion.

---

## Repository Structure

```text
SkyScry/
├── .gitignore               # Excludes *.db, *.db-wal, *.db-shm, *.exe, *.dll, bin/, config/config.yaml
├── README.md                # Project documentation, hardware setup, and CLI usage
├── requirements.txt         # Minimal runtime dependencies (PyYAML, etc.)
├── config/
│   └── config.example.yaml  # Documented configuration template
├── src/
│   ├── __init__.py          # Package version (__version__ = "0.1.0")
│   ├── config.py            # YAML loader with safe defaults & cross-platform binary resolver
│   ├── models.py            # NormalizedReading dataclass with SI & imperial properties
│   ├── db.py                # SQLite WAL storage engine
│   ├── ingest/
│   │   ├── __init__.py
│   │   └── sdr_stream.py    # Non-blocking subprocess lifecycle manager for rtl_433
│   └── pipeline/
│       ├── __init__.py
│       └── normalizer.py    # JSON validation, unit conversion, & burst deduplication
└── run.py                   # Main CLI entrypoint
```

---

## Hardware Prerequisites

You need a standard **RTL2832U / R820T2 RTL-SDR USB dongle** (such as an RTL-SDR Blog V3/V4 or Nooelec NESDR) and the [`rtl_433`](https://github.com/merbanan/rtl_433) decoder binary.

### Windows Setup (Zadig WinUSB Driver)
1. Plug in your RTL-SDR USB dongle.
2. Download and open **[Zadig](https://zadig.akeo.ie/)**.
3. Select **Options -> List All Devices**.
4. Choose **Bulk-In, Interface (Interface 0)** (or `RTL2838UHIDIR`).
5. Ensure the USB ID is `0BDA:2838` (Interface 0) and install the **WinUSB** driver.
6. Download the latest Windows release of `rtl_433` and either add it to your `PATH` or drop `rtl_433-rtlsdr.exe` (and its companion `.dll` files) into `./bin/` (git-ignored).

### Linux Setup (udev Rules & Kernel Blacklist)
1. Install `rtl-sdr` and `rtl-433` via your package manager:
   ```bash
   sudo apt update && sudo apt install -y rtl-sdr rtl-433
   ```
2. Prevent the Linux kernel from claiming the dongle as a DVB-T TV tuner by creating `/etc/modprobe.d/blacklist-rtlsdr.conf`:
   ```text
   blacklist dvb_usb_rtl28xxu
   blacklist rtl2832
   blacklist rtl2830
   ```
3. Unload the module or reboot:
   ```bash
   sudo modprobe -r dvb_usb_rtl28xxu
   ```

---

## Installation & Quick Start

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

3. **Create your local configuration (optional):**
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```
   Edit `config/config.yaml` to customize your station aliases, deduplication window, or SQLite path. (If `config/config.yaml` does not exist, SkyScry runs out-of-the-box with safe defaults.)

4. **Run SkyScry:**
   ```bash
   python run.py
   ```

---

## CLI Usage

```bash
# Run with default configuration (writes to data/skyscry.db)
python run.py

# Dry-run mode: stream and normalize SDR telemetry to console without writing to SQLite
python run.py --dry-run

# Specify a custom configuration file, database path, or debug logging
python run.py --config config/config.yaml --db data/weather.db --log-level DEBUG

# Override the rtl_433 binary path or deduplication window from the CLI
python run.py --bin ./bin/rtl_433-rtlsdr.exe --dedup-window 4.0
```

### Example Console Output

```text
2026-11-04 22:58:10 | INFO    | SkyScry v0.1.0 starting up...
2026-11-04 22:58:10 | INFO    | SQLite WAL storage active at data/skyscry.db
2026-11-04 22:58:10 | INFO    | Starting RTL-SDR stream: rtl_433 -f 433.92M -d 0 -F json
2026-11-04 22:58:12 | INFO    | Desk Indoor [Acurite-Tower #8364] (CH A)       -> Temp: 71.2°F (21.8°C) | RH: 44% | Batt: OK
2026-11-04 22:58:16 | INFO    | Neighborhood 7-in-1 Mast [Cotech-367959 #218]  -> Temp: 54.0°F (12.2°C) | RH: 68% | Wind: 6.5 mph (2.9 m/s) [Gust 10.1 mph] WSW (248°) | Rain: 1.25 in (31.8 mm) | UV: 1.0 | Light: 14200 lux | Batt: OK
2026-11-04 22:58:21 | INFO    | Garden Patio [Oregon-THGR810 #234] (CH 1)      -> Temp: 53.6°F (12.0°C) | RH: 70% | Batt: OK
```

---

## License

Released under the [MIT License](LICENSE).

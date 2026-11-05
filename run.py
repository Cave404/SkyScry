#!/usr/bin/env python3
"""Main CLI and daemon entrypoint for SkyScry (v0.0.6).

Executes the standard-library pre-flight bootstrapper (`src.updater.run_startup_bootstrap`)
before importing third-party libraries so fresh installs and self-updates never crash.
"""

from __future__ import annotations

import sys

# 1. Execute standard-library pre-flight bootstrapper BEFORE any third-party imports (Flask, PyYAML, etc.)
from src.updater import run_startup_bootstrap

if __name__ == "__main__":
    run_startup_bootstrap(sys.argv[1:])

import argparse
import logging
import os
import signal
import threading
import webbrowser
from dataclasses import replace
from types import FrameType
from typing import Optional

from werkzeug.serving import BaseWSGIServer, make_server

from src import __version__
from src.config import AppConfig, load_config
from src.db import WeatherDatabase
from src.ingest.reference_weather import ReferenceWeatherService
from src.ingest.sdr_stream import SDRStream, SDRStreamError
from src.models import NormalizedReading
from src.pipeline.normalizer import BurstDeduplicator, PacketNormalizer
from src.web.app import create_app

logger = logging.getLogger("skyscry")


def setup_logging(level_name: str = "INFO") -> None:
    """Configure clean, technical console logging."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)

    logging.getLogger("werkzeug").setLevel(logging.WARNING if level > logging.DEBUG else logging.INFO)


def resolve_display_name(
    reading: NormalizedReading,
    db: Optional[WeatherDatabase] = None,
) -> str:
    """Resolve the display name for a sensor reading.

    If a user-defined `alias` is stored in SQLite, returns `{alias} [{model} #{sensor_id}]`.
    Otherwise defaults strictly to `{model} #{sensor_id}`.
    """
    default_id = f"{reading.model} #{reading.sensor_id}"
    if db is None:
        return default_id
    alias = db.get_sensor_alias(reading.model, reading.sensor_id)
    if alias:
        return f"{alias} [{default_id}]"
    return default_id


def format_reading_summary(
    reading: NormalizedReading,
    db: Optional[WeatherDatabase] = None,
    units: str = "both",
) -> str:
    """Build a technical, single-line console telemetry summary free of hardcoded names or emojis."""
    station_tag = resolve_display_name(reading, db=db)
    if reading.channel:
        station_tag += f" (CH {reading.channel})"

    metrics: list[str] = []

    if reading.temp_c is not None and reading.temp_f is not None:
        if units == "imperial":
            metrics.append(f"Temp: {reading.temp_f:.1f}F")
        elif units == "metric":
            metrics.append(f"Temp: {reading.temp_c:.1f}C")
        else:
            metrics.append(f"Temp: {reading.temp_f:.1f}F ({reading.temp_c:.1f}C)")

    if reading.humidity is not None:
        metrics.append(f"RH: {reading.humidity:.0f}%")

    if reading.pressure_hpa is not None and reading.pressure_inhg is not None:
        metrics.append(f"Baro: {reading.pressure_inhg:.2f} inHg ({reading.pressure_hpa:.1f} hPa)")

    if reading.wind_avg_mps is not None and reading.wind_avg_mph is not None:
        if units == "imperial":
            wind_str = f"Wind: {reading.wind_avg_mph:.1f} mph"
        elif units == "metric":
            wind_str = f"Wind: {reading.wind_avg_mps:.1f} m/s"
        else:
            wind_str = f"Wind: {reading.wind_avg_mph:.1f} mph ({reading.wind_avg_mps:.1f} m/s)"

        if reading.wind_gust_mps is not None and reading.wind_gust_mph is not None:
            if units == "metric":
                wind_str += f" [Gust {reading.wind_gust_mps:.1f} m/s]"
            else:
                wind_str += f" [Gust {reading.wind_gust_mph:.1f} mph]"

        if reading.wind_dir_deg is not None and reading.wind_cardinal is not None:
            wind_str += f" {reading.wind_cardinal} ({reading.wind_dir_deg:.0f} deg)"
        metrics.append(wind_str)
    elif reading.wind_dir_deg is not None and reading.wind_cardinal is not None:
        metrics.append(f"Wind Dir: {reading.wind_cardinal} ({reading.wind_dir_deg:.0f} deg)")

    if reading.rain_cumulative_mm is not None and reading.rain_cumulative_in is not None:
        if units == "imperial":
            metrics.append(f"Rain: {reading.rain_cumulative_in:.2f} in")
        elif units == "metric":
            metrics.append(f"Rain: {reading.rain_cumulative_mm:.1f} mm")
        else:
            metrics.append(
                f"Rain: {reading.rain_cumulative_in:.2f} in ({reading.rain_cumulative_mm:.1f} mm)"
            )

    if reading.uv_index is not None:
        metrics.append(f"UV: {reading.uv_index:.1f}")

    if reading.lux is not None:
        metrics.append(f"Light: {reading.lux:.0f} lux")

    if reading.snr is not None or reading.rssi is not None:
        rf_parts: list[str] = []
        if reading.rssi is not None:
            rf_parts.append(f"RSSI {reading.rssi:.1f}dB")
        if reading.snr is not None:
            rf_parts.append(f"SNR {reading.snr:.1f}dB")
        metrics.append(f"RF: {'/'.join(rf_parts)} ({reading.signal_quality})")

    if reading.battery_ok is not None:
        metrics.append("Batt: OK" if reading.battery_ok else "Batt: LOW")

    metrics_body = " | ".join(metrics) if metrics else "Telemetry received"
    return f"{station_tag:<42} -> {metrics_body}"


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for SkyScry."""
    parser = argparse.ArgumentParser(
        prog="skyscry",
        description=(
            f"SkyScry v{__version__} - Open-source 433.92 MHz microclimate telemetry "
            "and weather ingestion daemon with hosted WebUI."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"SkyScry v{__version__}",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default=None,
        help="Path to YAML configuration file (default: config/config.yaml).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override SQLite database path (default: data/skyscry.db).",
    )
    parser.add_argument(
        "--bin",
        dest="binary_path",
        type=str,
        default=None,
        help="Explicit path to the rtl_433 executable.",
    )
    parser.add_argument(
        "-f",
        "--frequency",
        type=str,
        default=None,
        help="Override SDR center frequency (default: 433.92M).",
    )
    parser.add_argument(
        "--dedup-window",
        type=float,
        default=None,
        help="Override burst deduplication sliding window in seconds (default: 4.0).",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Override WebUI bind host address (default: 0.0.0.0).",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help="Override WebUI HTTP port (default: 8080).",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Run the SDR ingestion daemon without starting the HTTP WebUI server.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Automatically open http://localhost:<port> in the default browser on startup.",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="Bypass startup GitHub self-update, pip upgrade, and rtl_433 release checks.",
    )
    parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Stream and normalize SDR telemetry to console without writing to SQLite.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override console logging verbosity.",
    )
    return parser


def _run_sdr_ingest_loop(
    stream: SDRStream,
    normalizer: PacketNormalizer,
    db: Optional[WeatherDatabase],
    config: AppConfig,
    stop_event: threading.Event,
) -> None:
    """Background worker loop that ingests, deduplicates, filters outliers, and persists RTL-SDR bursts."""
    while not stop_event.is_set():
        try:
            for raw_line in stream.stream_lines():
                if stop_event.is_set():
                    break
                reading = normalizer.process_line(raw_line)
                if reading is None:
                    continue

                if db is not None:
                    row_id = db.insert_reading(reading)
                    if row_id is None:
                        continue

                summary = format_reading_summary(
                    reading=reading,
                    db=db,
                    units=config.logging.console_units,
                )
                logger.info(summary)

                if reading.battery_ok is False:
                    station_label = resolve_display_name(reading, db=db)
                    logger.warning(
                        "LOW BATTERY reported by %s (sensor_key=%s)",
                        station_label,
                        reading.sensor_key,
                    )
        except SDRStreamError as exc:
            if stop_event.is_set():
                break
            logger.error("SDR stream error: %s", exc)
            if not config.sdr.auto_reconnect:
                break
            stop_event.wait(timeout=max(2.0, config.sdr.reconnect_delay_seconds))
        except Exception as exc:
            if stop_event.is_set():
                break
            logger.exception("Unexpected error in SDR ingestion worker: %s", exc)
            stop_event.wait(timeout=5.0)


def _run_reference_weather_loop(
    ref_service: ReferenceWeatherService,
    config: AppConfig,
    stop_event: threading.Event,
) -> None:
    """Periodically sync regional barometric pressure (`inHg`/`hPa`) and 48h backfill."""
    if not config.location.enable_regional_backfill:
        return
    interval_sec = max(300.0, float(config.location.sync_interval_minutes) * 60.0)
    while not stop_event.is_set():
        try:
            ref_service.sync_regional_weather(past_hours=config.location.backfill_hours)
        except Exception as exc:
            logger.debug("Regional barometric/weather sync skipped: %s", exc)
        stop_event.wait(timeout=interval_sec)


def main(argv: Optional[list[str]] = None) -> int:
    """Run the SkyScry ingestion daemon, regional barometer sync, and WebUI server."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    config = load_config(args.config)

    sdr_cfg = config.sdr
    if args.binary_path:
        sdr_cfg = replace(sdr_cfg, binary_path=args.binary_path)
    if args.frequency:
        sdr_cfg = replace(sdr_cfg, frequency=args.frequency)

    db_cfg = config.database
    if args.db:
        db_cfg = replace(db_cfg, path=args.db)

    pipe_cfg = config.pipeline
    if args.dedup_window is not None:
        pipe_cfg = replace(pipe_cfg, dedup_window_seconds=args.dedup_window)

    web_cfg = config.web
    if args.host:
        web_cfg = replace(web_cfg, host=args.host)
    if args.port is not None:
        web_cfg = replace(web_cfg, port=args.port)
    if args.no_web:
        web_cfg = replace(web_cfg, enabled=False)

    log_cfg = config.logging
    if args.log_level:
        log_cfg = replace(log_cfg, level=args.log_level)

    config = replace(
        config,
        sdr=sdr_cfg,
        database=db_cfg,
        pipeline=pipe_cfg,
        web=web_cfg,
        logging=log_cfg,
    )

    setup_logging(config.logging.level)
    logger.info("SkyScry v%s starting up...", __version__)

    deduplicator = BurstDeduplicator(
        window_seconds=config.pipeline.dedup_window_seconds,
        prune_interval_seconds=config.pipeline.dedup_prune_interval_seconds,
    )
    normalizer = PacketNormalizer(deduplicator=deduplicator)
    stream = SDRStream(sdr_config=config.sdr)

    db_path = ":memory:" if args.dry_run else config.database.path
    db = WeatherDatabase(db_path=db_path)
    if args.dry_run:
        logger.info("DRY-RUN mode enabled: using transient in-memory SQLite database.")
    else:
        logger.info("SQLite WAL storage active at %s", config.database.path)

    ref_service = ReferenceWeatherService(
        db=db,
        default_lat=config.location.latitude,
        default_lon=config.location.longitude,
    )

    stop_event = threading.Event()
    http_server: Optional[BaseWSGIServer] = None

    def _initiate_shutdown(signum: int, _frame: Optional[FrameType]) -> None:
        if stop_event.is_set():
            return
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info("Received %s - shutting down SkyScry services...", sig_name)
        stop_event.set()
        stream.stop()
        if http_server is not None:
            threading.Thread(target=http_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _initiate_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _initiate_shutdown)

    ref_thread = threading.Thread(
        target=_run_reference_weather_loop,
        args=(ref_service, config, stop_event),
        name="skyscry-ref-weather",
        daemon=True,
    )
    ref_thread.start()

    ingest_thread = threading.Thread(
        target=_run_sdr_ingest_loop,
        args=(stream, normalizer, None if args.dry_run else db, config, stop_event),
        name="skyscry-sdr-ingest",
        daemon=True,
    )
    ingest_thread.start()

    exit_code = 0
    try:
        if config.web.enabled:
            flask_app = create_app(db=db, sdr_stream=stream, ref_service=ref_service)
            http_server = make_server(
                config.web.host,
                config.web.port,
                flask_app,
                threaded=True,
            )
            logger.info(
                "Hosted WebUI & REST API listening on http://%s:%d",
                config.web.host,
                config.web.port,
            )

            # Open default browser once HTTP server is ready (prevent duplicate tabs on self-update re-exec)
            if args.open_browser and os.environ.get("SKYSCRY_BROWSER_OPENED") != "1":
                os.environ["SKYSCRY_BROWSER_OPENED"] = "1"
                browser_url = f"http://localhost:{config.web.port}"
                timer = threading.Timer(1.5, webbrowser.open, args=(browser_url,))
                timer.daemon = True
                timer.start()

            http_server.serve_forever()
        else:
            logger.info("WebUI disabled (--no-web); running headless SDR ingestion loop.")
            while ingest_thread.is_alive() and not stop_event.is_set():
                ingest_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        logger.info("Interrupted by user - stopping SkyScry.")
    except Exception as exc:
        logger.exception("Fatal exception in SkyScry daemon: %s", exc)
        exit_code = 1
    finally:
        stop_event.set()
        stream.stop()
        if http_server is not None:
            try:
                http_server.server_close()
            except Exception:
                pass
        if ingest_thread.is_alive():
            ingest_thread.join(timeout=3.0)
        db.close()
        logger.info("SkyScry stopped cleanly.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

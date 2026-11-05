#!/usr/bin/env python3
"""Main CLI entrypoint for the SkyScry 433.92 MHz weather ingestion daemon."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from dataclasses import replace
from types import FrameType
from typing import Optional

from src import __version__
from src.config import AppConfig, load_config
from src.db import WeatherDatabase
from src.ingest.sdr_stream import SDRStream, SDRStreamError
from src.models import NormalizedReading
from src.pipeline.normalizer import BurstDeduplicator, PacketNormalizer

logger = logging.getLogger("skyscry")


def setup_logging(level_name: str = "INFO") -> None:
    """Configure clean, structured console logging."""
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


def format_reading_summary(
    reading: NormalizedReading,
    config: AppConfig,
    units: str = "both",
) -> str:
    """Build a human-readable single-line console telemetry summary."""
    alias = config.get_sensor_label(reading.sensor_id, reading.model)
    station_tag = (
        f"{alias} [{reading.model} #{reading.sensor_id}]"
        if alias
        else f"{reading.model} #{reading.sensor_id}"
    )
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

    if reading.pressure_hpa is not None:
        metrics.append(f"Press: {reading.pressure_hpa:.1f} hPa")

    if reading.wind_avg_mps is not None and reading.wind_avg_mph is not None:
        if units == "imperial":
            wind_str = f"Wind: {reading.wind_avg_mph:.1f} mph"
        elif units == "metric":
            wind_str = f"Wind: {reading.wind_avg_mps:.1f} m/s"
        else:
            wind_str = f"Wind: {reading.wind_avg_mph:.1f} mph ({reading.wind_avg_mps:.1f} m/s)"

        if reading.wind_gust_mps is not None and reading.wind_gust_mph is not None:
            if units == "imperial":
                wind_str += f" [Gust {reading.wind_gust_mph:.1f} mph]"
            elif units == "metric":
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

    if reading.battery_ok is not None:
        metrics.append("Batt: OK" if reading.battery_ok else "Batt: LOW [!]")

    metrics_body = " | ".join(metrics) if metrics else "Telemetry received"
    return f"{station_tag:<46} -> {metrics_body}"


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for SkyScry."""
    parser = argparse.ArgumentParser(
        prog="skyscry",
        description=(
            f"SkyScry v{__version__} - Open-source 433.92 MHz microclimate telemetry "
            "and weather ingestion daemon."
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


def main(argv: Optional[list[str]] = None) -> int:
    """Run the SkyScry ingestion daemon."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    config = load_config(args.config)

    # Apply CLI overrides cleanly
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

    log_cfg = config.logging
    if args.log_level:
        log_cfg = replace(log_cfg, level=args.log_level)

    config = replace(
        config,
        sdr=sdr_cfg,
        database=db_cfg,
        pipeline=pipe_cfg,
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

    db: Optional[WeatherDatabase] = None
    if args.dry_run:
        logger.info("DRY-RUN mode enabled: SQLite persistence is disabled.")
    else:
        db = WeatherDatabase(db_path=config.database.path)
        logger.info("SQLite WAL storage active at %s", config.database.path)

    def _handle_shutdown(signum: int, _frame: Optional[FrameType]) -> None:
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info("Received %s — shutting down gracefully...", sig_name)
        stream.stop()

    signal.signal(signal.SIGINT, _handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_shutdown)

    exit_code = 0
    try:
        for raw_line in stream.stream_lines():
            reading = normalizer.process_line(raw_line)
            if reading is None:
                continue

            if db is not None:
                db.insert_reading(reading)

            summary = format_reading_summary(
                reading=reading,
                config=config,
                units=config.logging.console_units,
            )
            logger.info(summary)

            if reading.battery_ok is False:
                alias = config.get_sensor_label(reading.sensor_id, reading.model) or reading.model
                logger.warning(
                    "LOW BATTERY reported by %s (model=%s, id=%s)",
                    alias,
                    reading.model,
                    reading.sensor_id,
                )
    except KeyboardInterrupt:
        logger.info("Interrupted by user — stopping SkyScry.")
    except SDRStreamError as exc:
        logger.error("SDR stream error: %s", exc)
        exit_code = 1
    except Exception as exc:
        logger.exception("Unhandled fatal exception in SkyScry daemon: %s", exc)
        exit_code = 1
    finally:
        stream.stop()
        if db is not None:
            db.close()
        logger.info("SkyScry stopped cleanly.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

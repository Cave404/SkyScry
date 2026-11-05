"""Configuration loading and cross-platform binary resolution for SkyScry."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Project root directory (two levels up from src/config.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"

# Candidate binary names ordered by platform specificity
RTL433_CANDIDATES: tuple[str, ...] = (
    "rtl_433-rtlsdr.exe",
    "rtl_433.exe",
    "rtl_433",
)


def _parse_bool_env(val: Optional[str], default: bool) -> bool:
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SDRConfig:
    """RTL-SDR receiver and rtl_433 subprocess configuration."""

    binary_path: Optional[str] = None
    frequency: str = "433.92M"
    device_index: int = 0
    extra_args: list[str] = field(default_factory=list)
    auto_reconnect: bool = True
    reconnect_delay_seconds: float = 5.0


@dataclass(frozen=True)
class DatabaseConfig:
    """SQLite storage engine configuration."""

    path: str = "data/skyscry.db"


@dataclass(frozen=True)
class PipelineConfig:
    """Telemetry normalization and burst deduplication configuration."""

    dedup_window_seconds: float = 4.0
    dedup_prune_interval_seconds: float = 300.0


@dataclass(frozen=True)
class LocationConfig:
    """Station coordinates and Open-Meteo regional barometric/history backfill configuration."""

    latitude: float = 0.0
    longitude: float = 0.0
    enable_regional_backfill: bool = True
    backfill_hours: int = 48
    sync_interval_minutes: int = 15


@dataclass(frozen=True)
class WebConfig:
    """Hosted WebUI, reverse-proxy, and optional public read-only/PIN security configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    admin_pin: Optional[str] = None
    read_only_public: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    """Console and daemon logging configuration."""

    level: str = "INFO"
    console_units: str = "both"


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration container."""

    auto_update: bool = True
    sdr: SDRConfig = field(default_factory=SDRConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def resolve_rtl433_binary(configured_path: Optional[str] = None) -> Optional[str]:
    """Dynamically resolve the rtl_433 executable across Windows, Linux, and macOS."""
    env_override = os.environ.get("SKYSCRY_RTL433_BIN") or os.environ.get("RTL433_BIN")
    for raw_candidate in (configured_path, env_override):
        if not raw_candidate:
            continue
        candidate_path = Path(raw_candidate).expanduser()
        if not candidate_path.is_absolute():
            rel_to_root = PROJECT_ROOT / candidate_path
            if rel_to_root.is_file():
                return str(rel_to_root.resolve())
        if candidate_path.is_file():
            return str(candidate_path.resolve())
        which_match = shutil.which(raw_candidate)
        if which_match:
            return which_match

    for binary_name in RTL433_CANDIDATES:
        which_match = shutil.which(binary_name)
        if which_match:
            return which_match

    search_dirs = (
        PROJECT_ROOT / "bin",
        PROJECT_ROOT,
        Path.cwd() / "bin",
        Path.cwd(),
    )
    for directory in search_dirs:
        for binary_name in RTL433_CANDIDATES:
            local_candidate = directory / binary_name
            if local_candidate.is_file():
                return str(local_candidate.resolve())

    return None


def load_config(config_path: Optional[str | Path] = None) -> AppConfig:
    """Load configuration from YAML and environment variables with safe fallbacks."""
    target_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw_data: dict[str, Any] = {}

    if target_path.is_file():
        try:
            with target_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
                if isinstance(loaded, dict):
                    raw_data = loaded
            logger.debug("Loaded configuration from %s", target_path)
        except Exception as exc:
            logger.warning(
                "Failed to parse configuration file %s (%s); falling back to safe defaults.",
                target_path,
                exc,
            )
    elif EXAMPLE_CONFIG_PATH.is_file() and config_path is None:
        try:
            with EXAMPLE_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
                if isinstance(loaded, dict):
                    raw_data = loaded
            logger.debug(
                "config/config.yaml not found; loaded defaults from %s",
                EXAMPLE_CONFIG_PATH,
            )
        except Exception as exc:
            logger.warning("Could not read example config (%s); using built-in defaults.", exc)

    sdr_raw = raw_data.get("sdr") if isinstance(raw_data.get("sdr"), dict) else {}
    db_raw = raw_data.get("database") if isinstance(raw_data.get("database"), dict) else {}
    pipe_raw = raw_data.get("pipeline") if isinstance(raw_data.get("pipeline"), dict) else {}
    loc_raw = raw_data.get("location") if isinstance(raw_data.get("location"), dict) else {}
    web_raw = raw_data.get("web") if isinstance(raw_data.get("web"), dict) else {}
    log_raw = raw_data.get("logging") if isinstance(raw_data.get("logging"), dict) else {}

    extra_args_raw = sdr_raw.get("extra_args")
    extra_args = [str(arg) for arg in extra_args_raw] if isinstance(extra_args_raw, list) else []

    sdr_cfg = SDRConfig(
        binary_path=str(sdr_raw["binary_path"]) if sdr_raw.get("binary_path") else None,
        frequency=str(os.environ.get("SKYSCRY_FREQUENCY") or sdr_raw.get("frequency", "433.92M")),
        device_index=int(os.environ.get("SKYSCRY_DEVICE_INDEX") or sdr_raw.get("device_index", 0)),
        extra_args=extra_args,
        auto_reconnect=bool(sdr_raw.get("auto_reconnect", True)),
        reconnect_delay_seconds=float(sdr_raw.get("reconnect_delay_seconds", 5.0)),
    )

    db_cfg = DatabaseConfig(
        path=str(os.environ.get("SKYSCRY_DB_PATH") or db_raw.get("path", "data/skyscry.db")),
    )

    pipe_cfg = PipelineConfig(
        dedup_window_seconds=float(pipe_raw.get("dedup_window_seconds", 4.0)),
        dedup_prune_interval_seconds=float(pipe_raw.get("dedup_prune_interval_seconds", 300.0)),
    )

    lat_val = float(loc_raw.get("latitude", 0.0) or 0.0)
    lon_val = float(loc_raw.get("longitude", 0.0) or 0.0)
    env_lat = os.environ.get("LATITUDE") or os.environ.get("SKYSCRY_LATITUDE")
    env_lon = os.environ.get("LONGITUDE") or os.environ.get("SKYSCRY_LONGITUDE")
    if env_lat and env_lat.strip():
        try:
            lat_val = float(env_lat.strip())
        except ValueError:
            pass
    if env_lon and env_lon.strip():
        try:
            lon_val = float(env_lon.strip())
        except ValueError:
            pass

    loc_cfg = LocationConfig(
        latitude=lat_val,
        longitude=lon_val,
        enable_regional_backfill=bool(loc_raw.get("enable_regional_backfill", True)),
        backfill_hours=int(loc_raw.get("backfill_hours", 48)),
        sync_interval_minutes=int(loc_raw.get("sync_interval_minutes", 15)),
    )

    raw_pin = os.environ.get("ADMIN_PIN")
    if raw_pin is None and web_raw.get("admin_pin") is not None:
        raw_pin = str(web_raw.get("admin_pin"))
    admin_pin = raw_pin.strip() if raw_pin and raw_pin.strip() else None

    read_only_default = bool(web_raw.get("read_only_public", False))
    read_only_public = _parse_bool_env(os.environ.get("READ_ONLY_PUBLIC"), read_only_default)

    web_cfg = WebConfig(
        enabled=bool(web_raw.get("enabled", True)),
        host=str(web_raw.get("host", "0.0.0.0")),
        port=int(web_raw.get("port", 8080)),
        admin_pin=admin_pin,
        read_only_public=read_only_public,
    )

    log_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")).upper(),
        console_units=str(log_raw.get("console_units", "both")).lower(),
    )

    auto_update_default = bool(raw_data.get("auto_update", True))
    auto_update = _parse_bool_env(os.environ.get("AUTO_UPDATE_ON_START"), auto_update_default)

    return AppConfig(
        auto_update=auto_update,
        sdr=sdr_cfg,
        database=db_cfg,
        pipeline=pipe_cfg,
        location=loc_cfg,
        web=web_cfg,
        logging=log_cfg,
    )

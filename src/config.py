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

# Default station metadata for out-of-the-box operation when config.yaml is absent
DEFAULT_SENSORS: dict[str, dict[str, str]] = {
    "8364": {"name": "Desk Indoor", "model": "Acurite-Tower", "location": "indoor"},
    "11873": {"name": "Backyard Shaded", "model": "Acurite-Tower", "location": "outdoor"},
    "218": {"name": "Neighborhood 7-in-1 Mast", "model": "Cotech-367959", "location": "outdoor"},
    "234": {"name": "Garden Patio", "model": "Oregon-THGR810", "location": "outdoor"},
    "102": {"name": "North Eave Probe", "model": "Acurite-606TX", "location": "outdoor"},
}


@dataclass(frozen=True)
class SDRConfig:
    """RTL-SDR receiver and rtl_433 subprocess configuration."""

    binary_path: Optional[str] = None
    frequency: str = "433.92M"
    device_index: int = 0
    extra_args: list[str] = field(default_factory=list)
    auto_reconnect: bool = False
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
class LoggingConfig:
    """Console and daemon logging configuration."""

    level: str = "INFO"
    console_units: str = "both"


@dataclass(frozen=True)
class SensorMeta:
    """Friendly label and metadata for a known 433 MHz sensor."""

    name: str
    model: Optional[str] = None
    location: Optional[str] = None


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration container."""

    sdr: SDRConfig = field(default_factory=SDRConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    sensors: dict[str, SensorMeta] = field(default_factory=dict)

    def get_sensor_label(self, sensor_id: str, model: Optional[str] = None) -> Optional[str]:
        """Resolve a human-friendly station name for a given sensor ID and optional model."""
        if model:
            composite_key = f"{model}:{sensor_id}"
            if composite_key in self.sensors:
                return self.sensors[composite_key].name

        meta = self.sensors.get(str(sensor_id))
        if meta:
            return meta.name
        return None


def resolve_rtl433_binary(configured_path: Optional[str] = None) -> Optional[str]:
    """Dynamically resolve the rtl_433 executable across Windows, Linux, and macOS.

    Resolution priority:
      1. Explicit `configured_path` passed via CLI or `config.yaml`
      2. `SKYSCRY_RTL433_BIN` or `RTL433_BIN` environment variable
      3. System `PATH` lookup via `shutil.which()`
      4. Local `./bin/` directory inside the repository
      5. Repository root directory (`./`)
    """
    # 1 & 2: Explicit config path or environment variable override
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

    # 3: System PATH via shutil.which
    for binary_name in RTL433_CANDIDATES:
        which_match = shutil.which(binary_name)
        if which_match:
            return which_match

    # 4 & 5: Local ./bin/ folder and repository root fallbacks
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


def _parse_sensors(raw_sensors: Any) -> dict[str, SensorMeta]:
    """Convert raw YAML sensor mappings into strongly-typed SensorMeta objects."""
    if not isinstance(raw_sensors, dict):
        raw_sensors = DEFAULT_SENSORS

    parsed: dict[str, SensorMeta] = {}
    for key, value in raw_sensors.items():
        str_key = str(key).strip()
        if isinstance(value, str):
            parsed[str_key] = SensorMeta(name=value)
        elif isinstance(value, dict):
            name = str(value.get("name") or f"Sensor {str_key}")
            model = str(value["model"]) if value.get("model") else None
            location = str(value["location"]) if value.get("location") else None
            parsed[str_key] = SensorMeta(name=name, model=model, location=location)
    return parsed


def load_config(config_path: Optional[str | Path] = None) -> AppConfig:
    """Load configuration from YAML with safe fallbacks to built-in defaults."""
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
    log_raw = raw_data.get("logging") if isinstance(raw_data.get("logging"), dict) else {}
    sensors_raw = raw_data.get("sensors", DEFAULT_SENSORS)

    extra_args_raw = sdr_raw.get("extra_args")
    extra_args = [str(arg) for arg in extra_args_raw] if isinstance(extra_args_raw, list) else []

    sdr_cfg = SDRConfig(
        binary_path=str(sdr_raw["binary_path"]) if sdr_raw.get("binary_path") else None,
        frequency=str(sdr_raw.get("frequency", "433.92M")),
        device_index=int(sdr_raw.get("device_index", 0)),
        extra_args=extra_args,
        auto_reconnect=bool(sdr_raw.get("auto_reconnect", False)),
        reconnect_delay_seconds=float(sdr_raw.get("reconnect_delay_seconds", 5.0)),
    )

    db_cfg = DatabaseConfig(
        path=str(db_raw.get("path", "data/skyscry.db")),
    )

    pipe_cfg = PipelineConfig(
        dedup_window_seconds=float(pipe_raw.get("dedup_window_seconds", 4.0)),
        dedup_prune_interval_seconds=float(pipe_raw.get("dedup_prune_interval_seconds", 300.0)),
    )

    log_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")).upper(),
        console_units=str(log_raw.get("console_units", "both")).lower(),
    )

    return AppConfig(
        sdr=sdr_cfg,
        database=db_cfg,
        pipeline=pipe_cfg,
        logging=log_cfg,
        sensors=_parse_sensors(sensors_raw),
    )


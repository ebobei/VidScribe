"""YAML config loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.errors import ConfigError
from app.models import RunConfig


def load_yaml_file(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")
    if not path.is_file():
        raise ConfigError(f"Config path is not a file: {path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config file {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"Config file is empty: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping/object: {path}")

    return data


def apply_cli_overrides(raw_config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply supported CLI overrides to the raw config dictionary."""

    config = dict(raw_config)

    if overrides.get("limit") is not None:
        config["limit"] = overrides["limit"]

    if overrides.get("candidate_pool_size") is not None:
        config["candidate_pool_size"] = overrides["candidate_pool_size"]

    if overrides.get("order") is not None:
        youtube = dict(config.get("youtube") or {})
        youtube["order"] = overrides["order"]
        config["youtube"] = youtube

    if overrides.get("output_directory") is not None:
        output = dict(config.get("output") or {})
        output["directory"] = overrides["output_directory"]
        config["output"] = output

    return config


def load_run_config(config_path: str | Path, overrides: dict[str, Any] | None = None) -> RunConfig:
    raw_config = load_yaml_file(config_path)
    merged_config = apply_cli_overrides(raw_config, overrides or {})

    try:
        return RunConfig.model_validate(merged_config)
    except ValidationError as exc:
        raise ConfigError(f"Invalid VidScribe config: {exc}") from exc

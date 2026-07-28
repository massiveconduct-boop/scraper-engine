# config/loader.py
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .schema import AppConfig

# Pattern to match ${ENV_VAR} style placeholders with optional defaults
_ENV_VAR_RE = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve ${ENV_VAR} and ${ENV_VAR:default} placeholders in config values."""
    if isinstance(value, str):
        def _replace(m: re.Match[str]) -> str:
            var_name = m.group(1)
            default = m.group(2)
            result = os.environ.get(var_name, default if default is not None else m.group(0))
            return result if result is not None else m.group(0)
        return _ENV_VAR_RE.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def load_config(
    config_dir: str | None = None,
    env: str | None = None,
) -> AppConfig:
    """Load configuration from YAML files, merging base + environment-specific overrides.

    Order: base.yaml → {env}.yaml → environment variables (${VAR} placeholders)

    Args:
        config_dir: Path to config directory. Defaults to this package's directory.
        env: Environment name (e.g., 'production', 'staging').
             Defaults to APP_ENV env var, then 'staging'.

    Returns:
        Validated AppConfig instance.
    """
    if config_dir is None:
        config_dir = str(Path(__file__).parent)

    if env is None:
        env = os.environ.get("APP_ENV", "staging")

    # Load base config
    base_path = Path(config_dir) / "base.yaml"
    if not base_path.exists():
        raise FileNotFoundError(f"Base config not found: {base_path}")

    with open(base_path) as f:
        config: dict[str, Any] = yaml.safe_load(f) or {}

    # Merge environment-specific overrides
    env_path = Path(config_dir) / f"{env}.yaml"
    if env_path.exists():
        with open(env_path) as f:
            env_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, env_config)

    # Resolve ${ENV_VAR} placeholders
    config = _resolve_env_vars(config)

    return AppConfig.model_validate(config)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dictionaries. Override values take precedence."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

# observability/bootstrap.py
"""Single entry point that every process (api, cli, harvester daemon, rq
worker) calls once at startup to actually apply ObservabilityConfig.

Without this, configure_logging()/configure_tracing() exist but are never
invoked anywhere — production runs on Python's default unconfigured logger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from observability.logging import configure_logging
from observability.tracing import configure_tracing

if TYPE_CHECKING:
    from config.schema import ObservabilityConfig


def bootstrap_observability(cfg: ObservabilityConfig) -> None:
    """Apply logging config always; tracing only when enabled."""
    configure_logging(level=cfg.logging_level)
    if cfg.tracing_enabled:
        configure_tracing(otlp_endpoint=cfg.otlp_endpoint)

# observability/logging.py
"""Structured logging via structlog.

All log entries include tenant_id for enrichment (ContextVar, per §1.1.3).
"""

from __future__ import annotations

import logging
import sys as _sys

import structlog


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure structlog AND bridge stdlib logging through it.

    Every logger in this codebase is a plain ``logging.getLogger(__name__)``
    (no module calls ``get_logger()`` below) — a structlog processor chain
    that only fires for structlog-native loggers would never touch any of
    them. ``structlog.stdlib.ProcessorFormatter`` on the root handler is what
    actually makes stdlib records (which is 100% of this app's log calls)
    render through the same JSON/console pipeline.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # NOTE: no filter_by_level here — it expects a real logging.Logger with
    # a `.disabled` attribute, which foreign (plain stdlib) records passed
    # through ProcessorFormatter.foreign_pre_chain don't provide, and blows up
    # on every single stdlib log call (AttributeError, swallowed by logging's
    # own error handler — every message silently failed to format). The root
    # logger's own level (set below) already does the level filtering.
    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer() if json_format else structlog.dev.ConsoleRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(_sys.stdout)
    handler.setFormatter(formatter)

    # Set directly rather than logging.basicConfig() — basicConfig() is a
    # no-op once the root logger already has a handler (a common gotcha this
    # codebase hits: every process here imports libraries that attach their
    # own handlers before configure_logging() gets a chance to run).
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name or "scraper_engine")
    return logger

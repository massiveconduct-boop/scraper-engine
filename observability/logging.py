# observability/logging.py
"""Structured logging via structlog.

All log entries include tenant_id for enrichment (ContextVar, per §1.1.3).
"""

from __future__ import annotations

import logging
import sys as _sys

import structlog


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure structlog with the given level and format."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    if json_format:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.dev.ConsoleRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        stream=_sys.stdout,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name or "scraper_engine")
    return logger

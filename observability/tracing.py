# observability/tracing.py
"""Distributed tracing — enabled by default in staging+prod.

Closes F-20: v1.0 shipped with tracing OFF by default.
"""

from __future__ import annotations


def configure_tracing(service_name: str = "scraper-engine") -> None:
    """Initialize distributed tracing.

    Uses OpenTelemetry when available. Falls back to no-op if not installed.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()
        exporter = OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "OpenTelemetry not installed — tracing disabled"
        )

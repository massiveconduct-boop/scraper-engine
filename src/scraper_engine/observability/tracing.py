# observability/tracing.py
"""Distributed tracing — enabled by default in staging+prod.

Closes F-20: v1.0 shipped with tracing OFF by default.
"""

from __future__ import annotations


def configure_tracing(
    service_name: str = "scraper-engine",
    otlp_endpoint: str = "http://jaeger:4317",
) -> None:
    """Initialize distributed tracing.

    Uses OpenTelemetry when available. Falls back to no-op if not installed.
    ``otlp_endpoint`` must point at a real OTLP receiver (e.g. the ``jaeger``
    docker-compose service) — the exporter's own default (``localhost:4317``)
    resolves inside whichever process is exporting, not a separate container.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        # timeout=2s bounds the underlying gRPC export call itself — this is
        # what orchestrator/tasks.py's force_flush() actually waits on when an
        # rq work-horse flushes synchronously before exit; force_flush's own
        # timeout_millis doesn't shorten an in-flight export call already
        # blocked on a longer default (10s) exporter-level deadline.
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True, timeout=2)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # BatchSpanProcessor runs its own background export thread that
        # retries forever if the collector is unreachable — without an
        # explicit shutdown it outlives normal process/test teardown (caught
        # live: pytest closing its captured stdout mid-retry produced
        # "ValueError: I/O operation on closed file" from the export thread's
        # own error logging). shutdown() flushes and stops that thread.
        import atexit

        atexit.register(provider.shutdown)

        # A TracerProvider alone only covers requests FastAPIInstrumentor
        # wraps (api/main.py) — everything else this system spends its time
        # on (outbound fetches, DB queries, Redis calls, in rq workers and the
        # harvester daemon, neither of which has an HTTP request to hang
        # instrumentation off) would otherwise be invisible. These three patch
        # their respective libraries process-wide, so every call automatically
        # nests under whatever span is active (a job/cycle root span, or the
        # API's request span) with zero per-call-site changes.
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        HTTPXClientInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()
        RedisInstrumentor().instrument()

    except ImportError:
        import logging

        logging.getLogger(__name__).warning("OpenTelemetry not installed — tracing disabled")

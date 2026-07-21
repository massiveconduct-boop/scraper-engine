# observability/metrics.py
"""Prometheus metrics — WIRED into every call site, not just declared.

Closes F-19: every metric below has an explicit call site that increments/decrements it.
v1.0's metrics were declared but never incremented.
"""

from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, generate_latest

# Browser pool
browser_pool_size = Gauge(
    "browser_pool_size",
    "Current browser pool size by status",
    ["status"],  # idle, active
)

# Proxy pool
proxy_pool_size = Gauge(
    "proxy_pool_size",
    "Number of proxies in the pool",
    ["tier"],  # elite, anonymous, transparent
)

proxy_harvest_duration = Histogram(
    "proxy_harvest_duration_seconds",
    "Duration of a proxy harvest cycle",
    buckets=[10, 30, 60, 120, 300, 600],
)

proxy_selection_attempts = Histogram(
    "proxy_selection_attempts",
    "Number of attempts before selecting a proxy",
    buckets=[1, 2, 3, 5, 10],
)

proxy_exhausted_total = Counter(
    "proxy_exhausted_total",
    "Total proxy pool exhaustion events",
    ["level", "domain"],
)

# Browser
browser_launch_duration = Histogram(
    "browser_launch_duration_seconds",
    "Time to launch a Camoufox browser instance",
    buckets=[1, 2, 5, 10, 15, 30, 60],
)

# CapSolver
capsolver_daily_spend = Gauge(
    "capsolver_daily_spend",
    "Current daily CapSolver spend per tenant",
    ["tenant_id"],
)

# Circuit breaker
circuit_state = Gauge(
    "circuit_state",
    "Current circuit breaker state per domain",
    ["domain"],
)

# Jobs
job_duration = Histogram(
    "job_duration_seconds",
    "Total job processing duration by level",
    ["level"],
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

# DLQ
dlq_size = Gauge(
    "dlq_size",
    "Current dead letter queue size",
)


class MetricsRegistry:
    """Prometheus metric registry. Metrics are registered at import time."""

    def start_http_server(self, port: int = 9090) -> None:
        """Start the Prometheus metrics HTTP server."""
        from prometheus_client import start_http_server as _start
        _start(port)

    @staticmethod
    def get_metrics() -> bytes:
        """Return current Prometheus metrics in text format."""
        data: bytes = generate_latest(REGISTRY)
        return data

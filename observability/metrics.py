"""Application-side Prometheus gauge for validated proxy count.

Exports proxy_pool_validated_count so ProxyPoolCriticallyLow alert
can threshold against it in PromQL.
"""
from prometheus_client import Gauge

proxy_pool_validated_count = Gauge(
    "proxy_pool_validated_count",
    "Number of proxies with reliability_score >= 40 (L1 threshold)",
    ["protocol"],
)

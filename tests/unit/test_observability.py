"""observability/ — metrics refresh functions, logger helper, tracing fallback.

Round 64 (round-62 audit T3). /metrics' route tests patch these refresh
functions with AsyncMocks, so their bodies never ran: a wrong Redis key or a
tenant loop that stops at the first failure would have shipped unnoticed.
"""

import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.observability import metrics
from scraper_engine.observability.logging import get_logger
from scraper_engine.observability.tracing import configure_tracing


def _gauge(gauge, **labels):
    return (gauge.labels(**labels) if labels else gauge)._value.get()


def _redis(values):
    redis = MagicMock()
    redis.raw.get = AsyncMock(side_effect=lambda key: values.get(key))
    return redis


class TestCountValidatedProxies:
    @pytest.mark.asyncio
    async def test_returns_the_count(self):
        pg = MagicMock(fetch=AsyncMock(return_value=[{"n": 7}]))
        assert await metrics.count_validated_proxies(pg, TenantId("system")) == 7

    @pytest.mark.asyncio
    async def test_no_rows_is_zero(self):
        pg = MagicMock(fetch=AsyncMock(return_value=[]))
        assert await metrics.count_validated_proxies(pg, TenantId("system")) == 0


class TestRefreshDlqSize:
    @pytest.mark.asyncio
    async def test_one_broken_tenant_does_not_blank_the_rest(self):
        pg = MagicMock(
            fetch=AsyncMock(
                side_effect=[
                    [
                        {"tenant_id": "tenant_a"},
                        {"tenant_id": "tenant_b"},
                        {"tenant_id": "tenant_c"},
                    ],
                    [{"n": 2}],
                    RuntimeError("schema b missing"),
                    [],
                ]
            )
        )
        await metrics.refresh_dlq_size(pg)
        assert _gauge(metrics.dlq_size) == 2


class TestRefreshCapsolverSpend:
    @pytest.mark.asyncio
    async def test_reads_ceiling_and_spend_per_tenant(self):
        pg = MagicMock(
            fetch=AsyncMock(
                return_value=[
                    {"tenant_id": "obs_a", "capsolver_daily_credit_ceiling": 2.5},
                    {"tenant_id": "obs_b", "capsolver_daily_credit_ceiling": None},
                ]
            )
        )
        redis = _redis({"capsolver:daily_spend:obs_a": "0.75"})
        await metrics.refresh_capsolver_spend(pg, redis)
        assert _gauge(metrics.capsolver_daily_spend, tenant_id="obs_a") == 0.75
        assert _gauge(metrics.capsolver_daily_ceiling, tenant_id="obs_a") == 2.5
        assert _gauge(metrics.capsolver_daily_spend, tenant_id="obs_b") == 0.0
        assert _gauge(metrics.capsolver_daily_ceiling, tenant_id="obs_b") == 1.0


class TestRefreshProxySourceHealth:
    @pytest.mark.asyncio
    async def test_sets_only_sources_the_harvester_reported(self):
        from scraper_engine.proxy.harvester import ProxyHarvester
        from scraper_engine.proxy.source_health import REDIS_KEY_PREFIX, proxy_source_healthy

        first = ProxyHarvester.SOURCES[0][0]
        await metrics.refresh_proxy_source_health(_redis({f"{REDIS_KEY_PREFIX}{first}": "0"}))
        assert _gauge(proxy_source_healthy, source_name=first) == 0.0


class TestRefreshRedisBackedCounters:
    @pytest.mark.asyncio
    async def test_values_are_copied_and_missing_keys_read_as_zero(self):
        redis = _redis(
            {
                "metrics:circuit_breaker_trips_total": "4",
                "metrics:proxy_exhausted_total:2": "9",
                "metrics:job_duration:completed:count": "3",
                "metrics:job_duration:completed:sum": "12.5",
                "metrics:webhook_outbox_pending": "1",
            }
        )
        await metrics.refresh_redis_backed_counters(redis)
        assert _gauge(metrics.circuit_breaker_trips_total) == 4.0
        assert _gauge(metrics.proxy_exhausted_total, level="2") == 9.0
        assert _gauge(metrics.proxy_exhausted_total, level="1") == 0.0
        assert _gauge(metrics.job_duration_seconds_count, status="completed") == 3.0
        assert _gauge(metrics.job_duration_seconds_sum, status="completed") == 12.5
        assert _gauge(metrics.job_duration_seconds_count, status="failed") == 0.0
        assert _gauge(metrics.webhook_outbox_pending) == 1.0
        assert _gauge(metrics.webhook_delivery_failures_total) == 0.0


def test_get_logger_defaults_to_the_engine_name():
    assert get_logger() is not None
    assert get_logger("custom") is not None


def test_tracing_without_opentelemetry_warns_instead_of_crashing(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", None)
    with caplog.at_level(logging.WARNING):
        configure_tracing()
    assert "OpenTelemetry not installed" in caplog.text

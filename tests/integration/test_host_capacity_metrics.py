# tests/integration/test_host_capacity_metrics.py
"""observability/metrics.py::refresh_host_capacity against real Redis (round 65)."""

from __future__ import annotations

import uuid

import pytest
from prometheus_client import REGISTRY

from scraper_engine.config.schema import HostCapacityConfig
from scraper_engine.observability.metrics import refresh_host_capacity
from scraper_engine.orchestrator.host_capacity import HostAdmission
from scraper_engine.storage.redis_client import RedisClient

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_gauges_follow_the_hosts_admission_keys():
    client = RedisClient(redis_url="redis://localhost:6379/0")
    await client.start()
    host = f"hcmetrics-{uuid.uuid4().hex[:6]}"
    admission = HostAdmission(client.raw, host, HostCapacityConfig(default_units=3.0))
    try:
        await client.raw.hset(
            admission.stats_key,
            mapping={
                "granted": "5",
                "timeouts": "1",
                "adjust_up": "2",
                "wait_le_1000": "3",
                "wait_le_30000": "1",
                "wait_le_inf": "1",
                "wait_ms_sum": "45000",
                "cpu_pressure": "12.5",
            },
        )
        await refresh_host_capacity(client, admission)

        def value(name, labels=None):
            return REGISTRY.get_sample_value(name, labels or {})

        assert value("host_capacity_target_units") == 3.0
        assert value("host_capacity_in_use_units") == 0.0
        assert value("host_capacity_waiters") == 0.0
        assert value("host_cpu_pressure") == 12.5
        assert value("host_admission_granted_total") == 5.0
        assert value("host_admission_timeouts_total") == 1.0
        assert value("host_capacity_adjustments_total", {"direction": "up"}) == 2.0
        assert value("host_capacity_adjustments_total", {"direction": "down"}) == 0.0
        assert value("host_admission_wait_seconds_bucket", {"le": "1.0"}) == 3.0
        assert value("host_admission_wait_seconds_bucket", {"le": "30.0"}) == 4.0
        assert value("host_admission_wait_seconds_bucket", {"le": "+Inf"}) == 5.0
        assert value("host_admission_wait_seconds_sum") == 45.0
    finally:
        await client.raw.delete(admission.stats_key)
        await client.stop()

# tests/unit/test_lease.py
"""ProxyLease tests — context manager, heartbeat, expiry."""

import asyncio

import pytest

from scraper_engine.core.models import Proxy, ProxyProtocol
from scraper_engine.core.tenant import TenantId
from scraper_engine.proxy.lease import ProxyLease


@pytest.fixture
def proxy():
    return Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)


class TestProxyLease:
    @pytest.mark.asyncio
    async def test_context_manager_acquires(self, proxy):
        tenant = TenantId("test")
        async with ProxyLease(proxy=proxy, tenant_id=tenant, lease_ttl_seconds=120) as lease:
            assert lease.proxy.ip == "1.2.3.4"
            assert lease.tenant_id == tenant
            assert lease.is_expired is False
            assert lease._released is False

    @pytest.mark.asyncio
    async def test_context_manager_releases(self, proxy):
        tenant = TenantId("test")
        lease = ProxyLease(proxy=proxy, tenant_id=tenant, lease_ttl_seconds=120)
        async with lease:
            assert lease._acquired_at is not None
        assert lease._released is True

    @pytest.mark.asyncio
    async def test_heartbeat_extends_lease(self, proxy):
        tenant = TenantId("test")
        lease = ProxyLease(proxy=proxy, tenant_id=tenant, lease_ttl_seconds=1)
        async with lease:
            await lease.heartbeat()
            assert lease.remaining_seconds > 0

    @pytest.mark.asyncio
    async def test_expiry(self, proxy):
        tenant = TenantId("test")
        lease = ProxyLease(proxy=proxy, tenant_id=tenant, lease_ttl_seconds=0)
        async with lease:
            # Lease with TTL=0 expires immediately
            await asyncio.sleep(0.01)
            assert lease.is_expired is True

    def test_remaining_seconds_unacquired(self, proxy):
        tenant = TenantId("test")
        lease = ProxyLease(proxy=proxy, tenant_id=tenant, lease_ttl_seconds=120)
        assert lease.remaining_seconds == 0.0  # not acquired, expires_at is 0

    def test_repr(self, proxy):
        tenant = TenantId("test")
        lease = ProxyLease(proxy=proxy, tenant_id=tenant)
        assert "1.2.3.4" in repr(lease.proxy)

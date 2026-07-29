# tests/unit/test_auth.py
"""api/auth.py — TenantResolver is the only place a raw client-supplied API
key becomes a TenantId (design invariant §1.1.3). Was 0% covered."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.api.auth import TenantResolver
from scraper_engine.core.exceptions import AuthenticationError
from scraper_engine.core.tenant import TenantId


@pytest.mark.asyncio
async def test_resolve_hit_returns_tenant_id_wrapping_stored_slug():
    pg = AsyncMock()
    pg.fetchrow.return_value = {"tenant_slug": "acme"}
    resolver = TenantResolver(pg=pg)

    tenant_id = await resolver.resolve("sk-valid")

    assert isinstance(tenant_id, TenantId)
    assert str(tenant_id) == "acme"
    # api_key resolution happens before tenant scoping, so the lookup itself
    # runs under a system/global tenant context, not the resolved tenant.
    system_tenant, query, api_key = pg.fetchrow.await_args.args
    assert str(system_tenant) == "system"
    assert "api_keys" in query
    assert api_key == "sk-valid"


@pytest.mark.asyncio
async def test_resolve_miss_raises_authentication_error():
    pg = AsyncMock()
    pg.fetchrow.return_value = None
    resolver = TenantResolver(pg=pg)

    with pytest.raises(AuthenticationError):
        await resolver.resolve("sk-does-not-exist")


@pytest.mark.asyncio
async def test_create_tenant_generates_key_inserts_row_and_bootstraps_schema():
    pg = AsyncMock()
    resolver = TenantResolver(pg=pg)

    tenant_id, api_key = await resolver.create_tenant("newco")

    assert str(tenant_id) == "newco"
    assert api_key.startswith("sk-")
    assert len(api_key) == len("sk-") + 40

    assert pg.execute.await_count == 3
    insert_key_call, insert_tenant_call, bootstrap_call = pg.execute.await_args_list

    assert "INSERT INTO public.api_keys" in insert_key_call.args[1]
    assert insert_key_call.args[2] == "newco"
    assert insert_key_call.args[3] == api_key

    assert "INSERT INTO public.tenants" in insert_tenant_call.args[1]
    assert insert_tenant_call.args[2] == "newco"

    assert "create_tenant_schema" in bootstrap_call.args[1]
    assert bootstrap_call.args[2] == "newco"

    # every write in create_tenant runs under the system tenant context —
    # provisioning happens before the new tenant's own schema exists.
    for call in pg.execute.await_args_list:
        assert str(call.args[0]) == "system"


@pytest.mark.asyncio
async def test_create_tenant_generates_unique_keys():
    pg = AsyncMock()
    resolver = TenantResolver(pg=pg)

    _, api_key_1 = await resolver.create_tenant("tenanta")
    _, api_key_2 = await resolver.create_tenant("tenantb")

    assert api_key_1 != api_key_2

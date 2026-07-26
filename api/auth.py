# api/auth.py
"""API-key → TenantId resolution — the single tenant trust boundary.

Design invariant §1.1.3: tenant_id is never read from an ambient ContextVar
at a trust boundary. This is the ONLY place a raw client-supplied credential
becomes a TenantId.

BD-04: tenant provisioning is built into this system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.exceptions import AuthenticationError
from core.tenant import TenantId

if TYPE_CHECKING:
    from storage.postgres_client import PostgresClient


class TenantResolver:
    """Resolve API key → TenantId from the api_keys table.

    The ONLY place a raw client-supplied credential becomes a TenantId.
    On hit, wraps the stored tenant slug in TenantId(), which re-validates it
    (defense in depth even though it was written by us, not the client).
    """

    def __init__(self, pg: PostgresClient) -> None:
        self._pg = pg

    async def resolve(self, api_key: str) -> TenantId:
        """Look up api_key in the api_keys table. Returns TenantId or raises AuthenticationError."""
        # Use a system/global tenant context for the api_keys lookup
        # since api_key resolution happens before tenant scoping
        system_tenant = TenantId("system")
        row = await self._pg.fetchrow(
            system_tenant,
            "SELECT tenant_slug FROM public.api_keys WHERE api_key = $1 AND revoked_at IS NULL",
            api_key,
        )
        if row is None:
            raise AuthenticationError("Invalid API key")
        return TenantId(row["tenant_slug"])

    async def create_tenant(self, tenant_slug: str) -> tuple[TenantId, str]:
        """Admin endpoint: create a new tenant and generate an API key (BD-04)."""
        import secrets
        import string

        tenant_id = TenantId(tenant_slug)
        api_key = "sk-" + "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(40)
        )
        system_tenant = TenantId("system")

        await self._pg.execute(
            system_tenant,
            "INSERT INTO public.api_keys (tenant_slug, api_key) VALUES ($1, $2)",
            tenant_slug,
            api_key,
        )
        await self._pg.execute(
            system_tenant,
            "INSERT INTO public.tenants (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
            tenant_slug,
        )
        # Bootstrap the per-tenant schema (tables, indexes)
        await self._pg.execute(
            system_tenant,
            "SELECT public.create_tenant_schema($1)",
            tenant_slug,
        )
        return tenant_id, api_key

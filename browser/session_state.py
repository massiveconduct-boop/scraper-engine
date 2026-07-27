# browser/session_state.py
"""Cookie, localStorage, and storage_state persistence — Postgres-backed.

Persists Camoufox storage_state/config references, not hand-built fingerprints
(design invariant §1.1.2).

Plan §5.1-5.2: per-tenant browser_sessions table, domain-keyed, 30-day TTL.
Uses PostgresClient.acquire(tenant_id) for per-tenant schema isolation —
same pattern as the rest of the storage layer. No separate connection path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient


class SessionStateManager:
    """Persist and restore browser session state (cookies, localStorage, etc.)."""

    def __init__(self, pg: PostgresClient, ttl_days: int = 30) -> None:
        self._pg = pg
        self._ttl_days = ttl_days

    async def save(
        self, tenant_id: TenantId, domain: str, storage_state: dict[str, object]
    ) -> None:
        """Persist a storage_state blob keyed by (tenant schema, domain).

        Upserts: overwrites existing session for same domain, extends TTL.
        """
        expires_at = datetime.now(UTC) + timedelta(days=self._ttl_days)
        async with self._pg.acquire(tenant_id) as conn:
            await conn.execute(
                """INSERT INTO browser_sessions (domain, storage_state, last_used_at, expires_at)
                   VALUES ($1, $2, NOW(), $3)
                   ON CONFLICT (domain) DO UPDATE SET
                     storage_state = EXCLUDED.storage_state,
                     last_used_at = NOW(),
                     expires_at = EXCLUDED.expires_at""",
                domain,
                json.dumps(storage_state, default=str),
                expires_at,
            )

    async def load(
        self, tenant_id: TenantId, domain: str
    ) -> dict[str, object] | None:
        """Load a previously persisted storage_state, or None if not found/expired."""
        async with self._pg.acquire(tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT storage_state FROM browser_sessions "
                "WHERE domain = $1 AND expires_at > NOW()",
                domain,
            )
        if row is None:
            return None
        raw = row["storage_state"]
        if isinstance(raw, str):
            result: dict[str, object] = json.loads(raw)
        else:
            result = raw
        return result

    async def delete(self, tenant_id: TenantId, domain: str) -> None:
        """Remove a persisted storage_state.

        Called when a session turns out to be bad (site invalidated it,
        got logged out) — don't keep reloading a poisoned session.
        """
        async with self._pg.acquire(tenant_id) as conn:
            await conn.execute(
                "DELETE FROM browser_sessions WHERE domain = $1", domain
            )

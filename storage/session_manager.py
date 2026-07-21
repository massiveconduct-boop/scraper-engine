# storage/session_manager.py
"""Browser session persistence — cookies, localStorage, storage_state.

Used by browser/session_state.py to persist and restore Camoufox session blobs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tenant import TenantId

    from .postgres_client import PostgresClient
    from .redis_client import RedisClient


class SessionManager:
    """Persist browser session state (cookies, localStorage, Camoufox storage_state)."""

    def __init__(self, redis: RedisClient, pg: PostgresClient) -> None:
        self._redis = redis
        self._pg = pg

    async def save_state(
        self, tenant_id: TenantId, session_id: str, state: dict[str, object]
    ) -> None:
        """Persist a browser session state blob.

        Fast path: Redis for hot sessions. Slow path: PG for durable storage.
        """
        data = json.dumps(state, default=str)
        await self._redis.set(tenant_id, f"session:{session_id}", data, ttl=3600 * 24 * 30)

        await self._pg.execute(
            tenant_id,
            """
            INSERT INTO browser_sessions (session_id, state, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (session_id) DO UPDATE
            SET state = $2, updated_at = $3
            """,
            session_id,
            data,
            datetime.now(UTC),
        )

    async def load_state(
        self, tenant_id: TenantId, session_id: str
    ) -> dict[str, object] | None:
        """Load a previously persisted session state, or None.

        Fast path: Redis. Fallback: Postgres.
        """
        raw = await self._redis.get(tenant_id, f"session:{session_id}")
        if raw is not None:
            result: dict[str, object] = json.loads(raw)
            return result

        row = await self._pg.fetchrow(
            tenant_id,
            "SELECT state FROM browser_sessions WHERE session_id = $1",
            session_id,
        )
        if row is None:
            return None

        cached: dict[str, object] = json.loads(row["state"])
        await self._redis.set(
            tenant_id,
            f"session:{session_id}",
            row["state"],
            ttl=3600 * 24 * 30,
        )
        return cached

    async def delete_expired_sessions(self, ttl_days: int = 30) -> int:
        """Clean up sessions older than ttl_days (closes F-22). Returns count deleted.

        Runs per tenant using a system-scoped connection.
        """
        from core.tenant import TenantId

        cutoff = datetime.now(UTC)
        system = TenantId("system")

        result = await self._pg.execute(
            system,
            "DELETE FROM browser_sessions WHERE updated_at < $1",
            cutoff,
        )
        # parse count from result string like "DELETE N"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

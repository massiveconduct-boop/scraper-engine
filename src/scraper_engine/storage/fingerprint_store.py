# storage/fingerprint_store.py
"""Persists Camoufox storage_state/config references, NOT hand-built fingerprints.

Design invariant §1.1.2: Camoufox owns 100% of fingerprint surface.
This module stores opaque Camoufox-generated state blobs, never raw fingerprint data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraper_engine.core.tenant import TenantId

    from .postgres_client import PostgresClient


class FingerprintStore:
    """Store and retrieve Camoufox-generated browser configs and storage_state refs."""

    def __init__(self, pg: PostgresClient) -> None:
        self._pg = pg

    async def store_profile(
        self,
        tenant_id: TenantId,
        profile_id: str,
        storage_state_ref: str,
        config_ref: str,
    ) -> None:
        """Store a Camoufox profile reference."""
        now = datetime.now(UTC)
        await self._pg.execute(
            tenant_id,
            """
            INSERT INTO browser_profiles (profile_id, storage_state_ref, config_ref, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (profile_id) DO UPDATE
            SET storage_state_ref = $2, config_ref = $3, updated_at = $4
            """,
            profile_id,
            storage_state_ref,
            config_ref,
            now,
        )

    async def get_profile(
        self, tenant_id: TenantId, profile_id: str
    ) -> dict[str, object] | None:
        """Retrieve a profile's storage_state and config references."""
        row = await self._pg.fetchrow(
            tenant_id,
            "SELECT storage_state_ref, config_ref FROM browser_profiles WHERE profile_id = $1",
            profile_id,
        )
        if row is None:
            return None
        return {
            "storage_state_ref": row["storage_state_ref"],
            "config_ref": row["config_ref"],
        }

    async def list_profiles(self, tenant_id: TenantId) -> list[str]:
        """List all profile IDs for a tenant."""
        rows = await self._pg.fetch(
            tenant_id,
            "SELECT profile_id FROM browser_profiles ORDER BY updated_at DESC",
        )
        return [r["profile_id"] for r in rows]

# tests/unit/test_fingerprint_store.py
"""FingerprintStore tests — design invariant §1.1.2: stores opaque Camoufox
storage_state/config references only, never raw fingerprint data."""

import pytest


class FakePostgresClient:
    """In-memory PostgresClient fake, mirroring test_dedup.py's FakeRedis pattern."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.fetchrow_result: dict | None = None
        self.fetch_rows: list[dict] = []

    async def execute(self, tenant_id, query, *args):
        self.executed.append((tenant_id, query, args))
        return "OK"

    async def fetchrow(self, tenant_id, query, *args):
        self.fetchrow_calls = (tenant_id, query, args)
        return self.fetchrow_result

    async def fetch(self, tenant_id, query, *args):
        self.fetch_calls = (tenant_id, query, args)
        return self.fetch_rows


class TestFingerprintStore:
    @pytest.fixture
    def pg(self):
        return FakePostgresClient()

    @pytest.fixture
    def store(self, pg):
        from scraper_engine.storage.fingerprint_store import FingerprintStore

        return FingerprintStore(pg)

    @pytest.mark.asyncio
    async def test_store_profile_writes_upsert(self, store, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        await store.store_profile(tenant, "profile-1", "s3://state/1", "s3://config/1")

        assert len(pg.executed) == 1
        recorded_tenant, query, args = pg.executed[0]
        assert recorded_tenant == tenant
        assert "INSERT INTO browser_profiles" in query
        assert "ON CONFLICT" in query
        assert args[0] == "profile-1"
        assert args[1] == "s3://state/1"
        assert args[2] == "s3://config/1"

    @pytest.mark.asyncio
    async def test_get_profile_found(self, store, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        pg.fetchrow_result = {
            "storage_state_ref": "s3://state/1",
            "config_ref": "s3://config/1",
        }

        result = await store.get_profile(TenantId("test"), "profile-1")

        assert result == {
            "storage_state_ref": "s3://state/1",
            "config_ref": "s3://config/1",
        }

    @pytest.mark.asyncio
    async def test_get_profile_not_found(self, store, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        pg.fetchrow_result = None
        result = await store.get_profile(TenantId("test"), "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_profiles(self, store, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        pg.fetch_rows = [{"profile_id": "profile-1"}, {"profile_id": "profile-2"}]
        result = await store.list_profiles(TenantId("test"))
        assert result == ["profile-1", "profile-2"]

    @pytest.mark.asyncio
    async def test_list_profiles_empty(self, store, pg) -> None:
        from scraper_engine.core.tenant import TenantId

        pg.fetch_rows = []
        result = await store.list_profiles(TenantId("test"))
        assert result == []

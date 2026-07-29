# tests/unit/test_dedup.py
"""Deduplication engine tests — spec §3.9, design invariant §1.1.5."""

import pytest


class FakeRedis:
    """In-memory Redis fake for dedup tests."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, tenant_id, key):
        full_key = f"{tenant_id}:{key}"
        return self._store.get(full_key)

    async def set(self, tenant_id, key, value, ttl=None):
        full_key = f"{tenant_id}:{key}"
        self._store[full_key] = value
        if ttl:
            self._ttls[full_key] = ttl


class TestDeduplicationEngine:
    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def engine(self, redis):
        from scraper_engine.storage.dedup import DeduplicationEngine

        return DeduplicationEngine(redis)

    @pytest.mark.asyncio
    async def test_get_returns_none_for_miss(self, engine) -> None:
        from scraper_engine.core.tenant import TenantId

        result = await engine.get("http://example.com/page", TenantId("test"))
        assert result is None

    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, engine) -> None:
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        result = FetchResult(
            url="http://example.com/page",
            success=True,
            html="<html>hello</html>",
            level_used=1,
            duration_ms=42,
        )

        await engine.store(result, tenant)
        cached = await engine.get("http://example.com/page", tenant)
        assert cached is not None
        assert cached.url == result.url
        assert cached.success is True

    @pytest.mark.asyncio
    async def test_does_not_cache_failed_results(self, engine) -> None:
        """Design invariant §1.1.5: only successful results are cached."""
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        result = FetchResult(
            url="http://example.com/fail",
            success=False,
            level_used=1,
            duration_ms=42,
        )

        await engine.store(result, tenant)
        cached = await engine.get("http://example.com/fail", tenant)
        assert cached is None, "Failed results must not be cached"

    @pytest.mark.asyncio
    async def test_does_not_cache_challenge_pages(self, engine) -> None:
        """Design invariant §1.1.5: challenge pages must not be cached."""
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        result = FetchResult(
            url="http://example.com/challenge",
            success=True,
            is_challenge_page=True,
            html="<script>g-recaptcha</script>",
            level_used=1,
            duration_ms=42,
        )

        await engine.store(result, tenant)
        cached = await engine.get("http://example.com/challenge", tenant)
        assert cached is None, "Challenge pages must not be cached"

    @pytest.mark.asyncio
    async def test_invalidate(self, engine) -> None:
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        result = FetchResult(
            url="http://example.com/page",
            success=True,
            html="<html>old</html>",
            level_used=1,
            duration_ms=42,
        )

        await engine.store(result, tenant)
        await engine.invalidate("http://example.com/page", tenant)
        cached = await engine.get("http://example.com/page", tenant)
        assert cached is None

    @pytest.mark.asyncio
    async def test_change_detection(self, engine) -> None:
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        old = FetchResult(
            url="http://example.com/page",
            success=True,
            html="<html>old content</html>",
            level_used=1,
            duration_ms=42,
        )
        new = FetchResult(
            url="http://example.com/page",
            success=True,
            html="<html>new content</html>",
            level_used=1,
            duration_ms=42,
        )

        await engine.store(old, tenant)
        changed = await engine.has_changed(new, tenant)
        assert changed is True

    @pytest.mark.asyncio
    async def test_no_change_when_same(self, engine) -> None:
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        result = FetchResult(
            url="http://example.com/page",
            success=True,
            html="<html>same</html>",
            level_used=1,
            duration_ms=42,
        )

        await engine.store(result, tenant)
        changed = await engine.has_changed(result, tenant)
        assert changed is False

    @pytest.mark.asyncio
    async def test_has_changed_true_when_never_cached(self, engine) -> None:
        """No prior store() call — cached is None, so has_changed short-circuits True."""
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        result = FetchResult(
            url="http://example.com/never-cached",
            success=True,
            html="<html>fresh</html>",
            level_used=1,
            duration_ms=42,
        )

        changed = await engine.has_changed(result, tenant)
        assert changed is True

    @pytest.mark.asyncio
    async def test_content_hash_includes_markdown(self, engine) -> None:
        """_content_hash must fold in markdown, not just html (line 47 branch)."""
        from scraper_engine.core.models import FetchResult
        from scraper_engine.core.tenant import TenantId

        tenant = TenantId("test")
        old = FetchResult(
            url="http://example.com/md",
            success=True,
            markdown="old markdown",
            level_used=1,
            duration_ms=42,
        )
        new = FetchResult(
            url="http://example.com/md",
            success=True,
            markdown="new markdown",
            level_used=1,
            duration_ms=42,
        )

        await engine.store(old, tenant)
        changed = await engine.has_changed(new, tenant)
        assert changed is True

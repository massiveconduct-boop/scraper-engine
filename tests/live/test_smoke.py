# tests/live/test_smoke.py
"""Live smoke tests — only against owned/consented targets (BD-05).

These tests run ONLY against whitelisted test endpoints.
Anti-detection validation against real targets requires the self-hosted
Cloudflare-challenge-page mirror and is handled outside CI.
"""

import pytest


@pytest.mark.live
class TestPublicEndpoints:
    """Smoke tests against public test endpoints (BD-05)."""

    @pytest.mark.asyncio
    async def test_httpbin_reachable(self):
        """Verify httpbin.org is reachable for live testing."""
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://httpbin.org/ip")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_l1_fetch_httpbin(self):
        """Level 1 fetch against httpbin.org."""
        from core.tenant import TenantId
        from fetcher.level_1 import Level1Fetcher

        fetcher = Level1Fetcher()
        result = await fetcher.fetch(
            "https://httpbin.org/get",
            TenantId("system"),
        )
        assert result.success is True
        assert result.http_status == 200
        assert result.html is not None

    @pytest.mark.asyncio
    async def test_challenge_detector_no_false_positive(self):
        """Challenge detector must not flag httpbin as a challenge page."""
        from fetcher.challenge_detector import ChallengeDetector
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://httpbin.org/html")
            html = response.text

        detector = ChallengeDetector()
        assert detector.is_challenge_page(html, 200) is False

    @pytest.mark.asyncio
    async def test_ssrf_guard_blocks_loopback(self):
        """Live SSRF guard test — must block localhost."""
        from core.ssrf_guard import SSRFGuard
        from core.exceptions import SSRFBlockedError

        guard = SSRFGuard()
        with pytest.raises(SSRFBlockedError):
            await guard.validate("http://127.0.0.1:9999/secret")

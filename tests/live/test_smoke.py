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
        import asyncio

        import httpx
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get("https://httpbin.org/ip")
                    if response.status_code == 200:
                        return
            except (httpx.ReadTimeout, httpx.ConnectError):
                pass
            if attempt < 2:
                await asyncio.sleep(2)
        pytest.skip("httpbin.org unreachable after 3 attempts")

    @pytest.mark.asyncio
    async def test_l1_fetch_httpbin(self):
        """Level 1 fetch against httpbin.org."""
        import asyncio

        from core.tenant import TenantId
        from fetcher.level_1 import Level1Fetcher

        for attempt in range(3):
            fetcher = Level1Fetcher()
            result = await fetcher.fetch(
                "https://httpbin.org/get",
                TenantId("system"),
            )
            if result.success:
                assert result.http_status == 200
                assert result.html is not None
                return
            if attempt < 2:
                await asyncio.sleep(2)
        pytest.skip("httpbin.org L1 fetch failed after 3 attempts")

    @pytest.mark.asyncio
    async def test_challenge_detector_no_false_positive(self):
        """Challenge detector must not flag httpbin as a challenge page."""
        import asyncio

        import httpx

        from fetcher.challenge_detector import ChallengeDetector

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get("https://httpbin.org/html")
                    html = response.text
                detector = ChallengeDetector()
                assert detector.is_challenge_page(html, 200) is False
                return
            except (httpx.ReadTimeout, httpx.ConnectError):
                if attempt < 2:
                    await asyncio.sleep(2)
        pytest.skip("httpbin.org unreachable after 3 attempts")

    @pytest.mark.asyncio
    async def test_ssrf_guard_blocks_loopback(self):
        """Live SSRF guard test — must block localhost."""
        from core.exceptions import SSRFBlockedError
        from core.ssrf_guard import SSRFGuard

        guard = SSRFGuard()
        with pytest.raises(SSRFBlockedError):
            await guard.validate("http://127.0.0.1:9999/secret")

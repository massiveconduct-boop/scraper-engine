# tests/unit/test_level1_ja3_wiring.py
"""Round 26: JA3-matched botasaurus_requests client — config-gated wiring
into Level1Fetcher. Tries the JA3 client first, falls back to plain httpx
on any failure, same first-attempt/fallback shape as Level2Fetcher's
Botasaurus-then-Camoufox pipeline."""

from unittest.mock import AsyncMock

import pytest

from core.tenant import TenantId
from fetcher.level_1 import Level1Fetcher
from services.botasaurus_requests_client import Ja3Response


class _FakeResponse:
    status_code = 200
    text = "<html>httpx fallback</html>"
    is_redirect = False


class _FakeAsyncClient:
    def __init__(self, *a, **kw): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url): return _FakeResponse()


def _fake_ja3_client(session: AsyncMock) -> AsyncMock:
    ja3 = AsyncMock()
    ja3.open_session.return_value = session
    return ja3


@pytest.mark.asyncio
async def test_uses_ja3_result_when_it_succeeds(monkeypatch):
    session = AsyncMock()
    session.get.return_value = Ja3Response(status_code=200, text="<html>ja3</html>", location=None)
    ja3 = _fake_ja3_client(session)
    fetcher = Level1Fetcher(ja3_client=ja3)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    result = await fetcher.fetch("http://example.com", TenantId("system"))

    assert result.success is True
    assert result.html == "<html>ja3</html>"
    session.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_reuses_one_session_across_redirect_hops(monkeypatch):
    """A session opened for the first hop must be the same one used for
    every subsequent redirect — otherwise cookies set by an intermediate
    hop are lost (the bug found and fixed during PR review)."""
    session = AsyncMock()
    session.get.side_effect = [
        Ja3Response(status_code=302, text="", location="http://example.com/next"),
        Ja3Response(status_code=200, text="<html>final</html>", location=None),
    ]
    ja3 = _fake_ja3_client(session)
    fetcher = Level1Fetcher(ja3_client=ja3)

    result = await fetcher.fetch("http://example.com", TenantId("system"))

    assert result.success is True
    assert result.html == "<html>final</html>"
    ja3.open_session.assert_awaited_once()  # one session, not one per hop
    assert session.get.await_count == 2


@pytest.mark.asyncio
async def test_falls_back_to_httpx_when_ja3_raises(monkeypatch):
    session = AsyncMock()
    session.get.side_effect = RuntimeError("tls client crashed")
    ja3 = _fake_ja3_client(session)
    fetcher = Level1Fetcher(ja3_client=ja3)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    result = await fetcher.fetch("http://example.com", TenantId("system"))

    assert result.success is True
    assert result.html == "<html>httpx fallback</html>"


@pytest.mark.asyncio
async def test_no_ja3_client_skips_straight_to_httpx(monkeypatch):
    fetcher = Level1Fetcher(ja3_client=None)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    result = await fetcher.fetch("http://example.com", TenantId("system"))

    assert result.success is True
    assert result.html == "<html>httpx fallback</html>"

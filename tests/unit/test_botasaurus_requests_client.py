# tests/unit/test_botasaurus_requests_client.py
"""Round 26: BotasaurusRequestsClient wraps botasaurus_requests' JA3-matched
`firefox` session for L1, config-gated off by default. No real network calls
here — `botasaurus_requests.session.firefox` is patched out; these tests
verify the async/redirect-disabled wiring, not the TLS client itself."""

from unittest.mock import MagicMock, patch

import pytest

from scraper_engine.services.botasaurus_requests_client import (
    BotasaurusRequestsClient,
    Ja3Session,
    build_ja3_client,
)


def _fake_session(
    status_code: int = 200, text: str = "<html>ok</html>", location: str | None = None
):
    session = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.headers = {"location": location} if location else {}
    session.get.return_value = response
    return session


class TestBuildJa3Client:
    def test_disabled_returns_none(self):
        assert build_ja3_client(enabled=False) is None

    def test_enabled_returns_client(self):
        client = build_ja3_client(enabled=True)
        assert isinstance(client, BotasaurusRequestsClient)


class TestBotasaurusRequestsClient:
    @pytest.mark.asyncio
    async def test_get_returns_parsed_response(self):
        client = BotasaurusRequestsClient()
        session = _fake_session()
        with patch("botasaurus_requests.session.firefox") as fake_firefox:
            fake_firefox.Session.return_value = session
            result = await client.get("https://target.example/page")
        assert result.status_code == 200
        assert result.text == "<html>ok</html>"
        assert result.location is None

    @pytest.mark.asyncio
    async def test_get_always_disables_redirects(self):
        """fetcher/level_1.py owns the redirect loop so every hop gets
        SSRF-revalidated — this client must never follow redirects itself."""
        client = BotasaurusRequestsClient()
        session = _fake_session()
        with patch("botasaurus_requests.session.firefox") as fake_firefox:
            fake_firefox.Session.return_value = session
            await client.get("https://target.example/page")
        _, kwargs = session.get.call_args
        assert kwargs["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_get_passes_proxy_dict(self):
        client = BotasaurusRequestsClient()
        session = _fake_session()
        with patch("botasaurus_requests.session.firefox") as fake_firefox:
            fake_firefox.Session.return_value = session
            await client.get("https://target.example/page", proxy="http://1.2.3.4:8080")
        _, kwargs = session.get.call_args
        assert kwargs["proxies"] == {
            "http": "http://1.2.3.4:8080",
            "https": "http://1.2.3.4:8080",
        }

    @pytest.mark.asyncio
    async def test_get_surfaces_redirect_location(self):
        client = BotasaurusRequestsClient()
        session = _fake_session(status_code=302, location="https://target.example/next")
        with patch("botasaurus_requests.session.firefox") as fake_firefox:
            fake_firefox.Session.return_value = session
            result = await client.get("https://target.example/page")
        assert result.status_code == 302
        assert result.location == "https://target.example/next"


class TestJa3Session:
    @pytest.mark.asyncio
    async def test_open_session_constructs_one_firefox_session(self):
        client = BotasaurusRequestsClient()
        session = _fake_session()
        with patch("botasaurus_requests.session.firefox") as fake_firefox:
            fake_firefox.Session.return_value = session
            ja3_session = await client.open_session()
        assert isinstance(ja3_session, Ja3Session)
        fake_firefox.Session.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_reused_across_multiple_get_calls(self):
        """The whole point of open_session(): cookies from the first call
        stay on the same underlying session for the second — a fresh
        firefox.Session() per get() (the pre-review design) would lose them."""
        client = BotasaurusRequestsClient()
        session = _fake_session()
        with patch("botasaurus_requests.session.firefox") as fake_firefox:
            fake_firefox.Session.return_value = session
            ja3_session = await client.open_session()
            await ja3_session.get("https://target.example/1")
            await ja3_session.get("https://target.example/2")
        # Exactly one underlying firefox.Session() for both calls
        fake_firefox.Session.assert_called_once()
        assert session.get.call_count == 2

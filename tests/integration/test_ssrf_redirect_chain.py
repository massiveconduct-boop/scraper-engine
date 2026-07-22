"""
Closes G-11 from the production-readiness gap audit.

SSRF redirect-chain test: a public URL that 302-redirects to a private range
must be caught by validate_redirect_chain, not just the pre-enqueue check.

Uses a minimal HTTP server that redirects to 169.254.169.254 to verify
the SSRF guard catches the redirect target.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from core.exceptions import SSRFBlockedError
from core.ssrf_guard import SSRFGuard


class _RedirectToMetadataHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server that 302-redirects to 169.254.169.254 (cloud metadata)."""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress server logs


@pytest.fixture
def redirect_server():
    """Start a server on a random port that redirects to cloud metadata IP."""
    server = HTTPServer(("127.0.0.1", 0), _RedirectToMetadataHandler)
    port = server.server_address[1]
    thread = asyncio.get_event_loop().run_in_executor(None, server.handle_request)
    yield f"http://127.0.0.1:{port}/redirect"
    server.server_close()


class TestSSRFRedirectChain:
    """G-11: SSRF guard must catch redirect-to-private-range."""

    def test_initial_url_validates_at_enqueue(self):
        """Pre-enqueue check: direct private IP must be blocked."""
        guard = SSRFGuard()

        async def check():
            with pytest.raises(SSRFBlockedError):
                await guard.validate("http://169.254.169.254/")

        asyncio.run(check())

    @pytest.mark.asyncio
    async def test_validate_redirect_chain_catches_private_target(self):
        """G-11: validate_redirect_chain must catch redirect targets.

        A URL that resolves publicly (127.0.0.1) but whose redirect target
        (169.254.169.254) is in a denied range must be blocked.
        """
        guard = SSRFGuard()
        # Simulate a response object that redirected to cloud metadata
        redirect_target = "http://169.254.169.254/latest/meta-data/"

        class MockResponse:
            url = redirect_target

        with pytest.raises(SSRFBlockedError):
            await guard.validate_redirect_chain(MockResponse())

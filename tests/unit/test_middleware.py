# tests/unit/test_middleware.py
"""API middleware tests — rate limiting, CORS, size limits, security headers."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scraper_engine.api.middleware import (
    RateLimitMiddleware,
    configure_middleware,
)


@pytest.fixture
def rate_limited_app():
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, max_requests=3, window_seconds=60)  # type: ignore[arg-type]
    return app


@pytest.fixture
def hardened_app():
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint():
        return {"ok": True}

    @app.post("/test")
    async def test_post():
        return {"ok": True}

    configure_middleware(app)
    return app


class TestRateLimit:
    def test_requests_under_limit(self, rate_limited_app):
        client = TestClient(rate_limited_app)
        for _ in range(3):
            r = client.get("/test")
            assert r.status_code == 200

    def test_rate_limit_exceeded(self, rate_limited_app):
        client = TestClient(rate_limited_app)
        for _ in range(3):
            client.get("/test")
        r = client.get("/test")
        assert r.status_code == 429


class TestRequestSizeLimit:
    def test_body_under_limit(self, hardened_app):
        client = TestClient(hardened_app)
        r = client.post("/test", json={"data": "small"})
        assert r.status_code == 200

    def test_body_over_limit(self, hardened_app):
        client = TestClient(hardened_app)
        large_payload = {"data": "x" * 2_000_000}
        r = client.post("/test", json=large_payload)
        assert r.status_code == 413


class TestSecurityHeaders:
    def test_security_headers_present(self, hardened_app):
        client = TestClient(hardened_app)
        r = client.get("/test")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("strict-transport-security") is not None


class TestCORS:
    def test_cors_headers_present(self, hardened_app):
        client = TestClient(hardened_app)
        r = client.options(
            "/test",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" in r.headers

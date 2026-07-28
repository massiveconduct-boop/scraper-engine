# api/middleware.py
"""FastAPI middleware — rate limiting, CORS, request size limits, security headers."""

from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests larger than max_size_bytes."""

    MAX_BODY_BYTES = 1_048_576  # 1 MB

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body exceeds {self.MAX_BODY_BYTES} bytes"},
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory sliding window rate limiter.

    Per-IP rate limiting. For production, use Redis-backed rate limiter
    (e.g., slowapi with Redis backend). This is a lightweight default.
    """

    def __init__(self, app: FastAPI, max_requests: int = 100, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._store: dict[str, list[float]] = {}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        key = f"rate:{client_ip}"

        # Prune old entries
        window = now - self._window_seconds
        self._store[key] = [t for t in self._store.get(key, []) if t > window]

        if len(self._store.get(key, [])) >= self._max_requests:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "retry_after_seconds": self._window_seconds,
                },
            )

        self._store.setdefault(key, []).append(now)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Server"] = ""  # hide server identity
        return response


def configure_middleware(app: FastAPI) -> None:
    """Apply all hardening middleware to the FastAPI app.

    Order: security headers → CORS → size limit → rate limit → routes.
    """
    # Security headers (outermost)
    app.add_middleware(SecurityHeadersMiddleware)

    # CORS. allow_credentials=True combined with allow_origins=["*"] is a real
    # misconfiguration, not just a spec nit: Starlette's CORSMiddleware works
    # around the browser-side rejection of that combo by reflecting the
    # request's actual Origin header back verbatim, which — for a browser
    # that has stored credentials for this API — defeats origin restriction
    # entirely. This API authenticates via X-API-Key (a header the calling
    # JS sets explicitly), never cookies/TLS-client-certs/HTTP auth, so there
    # is nothing for allow_credentials to protect; it stays False so the
    # wildcard origin can't be paired with credentialed reflection.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    # Request size limit
    app.add_middleware(RequestSizeLimitMiddleware)

    # Rate limiting (100 req/min per IP)
    app.add_middleware(RateLimitMiddleware, max_requests=100, window_seconds=60)  # type: ignore[arg-type]

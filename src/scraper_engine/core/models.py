# core/models.py
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class SessionType(str, Enum):
    ASYNC = "async"
    STEALTHY = "stealthy"
    DYNAMIC = "dynamic"


class ProxyProtocol(str, Enum):
    HTTP = "HTTP"
    HTTPS = "HTTPS"
    SOCKS4 = "SOCKS4"
    SOCKS5 = "SOCKS5"


class AnonymityLevel(str, Enum):
    TRANSPARENT = "transparent"
    ANONYMOUS = "anonymous"
    ELITE = "elite"


class AsnClass(str, Enum):
    DATACENTER = "datacenter"
    RESIDENTIAL = "residential"
    MOBILE = "mobile"
    UNKNOWN = "unknown"


class Proxy(BaseModel):
    id: int
    ip: str
    port: int
    protocol: ProxyProtocol
    anonymity_level: AnonymityLevel = AnonymityLevel.TRANSPARENT
    asn_class: AsnClass = AsnClass.UNKNOWN
    reliability_score: float = Field(ge=0, le=100, default=50.0)
    # Round 40 — paid rotating-gateway providers (e.g. DataImpulse) authenticate
    # via username/password rather than being an anonymous free-list IP. Both
    # None for every proxy_pool-sourced Proxy; only set by proxy/paid_gateway.py.
    username: str | None = None
    password: str | None = None
    # Distinguishes a scored proxy_pool row from a paid gateway's synthetic
    # Proxy so callers (orchestrator/worker.py::_fetch_with_proxy) know not to
    # run pool-only bookkeeping (mark_success/mark_failure, domain bans,
    # lease_preflight) against a gateway that has no proxy_pool row at all.
    source: Literal["pool", "paid_gateway"] = "pool"

    def url(self) -> str:
        return f"{self.protocol.value.lower()}://{self.ip}:{self.port}"

    def auth_url(self) -> str:
        """Same as url() but with credentials embedded (user:pass@host:port),
        for consumers that take a single proxy string rather than a
        structured dict (Botasaurus). Identical to url() when unauthenticated."""
        if self.username is None or self.password is None:
            return self.url()
        return f"{self.protocol.value.lower()}://{self.username}:{self.password}@{self.ip}:{self.port}"

    def key(self) -> str:
        return f"{self.ip}:{self.port}"


class FailureCategory(str, Enum):
    NETWORK_TIMEOUT = "network_timeout"
    DETECTION_BLOCK = "detection_block"
    BROWSER_CRASH = "browser_crash"
    CAPTCHA_TRIGGERED = "captcha_triggered"
    PARSE_ERROR = "parse_error"
    PROXY_EXHAUSTED = "proxy_exhausted"
    CIRCUIT_OPEN = "circuit_open"
    SSRF_BLOCKED = "ssrf_blocked"
    QUOTA_EXCEEDED = "quota_exceeded"
    # DNS / unresolvable-host failures. Non-retryable: escalating L1→L2→L3 or
    # retrying a domain that doesn't resolve just wastes browser launches
    # (round 15 — surfaced by a dead test domain crashing through all levels).
    HOST_UNREACHABLE = "host_unreachable"
    # Round 43 — a definitive HTTP 404 is a URL-level fact, not a domain- or
    # proxy-level one: no fetcher variant, proxy, or browser render will ever
    # make a nonexistent page exist. Live-caught: sec.gov.ng's one URL in a
    # batch was a genuine 404, but with no dedicated category it fell
    # through as an untagged generic failure — escalated needlessly through
    # L2/L3 (each a wasted browser launch) AND penalized the domain's
    # circuit breaker exactly like a real proxy/network failure, even
    # though it says nothing about the domain's actual health. Non-
    # retryable and circuit-exempt for the same reason HOST_UNREACHABLE is
    # non-retryable above.
    NOT_FOUND = "not_found"


class FetchResult(BaseModel):
    url: str
    success: bool
    http_status: int | None = None
    is_challenge_page: bool = False
    html: str | None = None
    markdown: str | None = None
    extracted: dict[str, Any] | None = None
    level_used: int
    failure_category: FailureCategory | None = None
    error_message: str | None = None
    proxy_used: str | None = None
    # Round 49 — which proxy source actually served this attempt. None means
    # no proxy-bearing level ever ran (e.g. a pure L1 HTTP fetch, or a cache
    # hit). Lets orchestrator/worker.py's gateway-fallback logic tell "this
    # result already came from the paid gateway" apart from "this came from
    # the free pool" without re-deriving it from proxy_used's raw string.
    proxy_source: Literal["pool", "paid_gateway"] | None = None
    # Round 60 — raw CDP request/response events captured during a
    # Botasaurus fetch, opt-in via config.botasaurus.capture_network_events.
    # None means capture was off or no real browser fetch ran (e.g. a pure
    # L1 HTTP fetch, or a cache hit) — mirrors proxy_source's None meaning
    # just above.
    network_events: list[dict[str, Any]] | None = None
    html_snapshot_url: str | None = None
    from_cache: bool = False
    duration_ms: int
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConfigOverrides(BaseModel):
    max_retries: int = 3
    extraction_mode: str = "standard"  # standard | exhaustive
    timeout_seconds: int = 120
    respect_robots: bool = False
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    extraction_schema: dict[str, Any] | None = None
    # round 29 — skip the "reuse a recent result for this URL" cache check
    # (see Worker.process_job) and force a fresh scrape for this request.
    bypass_cache: bool = False
    # extraction-engine integration — only take effect when extraction_schema is
    # also set AND EXTRACTION_ENGINE_BASE_URL is configured (see Worker.process_job);
    # otherwise extraction falls back to AdaptiveSelector, which ignores both.
    extraction_enable_smallmodel: bool = False
    # Spends real operator money if extraction-engine's own EXTRACTION_LLM_API_KEY
    # is configured on that service — off by default for the same reason.
    extraction_enable_llm: bool = False


class ScrapeRequest(BaseModel):
    urls: list[HttpUrl]
    config_overrides: ConfigOverrides | None = None
    async_mode: bool = True
    webhook: HttpUrl | None = None

    @field_validator("urls")
    @classmethod
    def non_empty(cls, v: list[HttpUrl]) -> list[HttpUrl]:
        if not v:
            raise ValueError("urls must contain at least one entry")
        if len(v) > 500:
            raise ValueError("max 500 urls per job; use /v1/crawl for larger sets")
        return v


class CrawlRequest(BaseModel):
    """Bulk crawl request — for target sets too large for /v1/scrape's 500-URL
    cap. Runs a named Scrapy spider (services/scrapy_adapter.py) instead of
    the L1->L2->L3 escalation ladder; results land in scrape_results with
    level_used=0 as the "bulk crawl" sentinel."""

    spider_name: str
    start_urls: list[HttpUrl]
    webhook: HttpUrl | None = None

    @field_validator("start_urls")
    @classmethod
    def non_empty(cls, v: list[HttpUrl]) -> list[HttpUrl]:
        if not v:
            raise ValueError("start_urls must contain at least one entry")
        return v


class JobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD_LETTER = "DEAD_LETTER"


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: float | None = None
    results: list[FetchResult] | None = None
    error: str | None = None
    # True when status=COMPLETED but at least one URL DLQ'd rather than
    # succeeding (round 34) — status alone conflates "everything worked"
    # with "some URLs permanently/transiently failed but at least one
    # succeeded"; this field disambiguates without changing JobStatus's
    # existing values (avoids breaking any `status.value == "COMPLETED"`
    # check already relying on today's enum).
    partial_failure: bool = False


class JobSummaryResponse(BaseModel):
    """Lightweight per-job shape for GET /v1/jobs (round 56) — deliberately
    excludes `results`/`error`, which require the scrape_results join
    JobStatusResponse's single-job route already pays for; a list endpoint
    doing that per row would be an N+1 query."""

    job_id: str
    status: JobStatus
    url_count: int
    created_at: datetime
    updated_at: datetime


class DeadLetterEntryResponse(BaseModel):
    """API-facing shape of storage.dlq.DeadLetterEntry (a dataclass, not a
    BaseModel, so FastAPI needs a serializable response model)."""

    job_id: str
    url: str
    failure_category: FailureCategory
    error_message: str
    level_attempted: int
    # How many times proxy/dlq_reaper.py has auto-retried this entry (round
    # 34) — 0 for permanent-category entries, which are never auto-retried.
    auto_retry_count: int = 0
    enqueued_at: datetime
    dead_at: datetime

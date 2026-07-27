# core/models.py
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

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

    def url(self) -> str:
        return f"{self.protocol.value.lower()}://{self.ip}:{self.port}"

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

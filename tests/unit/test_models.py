# tests/unit/test_models.py
"""Pydantic domain model validation — spec §2."""

import pytest
from pydantic import HttpUrl, ValidationError

from scraper_engine.core.models import (
    CrawlRequest,
    FailureCategory,
    FetchResult,
    JobStatus,
    JobStatusResponse,
    Proxy,
    ProxyProtocol,
    ScrapeRequest,
)


class TestProxy:
    def test_url_generation(self) -> None:
        proxy = Proxy(
            id=1,
            ip="1.2.3.4",
            port=8080,
            protocol=ProxyProtocol.HTTP,
        )
        assert proxy.url() == "http://1.2.3.4:8080"

    def test_key_uniqueness(self) -> None:
        p1 = Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP)
        p2 = Proxy(id=2, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTPS)
        assert p1.key() == p2.key()  # same ip:port

    def test_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=150)
        with pytest.raises(ValidationError):
            Proxy(id=1, ip="1.2.3.4", port=8080, protocol=ProxyProtocol.HTTP, reliability_score=-10)


class TestScrapeRequest:
    def test_valid_request(self) -> None:
        sr = ScrapeRequest(urls=[HttpUrl("http://example.com")])
        assert len(sr.urls) == 1

    def test_empty_urls_rejected(self) -> None:
        with pytest.raises(ValidationError, match="urls must contain at least one entry"):
            ScrapeRequest(urls=[])

    def test_max_urls(self) -> None:
        urls = [HttpUrl(f"http://example.com/{i}") for i in range(501)]
        with pytest.raises(ValidationError, match="max 500 urls"):
            ScrapeRequest(urls=urls)


class TestCrawlRequest:
    def test_valid_request(self) -> None:
        cr = CrawlRequest(spider_name="titles", start_urls=[HttpUrl("http://example.com")])
        assert len(cr.start_urls) == 1

    def test_empty_start_urls_rejected(self) -> None:
        with pytest.raises(ValidationError, match="start_urls must contain at least one entry"):
            CrawlRequest(spider_name="titles", start_urls=[])


class TestFetchResult:
    def test_minimal_result(self) -> None:
        fr = FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=100)
        assert fr.success is True
        assert fr.level_used == 1

    def test_failed_result(self) -> None:
        fr = FetchResult(
            url="http://example.com",
            success=False,
            level_used=2,
            duration_ms=500,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
            error_message="timeout",
        )
        assert fr.success is False
        assert fr.failure_category == FailureCategory.NETWORK_TIMEOUT


class TestJobStatusResponse:
    def test_pending_job(self) -> None:
        resp = JobStatusResponse(job_id="abc-123", status=JobStatus.PENDING)
        assert resp.status == JobStatus.PENDING
        assert resp.results is None

    def test_completed_job(self) -> None:
        results = [
            FetchResult(url="http://example.com", success=True, level_used=1, duration_ms=100)
        ]
        resp = JobStatusResponse(
            job_id="abc-123",
            status=JobStatus.COMPLETED,
            results=results,
            progress=1.0,
        )
        assert resp.status == JobStatus.COMPLETED
        assert resp.results is not None and len(resp.results) == 1


class TestEnums:
    def test_failure_category_values(self) -> None:
        assert FailureCategory.NETWORK_TIMEOUT.value == "network_timeout"
        assert FailureCategory.SSRF_BLOCKED.value == "ssrf_blocked"
        assert FailureCategory.CIRCUIT_OPEN.value == "circuit_open"

    def test_enum_from_string(self) -> None:
        assert FailureCategory("network_timeout") == FailureCategory.NETWORK_TIMEOUT
        assert ProxyProtocol("HTTP") == ProxyProtocol.HTTP

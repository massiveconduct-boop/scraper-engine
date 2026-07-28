# tests/unit/test_webhook.py
"""Webhook dispatcher tests."""

from unittest.mock import patch

import pytest

from scraper_engine.core.models import JobStatus, JobStatusResponse
from scraper_engine.orchestrator.webhook import WebhookDispatcher


class TestWebhookDispatcher:
    @pytest.mark.asyncio
    async def test_deliver_success(self):
        dispatcher = WebhookDispatcher(max_retries=1, timeout_seconds=5)
        result = JobStatusResponse(job_id="test", status=JobStatus.COMPLETED)
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value.status_code = 200
            ok = await dispatcher.deliver("http://hooks.example.com/cb", result)
            assert ok is True

    @pytest.mark.asyncio
    async def test_deliver_failure(self):
        dispatcher = WebhookDispatcher(max_retries=1, timeout_seconds=5)
        result = JobStatusResponse(job_id="test", status=JobStatus.FAILED)
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value.status_code = 500
            ok = await dispatcher.deliver("http://hooks.example.com/cb", result)
            assert ok is False

    @pytest.mark.asyncio
    async def test_deliver_network_error(self):
        import httpx
        dispatcher = WebhookDispatcher(max_retries=1, timeout_seconds=5)
        result = JobStatusResponse(job_id="test", status=JobStatus.COMPLETED)
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("refused")):
            ok = await dispatcher.deliver("http://hooks.example.com/cb", result)
            assert ok is False

    @pytest.mark.asyncio
    async def test_deliver_retries_with_backoff(self):
        import httpx
        dispatcher = WebhookDispatcher(max_retries=3, timeout_seconds=5)
        result = JobStatusResponse(job_id="test", status=JobStatus.COMPLETED)
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("fail")):
            ok = await dispatcher.deliver("http://hooks.example.com/cb", result, retries=2)
            assert ok is False

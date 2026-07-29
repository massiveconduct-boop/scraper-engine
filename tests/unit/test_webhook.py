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
    async def test_deliver_sleeps_between_non_exception_failures(self):
        """A non-2xx response (no exception raised) must still back off before
        the next attempt when retries remain — distinct from the
        httpx.HTTPError path, which `continue`s past the backoff entirely."""
        dispatcher = WebhookDispatcher(max_retries=1, timeout_seconds=5)
        result = JobStatusResponse(job_id="test", status=JobStatus.FAILED)
        with (
            patch("httpx.AsyncClient.post") as mock_post,
            patch("asyncio.sleep") as mock_sleep,
        ):
            mock_post.return_value.status_code = 500
            ok = await dispatcher.deliver("http://hooks.example.com/cb", result, retries=2)
            assert ok is False
            mock_sleep.assert_awaited_once_with(1)  # 2**0
        assert mock_post.call_count == 2

    @pytest.mark.asyncio
    async def test_deliver_retries_with_backoff(self):
        import httpx

        dispatcher = WebhookDispatcher(max_retries=3, timeout_seconds=5)
        result = JobStatusResponse(job_id="test", status=JobStatus.COMPLETED)
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("fail")):
            ok = await dispatcher.deliver("http://hooks.example.com/cb", result, retries=2)
            assert ok is False

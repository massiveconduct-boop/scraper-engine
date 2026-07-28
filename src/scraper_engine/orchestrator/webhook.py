# orchestrator/webhook.py
"""Webhook delivery for async job completion notifications."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from scraper_engine.core.models import JobStatusResponse


class WebhookDispatcher:
    """Deliver job completion notifications to tenant-configured webhook URLs."""

    def __init__(self, max_retries: int = 3, timeout_seconds: int = 10) -> None:
        self._max_retries = max_retries
        self._timeout = timeout_seconds

    async def deliver(
        self, webhook_url: str, result: JobStatusResponse, retries: int | None = None
    ) -> bool:
        """POST the job result to the webhook URL. Returns True on success."""
        retries = retries or self._max_retries
        payload = result.model_dump_json()

        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        webhook_url,
                        content=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if 200 <= response.status_code < 300:
                        return True
            except httpx.HTTPError:
                continue

            if attempt < retries - 1:
                backoff = 2 ** attempt
                await asyncio.sleep(backoff)

        return False

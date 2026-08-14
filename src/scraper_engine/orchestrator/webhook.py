# orchestrator/webhook.py
"""Webhook delivery for async job completion notifications."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx

from scraper_engine.core.models import JobStatusResponse

if TYPE_CHECKING:
    from scraper_engine.config.schema import WebhookConfig


class WebhookDispatcher:
    """Deliver job completion notifications to tenant-configured webhook URLs.

    Retry/timeout defaults match config.schema.WebhookConfig's own defaults
    (round 34) so a caller that constructs this without an explicit config
    still gets sane, documented behavior — but production call sites should
    pass `config.webhook` explicitly rather than relying on these."""

    def __init__(
        self,
        max_retries: int = 3,
        timeout_seconds: int = 10,
        backoff_base_seconds: float = 2.0,
    ) -> None:
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._backoff_base = backoff_base_seconds

    @classmethod
    def from_config(cls, config: WebhookConfig) -> WebhookDispatcher:
        return cls(
            max_retries=config.max_retries,
            timeout_seconds=config.timeout_seconds,
            backoff_base_seconds=config.backoff_base_seconds,
        )

    async def deliver(
        self,
        webhook_url: str,
        result: JobStatusResponse | dict[str, object],
        retries: int | None = None,
    ) -> bool:
        """POST the payload to the webhook URL. Returns True on success.

        Accepts either a JobStatusResponse (legacy job-completion callers)
        or a pre-built dict (round 34 — orchestrator/slack_formatter.py
        produces Slack's own {"text": ..., "blocks": [...]} shape for
        hooks.slack.com targets, which isn't a JobStatusResponse at all)."""
        retries = retries or self._max_retries
        payload = (
            result.model_dump_json()
            if isinstance(result, JobStatusResponse)
            else json.dumps(result)
        )

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
                backoff = self._backoff_base**attempt
                await asyncio.sleep(backoff)

        return False

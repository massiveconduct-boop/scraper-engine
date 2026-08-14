# orchestrator/slack_formatter.py
"""Render a WebhookEvent for delivery (round 34).

Slack's Incoming Webhook API rejects arbitrary JSON — it expects
`{"text": ..., "blocks": [...]}`. Before this module, every webhook target
(Slack or otherwise) got a raw JobStatusResponse dump, which a real Slack
endpoint would 400 on, and that failure then vanished (see
orchestrator/webhook.py's old fire-and-forget behavior). is_slack_target()
lets the caller (orchestrator/webhook_sweeper.py) branch on URL shape so
non-Slack targets keep today's raw-JSON passthrough unchanged.
"""

from __future__ import annotations

from scraper_engine.orchestrator.webhook_events import WebhookEvent, WebhookEventType

_SLACK_HOST_MARKER = "hooks.slack.com"

_SEVERITY_EMOJI: dict[WebhookEventType, str] = {
    WebhookEventType.JOB_COMPLETED: ":white_check_mark:",
    WebhookEventType.JOB_FAILED: ":x:",
    WebhookEventType.JOB_PARTIAL_FAILURE: ":warning:",
    WebhookEventType.JOB_CANCELLED: ":no_entry_sign:",
    WebhookEventType.PROXY_POOL_DEGRADED: ":warning:",
    WebhookEventType.PROXY_POOL_CRITICAL: ":rotating_light:",
    WebhookEventType.PROXY_POOL_RECOVERED: ":white_check_mark:",
}


def is_slack_target(url: str) -> bool:
    return _SLACK_HOST_MARKER in url


def _summary_line(event: WebhookEvent) -> str:
    emoji = _SEVERITY_EMOJI.get(event.event_type, ":bell:")
    payload = event.payload

    if event.event_type in (
        WebhookEventType.JOB_COMPLETED,
        WebhookEventType.JOB_FAILED,
        WebhookEventType.JOB_PARTIAL_FAILURE,
        WebhookEventType.JOB_CANCELLED,
    ):
        detail = f"job `{event.job_id}` for tenant `{event.tenant_id}`"
        if event.event_type is WebhookEventType.JOB_PARTIAL_FAILURE:
            error = payload.get("error") or "one or more URLs failed"
            return f"{emoji} Scrape {detail} completed with partial failures: {error}"
        if event.event_type is WebhookEventType.JOB_FAILED:
            error = payload.get("error") or "unknown error"
            return f"{emoji} Scrape {detail} failed: {error}"
        if event.event_type is WebhookEventType.JOB_CANCELLED:
            return f"{emoji} Scrape {detail} was cancelled"
        return f"{emoji} Scrape {detail} completed successfully"

    tier = payload.get("tier")
    count = payload.get("validated_count")
    if event.event_type is WebhookEventType.PROXY_POOL_RECOVERED:
        return f"{emoji} Proxy pool tier {tier} recovered — {count} validated proxies"
    state = "CRITICAL" if event.event_type is WebhookEventType.PROXY_POOL_CRITICAL else "DEGRADED"
    return f"{emoji} Proxy pool tier {tier} is {state} — only {count} validated proxies"


def to_slack_payload(event: WebhookEvent) -> dict[str, object]:
    """Render a WebhookEvent into Slack's Block Kit shape. `text` is always
    set too (Slack uses it for notifications/fallback rendering when blocks
    can't render, e.g. in a thread reply preview)."""
    text = _summary_line(event)
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"event: `{event.event_type.value}` · "
                            f"{event.created_at.isoformat()}"
                        ),
                    }
                ],
            },
        ],
    }


def render_for_target(event: WebhookEvent, target_url: str) -> dict[str, object]:
    """Single entry point orchestrator/webhook_sweeper.py calls — Slack
    shape for a Slack target, raw event dict (backward-compatible with the
    pre-round-34 JobStatusResponse dump) otherwise."""
    if is_slack_target(target_url):
        return to_slack_payload(event)
    return event.model_dump(mode="json")

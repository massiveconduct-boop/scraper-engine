# tests/unit/test_slack_formatter.py
"""slack_formatter tests — Slack Block Kit rendering vs raw-JSON passthrough."""

from scraper_engine.orchestrator.slack_formatter import (
    is_slack_target,
    render_for_target,
    to_slack_payload,
)
from scraper_engine.orchestrator.webhook_events import WebhookEvent, WebhookEventType


def make_event(event_type, payload=None, job_id="job-1"):
    return WebhookEvent(
        event_type=event_type,
        tenant_id="system",
        job_id=job_id,
        payload=payload or {},
    )


class TestIsSlackTarget:
    def test_detects_slack_incoming_webhook_url(self):
        assert is_slack_target("https://hooks.slack.com/services/T00/B00/XXX") is True

    def test_generic_url_is_not_slack(self):
        assert is_slack_target("https://example.com/webhook") is False


class TestToSlackPayload:
    def test_has_text_and_blocks(self):
        event = make_event(WebhookEventType.JOB_COMPLETED)
        payload = to_slack_payload(event)
        assert "text" in payload
        assert isinstance(payload["blocks"], list)
        assert len(payload["blocks"]) == 2

    def test_partial_failure_message_mentions_partial_failure(self):
        event = make_event(
            WebhookEventType.JOB_PARTIAL_FAILURE, payload={"error": "1 URL exhausted"}
        )
        payload = to_slack_payload(event)
        assert "partial failure" in payload["text"].lower()
        assert "1 URL exhausted" in payload["text"]

    def test_job_failed_message_includes_error(self):
        event = make_event(WebhookEventType.JOB_FAILED, payload={"error": "internal error"})
        payload = to_slack_payload(event)
        assert "failed" in payload["text"].lower()
        assert "internal error" in payload["text"]

    def test_job_cancelled_message(self):
        event = make_event(WebhookEventType.JOB_CANCELLED)
        payload = to_slack_payload(event)
        assert "cancelled" in payload["text"].lower()

    def test_pool_critical_message_names_tier_and_count(self):
        event = make_event(
            WebhookEventType.PROXY_POOL_CRITICAL,
            payload={"tier": 3, "validated_count": 2},
            job_id=None,
        )
        payload = to_slack_payload(event)
        assert "CRITICAL" in payload["text"]
        assert "tier 3" in payload["text"]
        assert "2" in payload["text"]

    def test_pool_recovered_message(self):
        event = make_event(
            WebhookEventType.PROXY_POOL_RECOVERED,
            payload={"tier": 1, "validated_count": 50},
            job_id=None,
        )
        payload = to_slack_payload(event)
        assert "recovered" in payload["text"].lower()


class TestRenderForTarget:
    def test_slack_target_gets_slack_shape(self):
        event = make_event(WebhookEventType.JOB_COMPLETED)
        rendered = render_for_target(event, "https://hooks.slack.com/services/T00/B00/XXX")
        assert "blocks" in rendered

    def test_generic_target_gets_raw_event_dict(self):
        event = make_event(WebhookEventType.JOB_COMPLETED)
        rendered = render_for_target(event, "https://example.com/webhook")
        assert "blocks" not in rendered
        assert rendered["event_type"] == "job.completed"
        assert rendered["job_id"] == "job-1"

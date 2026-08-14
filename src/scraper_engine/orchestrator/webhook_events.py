# orchestrator/webhook_events.py
"""Notification event taxonomy (round 34).

Before this, "webhook" meant exactly one thing: a raw JobStatusResponse
POSTed once at whole-job completion. That conflated two different kinds of
signal (a tenant's job finished; the shared proxy pool's health changed) into
one channel, and had no representation at all for the second kind. WebhookEvent
is the single shape both orchestrator/tasks.py (per-job) and
proxy/harvester_daemon.py (pool health) now produce; storage/webhook_outbox.py
persists it, orchestrator/webhook_sweeper.py delivers it, and
orchestrator/slack_formatter.py renders it for Slack targets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WebhookEventType(str, Enum):
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    # Distinct from JOB_COMPLETED (round 34) — status says COMPLETED but at
    # least one URL DLQ'd. See JobStatusResponse.partial_failure.
    JOB_PARTIAL_FAILURE = "job.partial_failure"
    JOB_CANCELLED = "job.cancelled"
    # Operator-facing, not scoped to any one tenant's job — see
    # proxy/pool_health.py::PoolHealthTransition.
    PROXY_POOL_DEGRADED = "proxy_pool.degraded"
    PROXY_POOL_CRITICAL = "proxy_pool.critical"
    PROXY_POOL_RECOVERED = "proxy_pool.recovered"


class WebhookEvent(BaseModel):
    event_type: WebhookEventType
    tenant_id: str
    # None for pool-health events — those aren't scoped to one job.
    job_id: str | None = None
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

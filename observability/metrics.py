"""Application-side Prometheus metrics.

Single source of truth for validated proxy count: count_validated_proxies().
Called by both /metrics endpoint (api/routes.py) and harvester daemon
(proxy/harvester.py). No duplicate query — one function, one SQL string.

Round 25: the rq worker process that actually runs scrape jobs
(orchestrator/, proxy/, browser/) is NOT the same process that serves
/metrics (api/). Worse, rq forks a brand-new "work horse" process per job
that exits via os._exit() immediately after — nothing scrapes it, and
anything held only in its in-process prometheus_client REGISTRY is gone the
instant the job finishes. An in-process Counter/Gauge set inside that
process would silently never reach Prometheus at all — the same "looks
wired, does nothing" failure mode this round exists to close. So every
metric whose event happens in the worker process is instead written to a
plain Redis/Postgres counter at event time, and refreshed into the *local*
Gauge only when /metrics is actually scraped (same pattern already
established below for proxy_pool_validated_count). http_requests_total is
the one exception — HTTP requests and /metrics scrapes both happen in the
same long-lived API process, so it can be a normal in-process Counter.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, Counter, Gauge

if TYPE_CHECKING:
    from core.tenant import TenantId
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient

proxy_pool_validated_count = Gauge(
    "proxy_pool_validated_count",
    "Number of proxies with reliability_score >= 40 (L1 threshold)",
    registry=REGISTRY,
)

safe_content_none_total = Counter(
    "safe_content_none_total",
    "Number of times Level3Fetcher._safe_content returned None "
    "(page.content() raised mid-navigation — guard fired, loop kept polling)",
    registry=REGISTRY,
)

captcha_solve_attempts_total = Counter(
    "captcha_solve_attempts_total",
    "In-page CAPTCHA solve attempts — a solvable widget was detected on a "
    "challenge page and a token solve was requested from the provider.",
    ["kind"],
    registry=REGISTRY,
)

captcha_solved_total = Counter(
    "captcha_solved_total",
    "In-page CAPTCHA solves that produced a token and injected it into the "
    "page (does not assert the site accepted it — only that injection ran).",
    ["kind"],
    registry=REGISTRY,
)

captcha_provider_configured = Gauge(
    "captcha_provider_configured",
    "1 if a CAPTCHA provider API key is present in the environment, else 0. "
    "Configured != verified: a key can be present but rejected (inactive "
    "capability / 401). Run tools/validate_captcha_keys.py to confirm the key "
    "is actually accepted by the provider.",
    ["provider"],
    registry=REGISTRY,
)


async def count_validated_proxies(pg: PostgresClient, tenant: TenantId) -> int:
    """Return count of proxies with reliability_score >= 40.

    Single source of truth — used by both the /metrics endpoint and the
    harvester daemon. No other file contains this query.
    """
    rows = await pg.fetch(
        tenant,
        "SELECT COUNT(*) as n FROM proxy_pool WHERE reliability_score >= 40",
    )
    return rows[0]["n"] if rows else 0


dlq_size = Gauge(
    "dlq_size",
    "Current total dead_letter_queue rows across every tenant schema",
    registry=REGISTRY,
)

capsolver_daily_spend = Gauge(
    "capsolver_daily_spend",
    "Current CapSolver daily spend per tenant",
    ["tenant_id"],
    registry=REGISTRY,
)

capsolver_daily_ceiling = Gauge(
    "capsolver_daily_ceiling",
    "Current CapSolver daily ceiling per tenant (tenants.capsolver_daily_credit_ceiling)",
    ["tenant_id"],
    registry=REGISTRY,
)

circuit_breaker_trips_total = Gauge(
    "circuit_breaker_trips_total",
    "Cumulative circuit breaker opens across all domains (Redis-backed counter, "
    "no per-domain label — Redis has no cheap 'list all domains' enumeration)",
    registry=REGISTRY,
)

proxy_exhausted_total = Gauge(
    "proxy_exhausted_total",
    "Cumulative proxy pool exhaustions per fetch level (Redis-backed counter)",
    ["level"],
    registry=REGISTRY,
)

job_duration_seconds_count = Gauge(
    "job_duration_seconds_count",
    "Cumulative count of completed scrape jobs per status (Redis-backed counter)",
    ["status"],
    registry=REGISTRY,
)

job_duration_seconds_sum = Gauge(
    "job_duration_seconds_sum",
    "Cumulative wall-clock seconds spent on completed scrape jobs per status",
    ["status"],
    registry=REGISTRY,
)

http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests served by the API",
    ["method", "route", "status"],
    registry=REGISTRY,
)

_JOB_STATUSES = ("completed", "failed")
_PROXY_LEVELS = ("1", "2", "3")


async def refresh_dlq_size(pg: PostgresClient) -> None:
    """Sum dead_letter_queue rows across every tenant — one tenant's schema
    being unreachable must not blank out the count for every other tenant,
    same isolation contract as proxy/retention_reaper.py."""
    from core.tenant import TenantId

    system = TenantId("system")
    total = 0
    tenants = await pg.fetch(system, "SELECT tenant_id FROM public.tenants")
    for row in tenants:
        try:
            tenant = TenantId(row["tenant_id"])
            rows = await pg.fetch(tenant, "SELECT COUNT(*) AS n FROM dead_letter_queue")
            total += rows[0]["n"] if rows else 0
        except Exception:
            continue
    dlq_size.set(total)


async def refresh_capsolver_spend(pg: PostgresClient, redis: RedisClient) -> None:
    """Read each tenant's ceiling from Postgres and current spend from the
    Redis key core.budget.CapSolverBudget actually writes to."""
    from core.tenant import TenantId

    system = TenantId("system")
    rows = await pg.fetch(
        system, "SELECT tenant_id, capsolver_daily_credit_ceiling FROM public.tenants"
    )
    for row in rows:
        tenant_id = str(row["tenant_id"])
        ceiling = (
            float(row["capsolver_daily_credit_ceiling"])
            if row["capsolver_daily_credit_ceiling"] is not None
            else 1.0
        )
        spend_raw = await redis.raw.get(f"capsolver:daily_spend:{tenant_id}")
        spend = float(spend_raw) if spend_raw else 0.0
        capsolver_daily_spend.labels(tenant_id=tenant_id).set(spend)
        capsolver_daily_ceiling.labels(tenant_id=tenant_id).set(ceiling)


async def refresh_proxy_source_health(redis: RedisClient) -> None:
    """Refresh proxy_source_healthy from the Redis keys
    proxy/source_health.py::record_source_health writes at harvest time (in
    the separate proxy-harvester process — see that module's docstring)."""
    from proxy.harvester import ProxyHarvester
    from proxy.source_health import REDIS_KEY_PREFIX, proxy_source_healthy

    for source_name, _url, _fmt in ProxyHarvester.SOURCES:
        raw = await redis.raw.get(f"{REDIS_KEY_PREFIX}{source_name}")
        if raw is not None:
            proxy_source_healthy.labels(source_name=source_name).set(float(raw))


async def refresh_redis_backed_counters(redis: RedisClient) -> None:
    """Refresh the scrape-time gauges backed by plain Redis counters that
    orchestrator/circuit_breaker.py, proxy/manager.py, and
    orchestrator/tasks.py increment at event time from the (separate,
    short-lived) rq worker process."""
    trips_raw = await redis.raw.get("metrics:circuit_breaker_trips_total")
    circuit_breaker_trips_total.set(float(trips_raw) if trips_raw else 0.0)

    for level in _PROXY_LEVELS:
        raw = await redis.raw.get(f"metrics:proxy_exhausted_total:{level}")
        proxy_exhausted_total.labels(level=level).set(float(raw) if raw else 0.0)

    for status in _JOB_STATUSES:
        count_raw = await redis.raw.get(f"metrics:job_duration:{status}:count")
        sum_raw = await redis.raw.get(f"metrics:job_duration:{status}:sum")
        job_duration_seconds_count.labels(status=status).set(
            float(count_raw) if count_raw else 0.0
        )
        job_duration_seconds_sum.labels(status=status).set(
            float(sum_raw) if sum_raw else 0.0
        )

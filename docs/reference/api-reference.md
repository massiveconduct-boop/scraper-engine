# Scraper Engine — API Reference

Base URL: `http://localhost:8000` | OpenAPI: `/openapi.json` (v3.1.0) | Swagger UI: `/docs`

## Authentication

All endpoints except `/v1/health` require an API key header:

```
X-API-Key: sk-<api_key>
```

API keys are generated at tenant creation (BD-04, `scraper-engine create-tenant <slug>`).
The key resolves to a `TenantId` which scopes all storage, quota, and proxy operations.

## Idempotent retries

`POST /v1/scrape` and `POST /v1/crawl` accept an optional repeat-safe key:

```
Idempotency-Key: <any string>
```

If the same key is sent again while the original job is still live (not
`FAILED`/`CANCELLED`/`DEAD_LETTER`), the original job is returned as-is — no
new job, no additional quota charge. A retry after the original job reached
one of those dead states starts a fresh job under the same key.

## Endpoints

### `POST /v1/scrape`

Submit URLs for scraping. Returns immediately with a `job_id` for async polling.

**Request:**
```json
{
  "urls": ["https://example.com/page"],
  "config_overrides": {
    "max_retries": 3,
    "extraction_mode": "standard",
    "timeout_seconds": 120,
    "respect_robots": false,
    "include_tags": ["article", "main"],
    "extraction_schema": {"title": "h1::text", "body": "article p::text"},
    "bypass_cache": false
  },
  "async_mode": true,
  "webhook": "https://your-app.com/callbacks/scrape"
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `urls` | string[] | yes | — | 1-500 URLs to scrape |
| `config_overrides.max_retries` | int | no | 3 | Retry attempts per level |
| `config_overrides.extraction_mode` | string | no | `standard` | `standard` or `exhaustive` |
| `config_overrides.timeout_seconds` | int | no | 120 | Per-URL timeout |
| `config_overrides.respect_robots` | bool | no | false | Respect robots.txt |
| `config_overrides.bypass_cache` | bool | no | false | Skip the cache reuse check below and force a fresh scrape |
| `config_overrides.min_level` | int | no | — | Start the L1→L2→L3 ladder at this level (1-3). Use when you already know the target needs a real browser, instead of paying two doomed attempts to rediscover it. Overrides the learned per-domain hint in both directions |
| `config_overrides.max_level` | int | no | — | Never escalate past this level (1-3). `max_level: 1` means "never spend a browser render on this". Must be >= `min_level` |
| `config_overrides.politeness_concurrency` | int | no | — | Concurrent fetches allowed against one domain for this job. Clamped server-side to the operator's `politeness.max_request_concurrency` |
| `config_overrides.politeness_delay_seconds` | float | no | — | Minimum delay between successive fetches of one domain. Clamped UP to the operator's `politeness.min_request_delay_seconds` — a request can never go below the configured floor |
| `async_mode` | bool | no | true | Async job processing |
| `webhook` | string | no | — | POST callback URL on completion. SSRF-checked the same way scrape target URLs are — a webhook pointed at a private/internal address is rejected with `403` before the job is created, it is not silently dropped |

**Caching:** before actually fetching a URL, a successful scrape of that
exact URL for this tenant within the last 7 days is reused instead of
re-fetching — no proxy/browser cost, no quota charge for that URL. Each
reuse refreshes the 7-day window. Set `config_overrides.bypass_cache: true`
on a request to force a fresh scrape regardless.

**Response:** `200 OK`
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "urls": 1,
  "blocked_urls": 0,
  "tenant": "acme"
}
```

**SSRF handling (round 33):** each URL is checked individually against
private/internal ranges — 1 bad address in a batch no longer rejects the
whole request. Only when *every* URL in the batch is blocked does this
endpoint return `403`; otherwise the job proceeds with the valid URLs
(`blocked_urls` in the response tells you how many were dropped), quota is
only charged for the valid ones, and each blocked URL still gets its own
`failure_category: "ssrf_blocked"` entry in `GET /v1/jobs/{job_id}` — same
as any other per-URL failure, not a silent drop.

**Errors:**
| Status | Condition |
|---|---|
| `400` / `422` | Validation error (bad body shape, >500 URLs) |
| `403` | Every URL in the batch was SSRF blocked (private/internal IP), or the `webhook` URL itself was SSRF blocked (rejects the whole request — there's only one webhook, unlike the per-URL batch handling above) |
| `413` | Request body > 1 MB |
| `429` | Quota exceeded or rate limit exceeded (100 req/min per IP) — carries a `Retry-After` header |
| `503` | Engine not fully started (database, Redis or job queue unavailable) — nothing was saved, charged or queued; safe to retry. Also returned when the job was saved but could not be queued (it is marked `FAILED`) |

---

### `POST /v1/crawl`

Bulk Scrapy crawl for target sets larger than `/v1/scrape`'s 500-URL cap.
Same auth, `Idempotency-Key`, and error shape as `/v1/scrape`, including the
same per-URL (not whole-batch) SSRF handling — a blocked seed URL is
dropped from `start_urls` before the crawl runs and recorded as its own
`ssrf_blocked` entry in `GET /v1/jobs/{job_id}`, same as `/v1/scrape`.
Every request the crawl then sends — including each redirect hop — is
SSRF-checked again inside the crawl, so a public seed that redirects to a
private address is dropped at that hop.

The crawl goes out through one leased proxy, chosen the same way as for
`/v1/scrape`: the free pool first, the paid gateway when the pool is
exhausted (if the deployment enables it), never directly from the server.
Each result carries `proxy_used` and `proxy_source`. A URL that appears
twice (or two seeds that land on the same final URL) is returned once.

```json
{
  "spider_name": "titles",
  "start_urls": ["https://example.com"],
  "webhook": "https://your-app.com/callbacks/crawl"
}
```

---

### `GET /v1/jobs`

List jobs for the calling tenant — schema-per-tenant search_path already
scopes this to the caller's own jobs, same isolation as
`GET /v1/jobs/{job_id}`. Ordered newest-first.

**Query params:** `status` (optional, must be a valid job status or `422`),
`limit` (default 50, capped at 500 — same per-request cap as
`POST /v1/scrape`'s URL list), `offset` (default 0)

**Response:** `200 OK`
```json
{
  "jobs": [
    {
      "job_id": "550e8400-e29b-41d4-a716-446655440000",
      "status": "COMPLETED",
      "url_count": 3,
      "created_at": "2026-08-16T12:00:00Z",
      "updated_at": "2026-08-16T12:00:07Z"
    }
  ],
  "limit": 50,
  "offset": 0,
  "count": 1
}
```

---

### `GET /v1/jobs/{job_id}`

Poll job status and retrieve results. `results` includes both successful
and failed URLs, each with its own `failure_category`/`error_message` —
a partial failure never silently disappears. `progress` is a real fraction
(URLs completed / total URLs), not an estimate.

**Results stream — you do not have to wait for the job to finish.** Rows are
written one per URL as each completes, and this endpoint returns whatever has
landed regardless of `status`, so a `PROCESSING` job already returns its
finished URLs and a real `progress`.

**Query parameters**

| Param | Type | Description |
|---|---|---|
| `since` | ISO-8601 timestamp | Return only results extracted strictly after this instant. Pass the newest `fetched_at` from your previous poll to fetch just what is new instead of re-downloading the whole result set each time. `progress` and `partial_failure` always reflect the WHOLE job, never just the returned window |

**Response:** `200 OK`
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "COMPLETED",
  "progress": 1.0,
  "results": [
    {
      "url": "https://example.com/page",
      "success": true,
      "http_status": 200,
      "is_challenge_page": false,
      "markdown": "# Example...",
      "extracted": {"title": "Example", "body": "..."},
      "level_used": 1,
      "failure_category": null,
      "error_message": null,
      "proxy_used": "1.2.3.4:8080",
      "html_snapshot_url": "snapshots/acme/550e8400.../20260729T120000.html",
      "from_cache": false,
      "proxy_source": "pool",
      "duration_ms": 234,
      "timings": {
        "cache_check_ms": 4,
        "politeness_wait_ms": 1759,
        "slot_wait_ms": 0,
        "level_1_ms": 235,
        "level_2_ms": 23427,
        "level_3_ms": 18897,
        "extract_ms": 107,
        "markdown_ms": 314,
        "total_ms": 70908
      },
      "escalations": [
        {"level": 1, "reason": "status:403", "http_status": 403,
         "engine": null, "proxy_source": "pool"}
      ],
      "fetched_at": "2026-07-21T12:00:00Z"
    }
  ],
  "error": null,
  "partial_failure": false,
  "queued_ms": 875,
  "runtime_ms": 80644
}
```

**`timings`, `queued_ms`, `runtime_ms` — where the time actually went.**
`duration_ms` is a single number written by whichever escalation level
finally returned, so it cannot distinguish "the fetch is slow" from "the
fetch was fine and everything around it was slow". `timings` breaks one
URL down by phase in milliseconds; `queued_ms` is how long the job waited
in the queue before a worker picked it up, and `runtime_ms` how long it
then ran (both `null` until the corresponding transition has happened).

A level that was skipped simply has no `level_N_ms` key — that is how you
see the engine's per-domain level memory working. Once a domain is known to
need a real browser, later URLs for it start at that level instead of
re-paying the attempts that already failed, so their timings show only
`level_3_ms`. Pin the ladder explicitly with `config_overrides.min_level` /
`max_level` if you want to override that.

With host-wide browser admission enabled (operator setting, off by
default), browser levels wait for a seat, the site's politeness slot and
its delay together: that wait is `admission_wait_ms`, and `level_N_ms` is
then render time only. `politeness_wait_ms` / `slot_wait_ms` appear only
for levels that did not go through admission (always L1).

`display_lock_wait_ms` is how long this URL's browsers queued behind other
browsers opening or closing in the same worker process (each one's virtual
display is started and torn down one at a time). It is part of the render
time above, and absent when there was no wait.

**`escalations` — why each earlier attempt was not the answer.** One
entry per level (or per same-level attempt) the engine rejected before the
result you got, in order; `null` when the first attempt succeeded.
`reason` is the exact check that fired — `status:403`,
`signature:<text>` (a known challenge-page marker), `gateway_error`,
`chromium_net_error`, `js_gated`, or `failure:<category>` — and `engine`
names what produced it inside a level (`botasaurus` or `camoufox` at L2).
When a free-pool proxy is blocked the engine retries that same level once
through the paid gateway (if enabled) before moving up; that retry's time
is `level_N_gateway_retry_ms` in `timings`, and the blocked attempt appears
here with `proxy_source: "pool"`.

**Reading a failed result.** A failed URL's `error_message` says what
happened. Two more fields help:
- **`block_reason`** is set on a `detection_block` or `rate_limited`. It
  names which check fired, in the same vocabulary as
  `escalations[].reason`: `status:403`, `status:429`, `signature:<text>`,
  `js_gated`, … For either category the message starts with a plain summary of what, where and through which
  route: `HTTP 403 (refused) at L3 via pool — …`, `HTTP 429 (rate limited)
  at L2 via pool — …`, `challenge page (signature 'cf-browser-verification')
  at L3 via pool — …`. The route is `pool` (free proxy), `paid_gateway`, or
  `direct` (L1, no proxy).
- **`paid_gateway_skipped: true`** means the engine would normally have
  sent this URL through the paid gateway, but the gateway was refusing the
  engine's credentials. That covers the block retry, a site known to refuse
  free proxies, an open circuit and an exhausted pool. The failure then
  reflects the free route only, not the site's full answer. The message also
  ends with `(paid gateway is refusing our credentials …)`, and callers may
  match on that exact text.

Both fields are `null` when they don't apply. `http_status` is the last
attempt's real status, including on terminal blocks.

**Failure categories.** One line each on what the category means, whose
doing it is, and whether the engine re-drives it by itself (see `GET
/v1/jobs/{job_id}/dlq`):

| `failure_category` | Meaning | Whose doing | Auto-retried |
|---|---|---|---|
| `detection_block` | The site answered 401/403/404/405/410, or served a challenge / JavaScript-gated page, at every level tried (a page that renders with one of those statuses counts too). `block_reason` says which | Target site (maybe only towards free proxies: check `paid_gateway_skipped`) | No |
| `rate_limited` | The site answered 429 ("too many requests") at every level and route tried. `block_reason` is `status:429` | Target site, asking us to slow down (maybe per exit IP) | Yes, after a wait, and not before the site's `Retry-After` (capped at one hour) |
| `circuit_open` | Too many recent failures on this domain; the engine is pausing it | Target site, by history | Yes, once the circuit closes |
| `proxy_exhausted` | No usable proxy was available for the level | Ours (proxy supply) | Yes, when that pool tier is healthy |
| `proxy_auth_failed` | A proxy refused the engine's credentials. From the paid gateway, that means the account (plan out of traffic, bad login) | Proxy provider / account | Yes: `paid_only` after a gateway probe succeeds, otherwise on pool health |
| `browser_crash` | The browser or the page load failed without a site verdict (includes proxy connection errors) | Ours or the proxy | Yes, on pool health |
| `network_timeout` | The request failed or timed out at the network level (L1's default for any fetch error) | The proxy or the site, undetermined | Yes, on pool health |
| `host_unreachable` | The domain does not resolve (checked directly, without a proxy) | Target (dead domain) | No |
| `ssrf_blocked` | The URL resolves to a private or denied network | Caller's input | No |
| `quota_exceeded` | The tenant's quota ran out | Caller's account | No |
| `politeness_timeout` | No politeness slot for this site came free in time (other URLs of the same site were busy) | Ours (contention) | Yes, with backoff |
| `capacity_timeout` | No browser capacity on this host came free in time | Ours (capacity) | Yes, with backoff |
| `dependency_unavailable` | The engine's own Redis failed during the fetch | Ours (infrastructure) | Yes, with backoff |
| `parse_error` | Anything unexpected inside the engine while handling this URL | Ours (a bug) | No |
| `captcha_triggered` | Defined but never assigned today: an unsolved CAPTCHA ends as `detection_block` (`block_reason` `signature:h-captcha`, …) | — | No |
| `not_found` | Historical rows only (rounds 43-44); nothing assigns it now | — | No |

**`partial_failure`:** `true` when `status` is `COMPLETED` but at least
one URL in this job landed in the dead-letter queue alongside a
succeeded one — `COMPLETED` alone only ever meant "at least one URL
succeeded," not "every URL succeeded." Check this field, not just
`status`, before treating a job as a fully clean run — the same value is
included in the webhook payload for `job.completed`/`job.partial_failure`
notifications.

**What you actually get back — 3 distinct fields, none of them raw HTML
inline:**
- `extracted` — title + main body text + links, always populated
  (`AdaptiveSelector`, or a schema-driven extractor if
  `config_overrides.extraction_schema` was set).
- `markdown` — always populated (round 33). Uses Firecrawl for the
  conversion when configured (`FIRECRAWL_API_KEY`/`FIRECRAWL_BASE_URL` in
  `.env.example`); otherwise falls back to a local HTML→Markdown converter,
  so this field is never left `null` waiting on an optional external tool.
  Independent of `extracted` — a caller who only wants clean markdown
  (e.g. to hand to their own extraction model) can read this field alone.
- `html_snapshot_url` — a pointer to the full raw HTML in object storage
  (S3/MinIO), not the HTML itself inline in this response. Fetch that URL
  separately if you need the exact original page. Raw HTML is deliberately
  never embedded in `GET /v1/jobs/{job_id}`'s JSON — a single large page
  would otherwise bloat every job-status response, including ones the
  caller only polls for progress.

**Status values:**
| Status | Meaning |
|---|---|
| `PENDING` | Job enqueued, not yet processing |
| `PROCESSING` | Worker is actively fetching |
| `COMPLETED` | At least one URL succeeded — check `partial_failure` to know whether *every* URL did |
| `FAILED` | No URL succeeded |
| `CANCELLED` | Job cancelled via `DELETE /v1/jobs/{job_id}` |
| `DEAD_LETTER` | Reserved for future use — not currently set by any code path |

---

### `GET /v1/jobs/{job_id}/dlq`

Raw dead-letter detail for one job — the same failed URLs already appear in
`GET /v1/jobs/{job_id}`'s `results`, but this endpoint additionally exposes
`enqueued_at`/`dead_at` timestamps from the dead-letter queue, plus
`auto_retry_count` for entries in a self-healing category (see below).

**Response:** `200 OK`
```json
[
  {
    "job_id": "550e8400-e29b-41d4-a716-446655440000",
    "url": "https://blocked.example.com",
    "failure_category": "proxy_exhausted",
    "error_message": "All fetch levels exhausted",
    "level_attempted": 3,
    "auto_retry_count": 0,
    "enqueued_at": "2026-07-21T12:00:00Z",
    "dead_at": "2026-07-21T12:00:05Z"
  }
]
```

**Auto-retry:** these entries are automatically re-enqueued once the
condition that caused them clears, up to a configured cap tracked in
`auto_retry_count`:

| `failure_category` | Retried when |
|---|---|
| `proxy_exhausted`, `browser_crash`, `network_timeout` | the proxy pool tier for that level is healthy again |
| `circuit_open` | the domain's circuit breaker has closed |
| `proxy_auth_failed` | a proxy refused the engine's credentials. With `strategy: paid_only`, once a test request through the paid gateway succeeds again (e.g. after a plan top-up). Otherwise the retry goes through the free pool, so once that level's pool tier is healthy |
| `politeness_timeout` | nothing holds a politeness slot on that domain any more |
| `capacity_timeout` | the host has spare browser capacity (nobody waiting, seats free) |
| `dependency_unavailable` | the engine's own Redis answers again |
| `rate_limited` | the domain's circuit breaker is not open, and the time the site's `Retry-After` header asked for (capped at one hour) has passed, if it sent one |

The last four also wait 60s × 2^`auto_retry_count` after their most
recent failure. A retried job keeps its original `job_id`, and only its
not-yet-successful URLs are fetched again; poll `GET /v1/jobs/{job_id}` to
see it move through `PENDING`/`PROCESSING` again. Every other
`failure_category` (`ssrf_blocked`, `quota_exceeded`, `host_unreachable`,
`detection_block`, `parse_error`, …) is never auto-retried.

---

### `GET /v1/dlq`

Tenant-wide dead-letter listing — the caller-facing sibling of
`GET /v1/jobs/{job_id}/dlq`, covering every dead URL across all of the
tenant's jobs instead of requiring the caller to already know one job's id.
Same entry shape, same auto-retry semantics as above.

**Query params:** `limit` (default 100), `offset` (default 0)

**Response:** `200 OK` — a JSON array of `DeadLetterEntryResponse`, same
shape as `GET /v1/jobs/{job_id}/dlq`'s response above.

---

### `GET /v1/quota`

Remaining daily quota for the calling tenant — lets a caller check its
limit proactively instead of discovering it by hitting a `429` on
`POST /v1/scrape`.

**Response:** `200 OK`
```json
{
  "tenant": "acme-corp",
  "daily_limit": 1000,
  "used": 342,
  "remaining": 658,
  "resets_in_seconds": 41273
}
```

---

### `GET /v1/webhook-events`

Webhook event taxonomy and payload schema — lets a caller wiring up a
webhook receiver discover every `WebhookEventType` value and the full
`WebhookEvent` JSON schema without reading this repo's source. Pure static
reflection: no database or Redis touch, and (unlike every other `/v1`
route) nothing tenant-specific to fail on once the API key itself checks
out.

**Response:** `200 OK`
```json
{
  "event_types": [
    "job.completed",
    "job.failed",
    "job.partial_failure",
    "job.cancelled",
    "proxy_pool.degraded",
    "proxy_pool.critical"
  ],
  "payload_schema": { "...": "JSON Schema for WebhookEvent" }
}
```

---

### `DELETE /v1/jobs/{job_id}`

Cancel a `PENDING`/`PROCESSING` job. Best-effort: a still-queued job is
removed from the queue outright; an in-flight job stops cooperatively
between URLs (results already recorded for prior URLs in the job are kept,
not rolled back).

**Response:** `200 OK`
```json
{"job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "CANCELLED"}
```

**Errors:** `404` (job not found), `409` (job already in a terminal state)

---

### `GET /v1/health`

Composite health check. No authentication required.

**Response:** `200 OK`
```json
{
  "status": "ok",
  "proxy_pool_size": 42,
  "pgbouncer_reachable": true,
  "redis_reachable": true,
  "s3_reachable": true,
  "daemons": {"proxy-harvester": "healthy", "dlq-reaper": "healthy",
              "webhook-sweeper": "healthy", "capacity-controller": "healthy"},
  "checks": {}
}
```

`daemons` is informational (a stale daemon never turns the status to
`degraded`). With host-wide browser admission enabled, a
`browser_capacity` block is added — `in_use_units`, `target_units`,
`waiters`, `status` — also informational.

A `paid_gateway` block reports the paid proxy gateway: `{"status":
"disabled"}`, `{"status": "ok", "strategy": …}`, or `{"status":
"refused", "strategy": …, "since": …, "error": …}` when the provider has
refused the engine's credentials (plan out of traffic, bad login) within
the last `dataimpulse.refused_ttl_seconds` (600s by default). While refused,
`checks.paid_gateway` says what to do, and `free_first` fetches go through
the free proxy pool instead. Informational: it never turns the status to
`degraded`, and reading it never sends traffic through the gateway.

---

### `GET /metrics`

Prometheus metrics endpoint (internal network only, enabled via
`observability.metrics_enabled` config).

---

## Error Format

All errors follow this shape:

```json
{
  "detail": "Human-readable error message"
}
```

429 responses (quota exceeded or rate limited) additionally carry a
standard `Retry-After` header (seconds), so a well-behaved HTTP client's
automatic backoff handling picks it up without needing to parse the body.

## Escalation Model

The system tries 3 levels of escalating intensity:

| Level | Engine | Proxy | Timeout | CAPTCHA |
|---|---|---|---|---|
| L1 | HTTP (httpx) | Any (score ≥ 40) | 20s | No |
| L2 | Botasaurus + Camoufox | Anonymous+ (≥ 70) | 40s | Yes |
| L3 | Camoufox only | Elite (≥ 90) | 60s | Yes |

A failure that skips further escalation (SSRF blocked, quota exceeded,
proxy exhausted, circuit open, a dead/unresolvable host) goes directly to
the dead-letter queue — but still appears in `GET /v1/jobs/{job_id}`'s
`results` with its real `failure_category`/`error_message`, not silently
dropped. Of these, `proxy_exhausted` and `circuit_open` are transient —
the DLQ entry is automatically retried once the underlying condition
clears (see `GET /v1/jobs/{job_id}/dlq` above); `ssrf_blocked`,
`quota_exceeded`, and `host_unreachable` are permanent and never
auto-retried.

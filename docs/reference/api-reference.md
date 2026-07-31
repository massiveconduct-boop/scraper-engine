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
| `async_mode` | bool | no | true | Async job processing |
| `webhook` | string | no | — | POST callback URL on completion |

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
  "tenant": "acme"
}
```

**Errors:**
| Status | Condition |
|---|---|
| `400` / `422` | Validation error (bad body shape, >500 URLs) |
| `403` | SSRF blocked (private/internal IP) |
| `413` | Request body > 1 MB |
| `429` | Quota exceeded or rate limit exceeded (100 req/min per IP) — carries a `Retry-After` header |

---

### `POST /v1/crawl`

Bulk Scrapy crawl for target sets larger than `/v1/scrape`'s 500-URL cap.
Same auth, `Idempotency-Key`, and error shape as `/v1/scrape`.

```json
{
  "spider_name": "titles",
  "start_urls": ["https://example.com"],
  "webhook": "https://your-app.com/callbacks/crawl"
}
```

---

### `GET /v1/jobs/{job_id}`

Poll job status and retrieve results. `results` includes both successful
and failed URLs, each with its own `failure_category`/`error_message` —
a partial failure never silently disappears. `progress` is a real fraction
(URLs completed / total URLs), not an estimate.

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
      "html": "<html>...</html>",
      "markdown": "# Example...",
      "extracted": {"title": "Example", "body": "..."},
      "level_used": 1,
      "failure_category": null,
      "error_message": null,
      "proxy_used": "1.2.3.4:8080",
      "html_snapshot_url": "snapshots/acme/550e8400.../20260729T120000.html",
      "from_cache": false,
      "duration_ms": 234,
      "fetched_at": "2026-07-21T12:00:00Z"
    }
  ],
  "error": null
}
```

`markdown` is produced regardless of which escalation level (L1/L2/L3)
actually succeeded, whenever Firecrawl conversion is configured (see
`FIRECRAWL_API_KEY`/`FIRECRAWL_BASE_URL` in `.env.example`) — it's
independent of `extracted`; a caller who only wants the clean markdown
(e.g. to hand to their own extraction model) can read that field and
ignore `extracted` entirely.

**Status values:**
| Status | Meaning |
|---|---|
| `PENDING` | Job enqueued, not yet processing |
| `PROCESSING` | Worker is actively fetching |
| `COMPLETED` | At least one URL succeeded |
| `FAILED` | No URL succeeded |
| `CANCELLED` | Job cancelled via `DELETE /v1/jobs/{job_id}` |
| `DEAD_LETTER` | Reserved for future use — not currently set by any code path |

---

### `GET /v1/jobs/{job_id}/dlq`

Raw dead-letter detail for one job — the same failed URLs already appear in
`GET /v1/jobs/{job_id}`'s `results`, but this endpoint additionally exposes
`enqueued_at`/`dead_at` timestamps from the dead-letter queue.

**Response:** `200 OK`
```json
[
  {
    "job_id": "550e8400-e29b-41d4-a716-446655440000",
    "url": "https://blocked.example.com",
    "failure_category": "proxy_exhausted",
    "error_message": "All fetch levels exhausted",
    "level_attempted": 3,
    "enqueued_at": "2026-07-21T12:00:00Z",
    "dead_at": "2026-07-21T12:00:05Z"
  }
]
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
  "s3_reachable": true
}
```

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

Non-retryable failures (SSRF blocked, quota exceeded, proxy exhausted, a
dead/unresolvable host) skip further escalation and go directly to the
dead-letter queue — but still appear in `GET /v1/jobs/{job_id}`'s `results`
with their real `failure_category`/`error_message`, not silently dropped.

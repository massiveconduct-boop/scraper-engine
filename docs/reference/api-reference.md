# Scraper Engine — API Reference

Base URL: `http://localhost:8000` | OpenAPI: `/openapi.json` (v3.1.0) | Swagger UI: `/docs`

## Authentication

All endpoints except `/v1/health` require an API key:

```
Authorization: Bearer sk-<api_key>
```

API keys are generated at tenant creation (BD-04). The key resolves to a `TenantId` which scopes all storage, quota, and proxy operations.

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
    "extraction_schema": {"title": "h1::text", "body": "article p::text"}
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
| `async_mode` | bool | no | true | Async job processing |
| `webhook` | string | no | — | POST callback URL on completion |

**Response:** `200 OK`
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "urls": 1
}
```

**Errors:**
| Status | Condition |
|---|---|
| `400` | SSRF blocked (private IP), validation error |
| `413` | Request body > 1 MB |
| `429` | Rate limit exceeded (100 req/min per IP) |

---

### `GET /v1/jobs/{job_id}`

Poll job status and retrieve results.

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
      "duration_ms": 234,
      "fetched_at": "2026-07-21T12:00:00Z"
    }
  ],
  "error": null
}
```

**Status values:**
| Status | Meaning |
|---|---|
| `PENDING` | Job enqueued, not yet processing |
| `PROCESSING` | Worker is actively fetching |
| `COMPLETED` | All URLs processed successfully |
| `FAILED` | Some or all URLs failed |
| `CANCELLED` | Job cancelled by user |
| `DEAD_LETTER` | All escalation levels exhausted |

---

### `DELETE /v1/jobs/{job_id}`

Cancel a pending or processing job.

**Response:** `200 OK`
```json
{"status": "cancelled"}
```

**Errors:** `404` (not found), `409` (already terminal state)

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

### `GET /v1/metrics`

Prometheus metrics endpoint (internal network only).

---

### `GET /admin/dlq`

List dead letter queue entries. Requires admin API key.

**Response:** `200 OK`
```json
[
  {
    "job_id": "uuid",
    "url": "https://blocked.example.com",
    "failure_category": "proxy_exhausted",
    "error_message": "No elite proxy available",
    "level_attempted": 3,
    "dead_at": "2026-07-21T12:00:00Z"
  }
]
```

### `POST /admin/dlq/{id}/resolve`

Resolve a DLQ entry. Requires admin API key.

```json
{"resolution": "manual_retry"}
```

---

## Error Format

All errors follow this shape:

```json
{
  "detail": "Human-readable error message"
}
```

## Escalation Model

The system tries 3 levels of escalating intensity:

| Level | Engine | Proxy | Timeout | CAPTCHA |
|---|---|---|---|---|
| L1 | HTTP (httpx) | Any (score ≥ 40) | 20s | No |
| L2 | Botasaurus + Camoufox | Anonymous+ (≥ 70) | 40s | Yes |
| L3 | Camoufox only | Elite (≥ 90) | 60s | Yes |

Non-retryable failures (SSRF blocked, quota exceeded, proxy exhausted) go directly to DLQ.

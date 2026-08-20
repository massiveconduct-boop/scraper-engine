# Architecture

**Purpose:** System design, invariants, module interactions, data flow.
**Scope:** Complete system architecture. Does NOT duplicate the specification — references it.
**When to read:** Understanding how components connect; adding new modules; debugging cross-cutting concerns.
**Keywords:** design invariants, escalation ladder, proxy pipeline, browser
pool, PgBouncer, API routing, SSRF enforcement, fetcher construction,
CAPTCHA solving, observability, tracing, botasaurus, metrics, data flow,
repository layout, src layout, webhook outbox, webhook sweeper, Slack
notifications, proxy pool health, proxy self-healing, DLQ auto-retry,
partial_failure.
**Dependencies:** none — describes the system as built; cross-references
`decisions.md` for WHY and `technical-debt.md` for full round history.
**Related:** `.local/specs/scraper-engine-blueprint-v2.md` (local-only, not tracked in git), `.claude/knowledge/decisions.md`, `.claude/knowledge/technical-debt.md`

**Path note (round 27):** every bare package path below (`core/`, `proxy/`,
`browser/pool.py`, etc.) means `src/scraper_engine/<that path>` — all
application code was consolidated under `src/scraper_engine/` this round.
See "Repository Layout" near the end of this file for the full picture.

---

## Design Invariants (from spec §1.1 — non-negotiable)

1. No component calls proxybroker2 HTTP control API — all proxy state in Postgres/Redis.
2. Camoufox owns 100% of fingerprint/UA/canvas/WebGL surface — app code never touches.
3. `tenant_id` is explicit `TenantId` value object everywhere — no ambient ContextVar at trust boundaries.
4. Every outbound fetch is SSRF-checked before enqueue and after every redirect.
5. Nothing cached as success unless `FetchResult.success is True` and not challenge page.
6. Every resource acquisition has guaranteed release path — context manager or TTL, never both.
7. SQL identifiers validated against allow-list regex before interpolation.

---

## Escalation State Machine

```
PENDING → CIRCUIT_CHECK → FETCHING_L1 → PARSING_L1
                                      ↘ failure → ESCALATING_L2 → FETCHING_L2 → PARSING_L2
                                                                               ↘ failure → ESCALATING_L3 → FETCHING_L3
                                                                                                              ↘ failure → dead-lettered (per-URL)
Non-retryable (SSRF, quota, proxy exhausted, unresolvable host): direct → dead-lettered (per-URL)
Cache hit (round 29): reused, no escalation attempted at all — see below
Cancellation (round 29): checked once per URL, before escalation starts
```

Levels: L1 (httpx/Scrapling, timeout 20s, any proxy), L2 (Botasaurus+Camoufox, timeout 40s, anonymous+ proxy), L3 (Camoufox-only, timeout 60s, elite proxy).

**Note on `DEAD_LETTER` (corrected round 29):** `JobStatus.DEAD_LETTER` is a
valid enum value and DB CHECK-constraint entry, but no code path has ever
set a job's *status* to it — "dead-lettered" above means the per-URL entry
written to the `dead_letter_queue` table (`DeadLetterQueue.enqueue`,
readable via `GET /v1/jobs/{job_id}/dlq`), which is a different thing from
the job's own terminal `status` (which lands on `FAILED` if no URL in the
job succeeded, `COMPLETED` if at least one did — see `Worker.process_job`'s
status derivation). Earlier versions of this doc conflated the two.

**`COMPLETED` no longer implies "every URL succeeded" (round 34):**
`JobStatusResponse.partial_failure` (bool) is `True` when `status ==
COMPLETED` but at least one URL landed in the DLQ alongside a success —
computed identically in `Worker.process_job` and
`api/routes.py::get_job`. A webhook/poller must check this flag, not just
`status`, to know whether a "COMPLETED" job was actually clean. See
`decisions.md` → "`partial_failure` as an Additive Boolean" for why this
is a field, not a new `JobStatus` value.

**DLQ entries are no longer all permanent (round 34):** `dead_letter_queue`
rows split into `PERMANENT_FAILURE_CATEGORIES` (`SSRF_BLOCKED`,
`QUOTA_EXCEEDED`, `HOST_UNREACHABLE`) and `TRANSIENT_FAILURE_CATEGORIES`
(`PROXY_EXHAUSTED`, `CIRCUIT_OPEN`) — both sets defined in
`orchestrator/worker.py`. Transient entries are auto-retried by
`proxy/dlq_reaper.py` once the condition that caused them clears (proxy
pool tier back to HEALTHY, circuit breaker back to CLOSED), up to
`config.dlq_reaper.max_auto_retries` attempts (`dead_letter_queue.
auto_retry_count`). `storage/dlq.py::enqueue` UPSERTs on `(job_id, url)`
so a repeat failure updates the same row instead of resetting the
counter via a fresh insert. See "Notifications & Proxy Self-Healing"
below for the full round-34 picture.

**Per-URL loop order (round 29), top to bottom inside `Worker.process_job`:**
1. Cooperative cancellation check (`_is_cancelled` — one Postgres point
   read per URL, not per fetch attempt). If the job's `scrape_jobs.status`
   is already `CANCELLED` (set by `DELETE /v1/jobs/{job_id}`), the whole
   URL loop stops here; results already recorded for prior URLs are kept.
2. Cache-reuse check (`_check_cache`, skippable per-request via
   `config_overrides.bypass_cache`) — see "Scrape Result Caching" below.
3. The escalation ladder itself (unchanged shape from round 1, plus
   markdown conversion and `on_result` persistence — see below).

Every terminal outcome for a URL — success, a synthesized failure
`FetchResult` from any of the three failure paths, or a cache hit — is now
appended to `results` (not just successes, see technical-debt.md round-29
item 2) and, if the caller (`orchestrator/tasks.py`) supplied one, awaited
through `on_result` immediately (see "Incremental Persistence" below).

## Scrape Result Caching (Round 29)

Before attempting L1, `Worker._check_cache(tenant_id, url)` looks up a
fresh (`extracted_at` within `CACHE_TTL_DAYS = 7`, a sliding window — see
`.claude/knowledge/decisions.md`) successful `scrape_results` row for that
exact URL, scoped to the tenant. A hit builds a `FetchResult` with
`from_cache=True` directly from the stored row (`markdown`, `extracted`,
`html_snapshot_url` carried forward, `html` deliberately left `None` — no
S3 re-upload needed) and skips the escalation ladder entirely: no
circuit-breaker/politeness/proxy cost, no quota consumption for that URL.
A caller forces a fresh scrape via `config_overrides.bypass_cache: true`.
`S3Client.SUCCESS_RETENTION_DAYS` (round 29: bumped 1 → 7) must stay ≥
`CACHE_TTL_DAYS`, or a cache hit's `html_snapshot_url` could point at an
already-expired S3 object.

## Incremental Persistence & Real Progress (Round 29)

`Worker.process_job` accepts an optional `on_result: Callable[[FetchResult],
Awaitable[None]]`, awaited once per URL the instant it reaches a terminal
outcome. `orchestrator/tasks.py::_run_scrape` builds a closure over
`pg`/`s3`/`tenant_id`/`job_id` and passes it as `on_result`; the closure
calls `_persist_one_result` (the per-item body `_persist_results` was
split into — the batch wrapper still exists, used only by the bulk-crawl
path, which has no per-item callback available since
`ScrapyAdapter.run_spider` returns a full batch). This replaced the old
"batch-persist everything after the whole job finishes" model, which is
what makes two things possible: `GET /v1/jobs/{job_id}`'s `progress` field
is now `len(result_rows) / len(urls)` while the job is `PROCESSING` (a real
fraction, not the old hardcoded `0.5`), and job cancellation (below) can
actually take effect mid-job with prior-URL results already durable.

## Job Cancellation (Round 29)

`DELETE /v1/jobs/{job_id}` does an atomic `UPDATE scrape_jobs SET status =
'CANCELLED' WHERE status <> ALL(terminal_values) RETURNING status` (404 if
no row exists at all, 409 if the row exists but was already terminal), then
best-effort calls `queue.fetch_job(job_id).cancel()` (rq 2.10.0's real
`Job.cancel()`) to pull a still-queued job before it's ever dequeued.
`job_id` is now passed explicitly to `_queue.enqueue(...)` at submission
time so rq's internal job id matches `scrape_jobs.job_id` — required for
`fetch_job(job_id)` to find the right job at all. An already-dequeued,
in-flight job is caught instead by `Worker._is_cancelled`'s per-URL check
(see loop order above). `orchestrator/tasks.py::_run_scrape_job` also
guards against a cancel that races ahead of rq actually dequeuing the job:
its initial job-row read now includes `status`, and returns immediately if
already `CANCELLED` instead of unconditionally flipping it to `PROCESSING`.

## Idempotent Submission (Round 29)

`POST /v1/scrape`/`/v1/crawl` accept an `Idempotency-Key` header. Before
the quota charge, `scrape_jobs` is queried for a live (not `FAILED`/
`CANCELLED`/`DEAD_LETTER`) job with that same key; if found, it's returned
as-is — no new row, no new quota deduction, no new enqueue. See
`.claude/knowledge/decisions.md` for why this is a non-unique index +
query-time exclusion rather than a DB uniqueness constraint.

---

## Notifications & Proxy Self-Healing (Round 34)

Two related subsystems, built together because pool-health alerts flow
through the same delivery mechanism as per-job webhooks.

**Webhook delivery — transactional outbox, not fire-and-forget.**
`orchestrator/webhook_events.py::WebhookEvent`/`WebhookEventType` is the
shape everything produces: `job.completed`/`failed`/`partial_failure`/
`cancelled`, `proxy_pool.degraded`/`critical`/`recovered`.
`orchestrator/webhook_dispatch.py::enqueue_and_deliver_webhook_event` is
the single delivery entry point both `orchestrator/tasks.py` (per-job) and
`proxy/harvester_daemon.py` (pool-health, see below) call: it writes a row
to `webhook_outbox` (migration `007`, per-tenant-schema, mirrors
`dead_letter_queue`'s shape — `storage/webhook_outbox.py`) *before*
attempting delivery, then makes one immediate best-effort attempt via
`WebhookDispatcher` (now `config.webhook`-driven, not hardcoded). A failed
or crashed attempt leaves the row `pending`; a standalone
`orchestrator/webhook_sweeper.py` daemon (own supervised OS process —
round 35 moved it, `proxy-harvester`, and `dlq-reaper` from their own
`docker-compose.yml` services into the `api` container via supervisord,
see "Container Topology (Round 35)" below — `_run_periodic`-shaped like
`harvester_daemon.py` — the loop
helper lives in `core/periodic.py` now, shared by both) sweeps every 30s
with exponential backoff, marking `dead` after `config.webhook.
max_retries` sweep-level attempts. `orchestrator/slack_formatter.py`
renders `WebhookEvent` into Slack's `{"text", "blocks"}` shape when the
target URL contains `hooks.slack.com`; any other URL gets the raw event
dict (backward compatible). Why split this way rather than one function
in `tasks.py`: `.claude/knowledge/decisions.md` → "`webhook_dispatch.py`
Split From `tasks.py`".

**Proxy pool self-healing — event-driven, not purely timer-driven.**
`ProxyManager.get_proxy`'s exhaustion path (`proxy/manager.py`) sets a
debounced Redis kick key (`SET proxy:harvest:kick NX EX 30`) and publishes
to `proxy:events:exhausted`; `harvester_daemon.py` runs a ~5s-poll watcher
task (independent of its existing timer-driven `_run_periodic` loops) that
reacts to the key — gated by a separate 60s cooldown key — and runs an
out-of-band `harvester.harvest_once()` cycle. See `decisions.md` →
"Debounced Redis Kick + Pub/Sub, Not Pub/Sub Alone" for why both the key
and the publish exist. `proxy/pool_health.py::PoolHealthMonitor` computes
per-tier (1/2/3) validated-proxy counts each health cycle, classifies
HEALTHY/DEGRADED/CRITICAL against `config.proxy_tiers.
degraded_below_count`/`critical_below_count`, persists state in Redis, and
returns only real transitions. A transition into DEGRADED/CRITICAL or back
to HEALTHY ("recovered") both (a) logs, and (b) — when
`config.webhook.ops_webhook_url` is set — enqueues a `WebhookEvent` through
the outbox/sweeper/Slack-formatter path above. **Known overlap, not
reconciled:** `operations.md`'s pre-existing `ProxyPoolCriticallyLow`
Prometheus/Alertmanager rule already alerts to Slack on low proxy counts
(round 25) via a completely different mechanism (threshold+duration on a
scraped gauge) — this round's ops-webhook path was built without
cross-referencing it. See `technical-debt.md`'s round-34 entry, "Open
thread" paragraph, before extending either one.

**Transient DLQ auto-retry.** `proxy/dlq_reaper.py` (own daemon, same
shape) polls `DeadLetterQueue.list_retryable()` per tenant every 60s for
`TRANSIENT_FAILURE_CATEGORIES` entries under their retry cap, checks
eligibility by reading current state (never mutating it — see
`decisions.md` for why `CircuitBreaker.state()` not `allow_request()`),
and re-enqueues the *same* `job_id` via the rq producer
(`orchestrator/job_queue.py::build_queue`) so `GET /v1/jobs/{job_id}`
keeps tracking the same job through a second attempt. See the DLQ note
in the "Escalation State Machine" section above for the
permanent/transient category split and the `(job_id, url)` UPSERT that
carries `auto_retry_count` across repeat failures.

**Container Topology (Round 35).** `proxy-harvester`, `dlq-reaper`, and
`webhook-sweeper` — described above as separate daemons — are no longer
separate `docker-compose.yml` services/containers. Root cause: the
documented dev bring-up command never named them, so on a real deployment
they simply never started, and the proxy pool went stale with no
operator-visible signal (see `technical-debt.md` round-35 entry). Fixed by
running all 4 long-running processes (`api` + the 3 daemons) as supervised
subprocesses of one container via `supervisord` (`docker/supervisord.conf`,
`Dockerfile`'s `CMD`) — each `autorestart`s independently, so one daemon
crash-looping doesn't take the others or the API down. `worker-l1/l2/l3`
stay separate compose services (different scaling unit — horizontally
scaled compute/browser workhorses, not lightweight always-on loops).
Operationally: `docker exec scraper_engine-api-1 supervisorctl status`
replaces `docker compose ps` for checking these 3; see `operations.md` →
"Self-healing daemons live inside the `api` container now" for the full
command reference. This does not change any of the process-boundary
reasoning elsewhere in this doc (separate OS process, separate in-process
metrics registry, etc.) — only the container each process runs in.

**Daemon Liveness in `/v1/health` (Round 36).** Closes the round-35
"Open follow-up" — `/v1/health` previously only reflected `api`'s own
Postgres/Redis/S3 reachability, with no signal at all for the 3 daemons
above. `core/periodic.py::run_periodic` now optionally writes a Redis
heartbeat (`heartbeat:<job-name>`, TTL = 3x the job's own interval) after
every cycle attempt when a `redis` client is passed — all 7 periodic jobs
across the 3 daemons pass one now. `api/health.py::_check_daemon_liveness`
reads those keys grouped by owning daemon (`proxy-harvester`:
harvest/promotion/health/pool_health/retention; `dlq-reaper`: dlq_reap;
`webhook-sweeper`: webhook_sweep) — Redis's own TTL expiry is the
staleness detector, no manual age math. Result surfaces as a new
`daemons` field, **informational only** — does not affect `/v1/health`'s
`healthy`/HTTP-status gate (a status-affecting first attempt broke a
real pre-existing test on a legitimate cold-start/standalone-testing
case; see `decisions.md` → "Daemon Liveness in `/v1/health` Is
Informational, Not Status-Affecting" for the full story, and → "Heartbeat-
via-Redis Over Supervisor RPC for Daemon Liveness" for why this reads
Redis heartbeats rather than querying supervisord's own RPC socket).

---

## Proxy Pipeline

```
harvest_once()
  ├─ _direct_scrape()        [PRIMARY — 5-10 proxies in ~5s]
  │   ├─ 8 source URLs → _scrape_one() per source
  │   │   ├─ _parse_ip_port() / _parse_geonode()
  │   │   ├─ _tcp_probe() (2s timeout)
  │   │   └─ _http_validate() through self-hosted judge (:8089)
  │   │       └─ Score: TCP-only=25 (below L1), validated=60 (above L1)
  │   └─ Persist to proxy_pool with anonymity_level + reliability_score
  │
  └─ _harvest_via_broker()   [SUPPLEMENTARY — 1-5 validated in ~20s]
      └─ proxybroker2 subprocess (30s timeout)
          └─ broker.find() → validate → JSON stdout → persist
```

**Proxy validation target:** `proxy/harvester.py::JUDGE_URLS` — a small
ordered list of independent, differently-hosted public IP-echo endpoints
(`httpbingo.org`, `api.ipify.org`, `postman-echo.com`, all plain-HTTP —
HTTPS would need CONNECT tunneling, which many free HTTP-only proxies
can't do). `_http_validate()` tries each in order, stopping at the first
200; `health_monitor.py::check_one()` imports the same list rather than
keeping its own, so both stay in sync. Round 32 tried a self-hosted
loopback judge (`proxy/judge_server.py`, port 8089) first — architecturally
unfixable: when a request is routed through a forward proxy, the *proxy*
resolves "127.0.0.1," not us, so a loopback judge can never validate a
real third-party proxy no matter how correctly it runs. Then tried a
single public target (`httpbin.org`, matching `health_monitor.py`'s prior
choice) — found it live-down (persistent 503s) while building the fix,
directly demonstrating why proxy scoring must never depend on one public
service. `judge_server.py` remains as a deterministic, network-independent
stand-in for tests only (`tests/unit/test_judge_server.py`,
`tests/integration/test_promotion.py`) — not the production judge. See
`.claude/knowledge/decisions.md` for the full decision record and
`.claude/knowledge/troubleshooting.md` → "All Pool Proxies Score 25".

**Source diversity:** 8 URLs across 6 operators (proxyscrape.com, openproxylist.xyz, TheSpeedX/GitHub, monosans/GitHub, pubproxy.com, geonode.com). 5 real failure domains (GitHub CDN shared by two repos).

**Scoring:** Two-tier. TCP-only=25 (below L1 threshold 40 — cannot be selected). HTTP-validated=60. `promote_tcp_only()` background job re-validates TCP-only proxies.

**ASN classification (`proxy/asn_classifier.py`, Round 35 rewrite).**
`build_asn_classifier()` unconditionally returns `ReverseDnsAsnClassifier`
— a DNS PTR-hostname lookup (`loop.getnameinfo()`) matched against the
same `_DATACENTER_KEYWORDS`/`_MOBILE_KEYWORDS` lists a MaxMind org-name
lookup would have used. Previously (round 22–34) this was
`MaxMindAsnClassifier`, gated on `GEOIP_ASN_DB_PATH` pointing at a
downloaded GeoLite2-ASN database — never actually set on this repo's
deployments, so every proxy silently scored `asn_class="unknown"` forever,
permanently zeroing `scoring.py`'s 10-point `ASN_BONUS` dimension. Root-
caused round 35 (see `technical-debt.md`): combined with the round-35
container-topology fix above, the pool's max achievable score sat at
exactly 69.8 — 0.2 points under `config.proxy_tiers.min_score_level_2`'s
70.0 floor — so L2/L3 always failed `proxy_exhausted`. Reverse-DNS was
chosen over fixing the MaxMind wiring because it needs no third-party
account/license key/database file to maintain (user-directed, see
`decisions.md`) — trade-off is lower precision (some datacenters skip a
descriptive PTR record, some residential ISPs set one).

---

## Paid Gateway Proxy (Round 40)

Toggleable paid rotating-gateway residential proxy (DataImpulse) for L2/L3,
alongside — never replacing — the free-pool pipeline above. Off by default
(`config.dataimpulse.enabled: false`); `config.dataimpulse.strategy`
(`free_only` / `paid_only` / `free_first`) only takes effect once enabled.
Full WHY: `decisions.md` → "Toggleable Paid Proxy Gateway". Full round
narrative incl. the two Docker-image bugs found live-verifying this:
`technical-debt.md`'s round-40 entry.

```
Worker._fetch_with_proxy()            [orchestrator/worker.py]
  strategy = free_only unless config.dataimpulse.enabled
  │
  ├─ paid_only  → build_gateway_proxy() → ProxyLease(...) directly
  │                (pm.get_proxy() never called)
  │
  ├─ free_first → pm.get_proxy() as today
  │                └─ ProxyPoolExhaustedError → build_gateway_proxy() fallback
  │
  └─ free_only  → pm.get_proxy() as today, unchanged (default)
```

- **`proxy/paid_gateway.py::build_gateway_proxy()`** — pure function, 4 env
  vars (`DATAIMPULSE_PROXY_HOST`, `DATAIMPULSE_PORT`, `DATAIMPULSE_USERNAME`,
  `DATAIMPULSE_PASSWORD`; note the host/port names are the user's own
  choice, not the originally-planned `DATAIMPULSE_GATEWAY_*`), no network
  I/O, no DB row. Returns `None` on any missing/invalid var — callers treat
  `None` as a hard misconfiguration, never a silent fallback (see
  `Worker.__init__`'s fail-fast startup check below).
- **`core/models.py::Proxy`** gained `username`/`password`/
  `source: Literal["pool","paid_gateway"]` (all optional/defaulted — zero
  effect on any existing free-pool `Proxy`) and `auth_url()`
  (`user:pass@host:port`, identical to `url()` when unauthenticated).
- **Deliberately bypasses `ProxyManager` entirely for a gateway lease** —
  no `_select_candidate`, no domain-ban check, no `lease_preflight`, and
  `mark_success`/`mark_failure` are skipped (`_fetch_with_proxy` checks
  `lease.proxy.source == "pool"` before calling either). A rotating
  gateway's exit IP changes server-side per connection — there's no fixed
  identity worth scoring, banning, or preflighting. See `decisions.md` for
  the full reasoning.
- **`Worker.__init__`** calls `build_gateway_proxy()` eagerly when
  `dataimpulse.enabled=true` and raises `RuntimeError` if it returns
  `None` — fails the job process at construction time, not silently
  mid-fetch (RQ forks one process per job).
- **Credential plumbing to the actual fetch:**
  `browser/camoufox_wrapper.py` adds `username`/`password` keys to the
  `proxy={"server": ...}` dict Playwright/Camoufox already accepts natively.
  Botasaurus takes a single proxy *string* (no dict support) — its two call
  sites (`fetcher/botasaurus_wrapper.py`, `browser/botasaurus_pool.py`)
  use `.auth_url()` instead of `.url()`.
- **Two Docker-image dependencies added** (`Dockerfile`, `system-base`
  stage): `nodejs` and `npm` — required by Botasaurus's own
  `botasaurus_proxy_authentication` helper for ANY credentialed proxy
  (local anonymizing-proxy chain via a lazily-`npm install`ed `proxy-chain`
  package), invisible before round 40 since no proxy this system used ever
  carried credentials. A related live-caught bug:
  `fetcher/level_2.py::_fetch_via_botasaurus`'s `except Exception:` didn't
  catch the `SystemExit` that library raises when Node is missing — fixed
  to `except (Exception, SystemExit):` (deliberately not a bare `except:`,
  to keep `asyncio.CancelledError`/`KeyboardInterrupt` propagating).
- **RESOLVED (round 41)** — see "Xvfb Display-Contention Lock" below for
  the root cause and fix. `paid_only`/`free_first` are now live-verified
  reliable for L2 (4 rounds of increasingly concurrent real jobs, zero
  crash-attributable job failures).

---

## Xvfb Display-Contention Lock (Round 41)

Closes round 40's open thread above. Two compounding bugs, both in
third-party code, worked around rather than patched (no vendored forks):

1. **botasaurus_driver's Xvfb launch picks a display number by scanning
   disk, not atomically.** `botasaurus_driver/core/config.py`'s
   `Config.__call__()` calls `pyvirtualdisplay.Display(visible=False,
   size=(1920, 1080))`, whose `_search_for_display()` lists
   `/tmp/.X*-lock` files and picks `max(existing) + 3` — a plain
   list-then-guess, not a claim. Camoufox's own launcher
   (`camoufox/virtdisplay.py::VirtualDisplay.get()`) is race-free by
   contrast: it launches Xvfb with `-displayfd`, so Xvfb itself claims a
   free number atomically and reports it back over a pipe. Two engines
   sharing one worker process (`Level2Fetcher.fetch()`'s Botasaurus-then-
   Camoufox fallback, `core.budget.BROWSER_SEMAPHORE` permitting several
   concurrent browser launches) meant a Botasaurus launch's stale-scan
   guess could collide with a Camoufox (or another Botasaurus) launch
   that had grabbed a real number moments earlier —
   `_XSERVTransSocketUNIXCreateListener: ...SocketCreateListener() failed
   / server already running`. Because this raises before
   `botasaurus_driver` ever returns a `Driver` object, application code
   had no handle to `close()` and clean up the half-started Xvfb process.
2. **`pyvirtualdisplay.Display.stop()` never unlinks its lock/socket
   files.** It `SIGKILL`s the Xvfb subprocess and waits for exit, but
   SIGKILL bypasses Xvfb's own atexit cleanup, so `/tmp/.X<N>-lock` and
   `/tmp/.X11-unix/X<N>` are left on disk even though the process is
   gone — feeding bad guesses to bug (1) for every future launch,
   compounding over a long-lived worker process's lifetime. (Camoufox's
   own `virtdisplay.py::kill()` already does this cleanup correctly for
   its own displays — this gap is specific to botasaurus_driver's use of
   `pyvirtualdisplay`.)

**Fix — `core/budget.py::XVFB_LOCK`**, a new process-wide `asyncio.Lock`
(alongside `BROWSER_SEMAPHORE`) serializing every Xvfb spinup *and*
teardown across both engines:
- `browser/camoufox_wrapper.py` — held across `__aenter__`'s launch call
  and, separately, across `__aexit__`'s `self._browser.__aexit__()`
  teardown call. Not held across the fetch itself, so
  `BROWSER_SEMAPHORE`'s real concurrency ceiling is unaffected.
- `fetcher/botasaurus_wrapper.py::fetch_html()` — held for the *entire*
  call. Botasaurus's `@browser` decorator bundles launch+navigate+close
  into one synchronous call with no seam to release early — an accepted
  throughput trade for correctness.
- `browser/botasaurus_pool.py::fetch()` — held across both the
  evict-and-close-old-entry step and the construct-new-driver step
  together (one `async with` spanning both), so a new launch can never
  start while a just-evicted driver's Xvfb teardown is still in flight.
  The reuse path (`_reuse_fetch`, no display touched) stays lock-free.

**Fix — `browser/_xvfb_cleanup.py::cleanup_stale_display()`** — new,
best-effort proactive removal of a just-closed Botasaurus driver's
`/tmp/.X<N>-lock`/`/tmp/.X11-unix/X<N>` files (reaches into
botasaurus_driver's private `Config._display` attribute; no public API
exists). Wired into `botasaurus_pool.py::_close_driver()` and
`botasaurus_wrapper.py::_botasaurus_fetch()`'s `finally` block (captured
via closure, since the `@browser` decorator never exposes the `Driver`
back to the caller after its own internal close).

**What this does and doesn't guarantee:** live verification (4 rounds of
concurrent real jobs against nairametrics.com, up to 4 simultaneous
2-URL jobs = 8 L2 fetches, `dataimpulse.strategy: paid_only`) showed the
underlying collision (`SocketCreateListener() failed`) can still
occasionally log — Xvfb's own `-displayfd` internal retry logic absorbs
a transient collision on a stale socket file — but it no longer
propagates into a crashed browser session or an unrecoverable job.
Zero job failures, zero stuck jobs, zero tracebacks attributable to it
across all 4 verification rounds, versus the pre-fix behavior (crashed
CDP/websocket connection, `"Connection to remote host was lost -
goodbye"`, no way to clean up the orphaned Xvfb process). This is a
concurrency-race fix, not a guarantee the warning line disappears
entirely — the warning is now cosmetic noise, not a failure mode.

---

## proxy_exhausted Mislabeling + browser_sessions Schema Regression (Round 42)

User-requested ("taken care of once and for all"): two stacked bugs behind
every `proxy_exhausted` DLQ entry, neither one about proxy supply.

**Bug 1 — terminal-failure mislabeling.** `orchestrator/worker.py::
process_job`'s per-URL `for level in LEVELS: ... else:` loop fabricated
`PROXY_EXHAUSTED`/"All fetch levels exhausted" whenever all 3 levels
failed for ANY reason not in `DLQ_ELIGIBLE_CATEGORIES` — which by design
(round 37) is most real proxy-adjacent failures (`BROWSER_CRASH`,
`NETWORK_TIMEOUT`), since those categories are meant to escalate rather
than DLQ early. Proven live under `dataimpulse.strategy=paid_only`, where
`ProxyManager.get_proxy()` is structurally never called — yet
`proxy_exhausted` still appeared. Fixed: the branch now tracks
`last_level_result` and reports its real category/message; falls back to
the old label only when literally no level was ever attempted (every
politeness slot stayed busy). `proxy/dlq_reaper.py` gained its own
(separate from `worker.py`'s `TRANSIENT_FAILURE_CATEGORIES`, which also
gates early-break-vs-escalate) transient list covering `BROWSER_CRASH`/
`NETWORK_TIMEOUT` too, same tier-health eligibility check as
`PROXY_EXHAUSTED`.

**Bug 2 — the real failure the mislabeling hid.** Fixing bug 1 exposed
every remaining terminal failure as `browser_crash / column
"storage_state" does not exist`. Root cause: migration 002 fixed
`browser_sessions`' columns (`domain`/`storage_state`/`last_used_at`/
`expires_at`, matching `browser/session_state.py`), but migrations
004/005/007 each redefine `create_tenant_schema()` wholesale and each
copy-pasted the *original* broken 001 shape — silently reverting 002's
fix every time. Every live tenant schema on this deployment had the
broken shape (verified directly, 100% affected). Invisible in practice
because `BrowserPool.lease()`'s `session_mgr.save()` call swallows its
own exception (warning-only), and `retention_reaper.py` already
defensively swallows per-tenant schema drift — only
`SessionStateManager.load()` (called unconditionally by `BrowserPool.
acquire()` on any Camoufox cold-start for a not-yet-warm domain,
effectively every first L3 attempt per domain per job) was unguarded,
and its crash is exactly what bug 1 was mislabeling. Fixed: new migration
`008_fix_browser_sessions_schema_regression.py` — restores the correct
`browser_sessions` block in `create_tenant_schema()` and drops+recreates
every existing tenant schema's table to match (safe: no schema under the
broken shape could have held real data, since both read and write paths
failed identically against it).

Live-verified together: the same URLs that previously crashed with
`storage_state` errors under `paid_only` now complete successfully, zero
`storage_state` errors in logs, zero new DLQ entries. Full narrative,
every detail: `.claude/knowledge/technical-debt.md`'s round-42 entry.

---

## Browser Pool

> **CORRECTION (round 25) — wired into production; supersedes the round-24
> correction below (kept for history).** `fetcher/factory.py`'s
> `build_level2_fetcher`/`build_level3_fetcher` now accept a `pool` param;
> `Level2Fetcher`/`Level3Fetcher` lease from it instead of constructing
> `CamoufoxWrapper` directly. **Lifecycle: one pool per rq job, not one per
> process** — `orchestrator/tasks.py::_run_scrape` constructs, `start()`s,
> and `shutdown()`s a `BrowserPool` bracketing a single job's
> `Worker.process_job()` call, because rq forks a fresh "work horse" process
> per job that `os._exit()`s right after (same fact behind the round-24
> tracing `force_flush()` fix below) — a pool literally cannot outlive one
> job. Still a real win for multi-URL-same-domain jobs (crawls), which now
> reuse one hot browser instead of cold-starting Camoufox per URL.
>
> **Mismatch-handling correctness fix (round 25, two parts):**
> 1. `acquire()` used to tear a live browser down on ANY mismatch (wrong
>    domain, wrong proxy) — contradicting this very section's own
>    documented contract ("tear-down only on unhealthy release, idle
>    timeout, or explicit shutdown"). Fixed: a mismatched wrapper is now
>    kept in the pool as a spare for a future request it does match,
>    instead of being destroyed. Total concurrently-alive instances still
>    can't exceed `core.budget.BROWSER_SEMAPHORE` either way.
> 2. A prewarmed wrapper's `_last_domain` starts `None` (never leased yet)
>    — this was being treated as a domain *mismatch* against any real
>    request, evicting every prewarmed instance on its first real use.
>    Fixed: `None` now means "unclaimed," matching anything.
>
> **Proxy mismatch is deliberately NOT given the same "unclaimed matches
> anything" treatment** — see `.claude/knowledge/decisions.md` →
> "BrowserPool Mismatch Handling" for why (a prewarmed browser's proxy is
> baked in at Camoufox launch time and can never be changed; treating
> `proxy=None` as a wildcard would silently serve a proxy-scoped request
> through no proxy at all).
>
> `SessionStateManager` is now actually constructed in production alongside
> each pool (`ttl_days=config.session_retention.browser_sessions_ttl_days`)
> — this is what closes the previously-dead `browser_sessions_ttl_days`
> config field, since the TTL only ever mattered once sessions started
> being persisted for real.

> **CORRECTION (round 24, historical — superseded above).** Everything
> below describes `browser/pool.py::BrowserPool` as designed and as its own
> unit tests exercise it — still accurate. What changed *then*: a fresh
> audit grepped every `BrowserPool(` call site and found zero outside its
> own file and tests — the "hot-browser pool" architecture below was never
> actually wired into the fetch path. Fixed round 25 (above).

**Design:** Hot-browser pool with real reuse. `pool.start(N)` launches N Camoufox instances and stores live contexts in an asyncio.Queue. `pool.lease(proxy, domain)` is the async context manager — returns a live context, guarantees release (structural cleanup per invariant §1.1.6).

**Key methods:**
- `start()` — launches prewarm_count browsers, stores (context, wrapper, idle_since)
- `acquire(domain)` — classifies drained items as selected/keep/teardown per idle timeout + domain matching
- `release(ctx, healthy)` — healthy returns to pool, unhealthy tears down
- `lease(proxy, domain)` — async context manager wrapping acquire/release
- `shutdown()` — tears down all live contexts

**Session persistence (round 7):** When `session_mgr` is supplied, storage_state is loaded in `acquire()` (outside the classify-loop), passed through `CamoufoxWrapper.__init__(storage_state=...)`, applied in `__aenter__` via `browser.new_context(storage_state=blob)` (Path B — Path A unavailable, AsyncCamoufox does not forward the kwarg). State saved back to Postgres on healthy `lease()` exit. Save failures logged at WARNING with `exc_info=True` — pool continues serving.

**Safety properties:**
- Semaphore-gated: `BROWSER_SEMAPHORE` prevents unbounded spawn (F-14).
- No double-issue: `acquire()` classifies each item exactly once — selected item never re-queued.
- Process cleanup: `__aexit__` always runs, browser process reaped.
- Domain/proxy guard: `lease(domain=X, proxy=Y)` only reuses a context whose `_last_domain`/`proxy` matches — except an unclaimed wrapper (`_last_domain is None`, never leased) matches any domain (round 25 fix, see the correction note above). Proxy is never given that same relaxation.
- Session I/O outside classify-loop: load at line 125, classify-loop return at line 119. Session code never executes during queue bookkeeping.

---

## PgBouncer

**Architecture:** `pgbouncer-init` Docker service auto-regenerates SCRAM userlist from Postgres `pg_authid.rolpassword`. PgBouncer mounts shared volume. Zero manual steps.

**Transaction pooling:** `PostgresClient.acquire()` wraps SET search_path in `BEGIN...COMMIT` to ensure all statements hit the same backend connection. On success: `SET search_path=public` then `COMMIT`. On any exception (including cancellation): `ROLLBACK` only, no `SET search_path` attempt — a failed query aborts the transaction server-side, so issuing anything but ROLLBACK/COMMIT there would itself raise `InFailedSQLTransactionError`, masking the real error and skipping COMMIT entirely, which returned the connection to the pool mid-transaction (this was also the source of the "Resetting connection with an active transaction" error-level log noise from asyncpg's own pool-release safety net — see `.claude/knowledge/troubleshooting.md`).

---

## API Routing (Round 8-11 — Fully Wired)

All routes enforce 4 invariants per blueprint:

1. **Tenant/auth** — `TenantResolver.resolve(api_key)` via `X-API-Key` header → `TenantId`. 401 on bad key.
2. **SSRF guard** — `SSRFGuard.validate(url)` on every URL before processing. 403 on blocked ranges.
3. **Quota enforcement** — `QuotaManager.check_and_increment(tenant_id)` reads per-tenant limit from `public.tenants.quota_daily_limit`. Raises `QuotaExceededError` → 429. No bare except.
4. **DB persistence** — `INSERT INTO scrape_jobs` before returning. `GET /v1/jobs/{job_id}` queries live `scrape_jobs` table. 404 on missing.

**Startup:** `api/main.py` uses `lifespan` context manager to initialize `PostgresClient`, `RedisClient`, and `TenantResolver` singletons. `@app.on_event("startup")` was unreliable in FastAPI 0.139.2.

**Caller-facing surface expanded (Round 56).** Five capabilities that
already existed internally had no route exposing them to a caller — surfaced
as 4 independently-shipped routes plus a CLI wrapper, each following the
same 4-invariant shape above:

- `GET /v1/jobs` — tenant-scoped job list (`status`/`limit`/`offset`
  filters), same schema-per-tenant isolation as `GET /v1/jobs/{job_id}`.
- `GET /v1/dlq` — tenant-wide dead-letter listing, the caller-facing sibling
  of the existing per-job `GET /v1/jobs/{job_id}/dlq`
  (`DeadLetterQueue.list_for_tenant`'s `job_id=None` mode, previously only
  used internally by ops tooling and the `dlq_size` gauge).
- `GET /v1/quota` — remaining daily quota for the calling tenant
  (`QuotaManager` already tracked this; a caller previously only discovered
  its limit by hitting a `429`).
- `GET /v1/webhook-events` — static reflection of `WebhookEventType`'s
  values and `WebhookEvent`'s JSON schema, no DB/Redis touch.
- `cli/` gained a caller-facing `api` subcommand group (`scrape`/`jobs`/
  `job`/`quota`/`dlq`) that wraps these routes over `httpx` instead of
  talking to storage directly — distinct from the ops subcommands
  (`serve`/`worker`/`harvest`/…), which still touch Postgres/Redis directly.

Caught along the way: FastAPI's `Query(...)` marker never resolves to its
plain value when a route function is called directly (every test in
`api/routes.py`'s module does this, bypassing FastAPI's DI) — `list_jobs`/
`list_dlq` use plain `int` params with a manual `_validate_pagination()`
helper (422 on out-of-range) instead.

Full detail: `.claude/knowledge/technical-debt.md` round-56 entry. Endpoint
request/response shapes: `docs/reference/api-reference.md`.

---

## SSRF Enforcement — Two Checkpoints, Not One (Round 22 — closes invariant #4 TOCTOU gap)

Invariant #4 ("every outbound fetch SSRF-checked before enqueue and after every
redirect") was only half-true through round 21: `SSRFGuard.validate()` ran
once in `api/routes.py` at job-submission time and never again. The actual
fetch happens later, in a different process (the worker), after a queue wait
of unknown length — a DNS-rebind or same-request redirect to a private/
metadata address in that window bypassed the guard completely.
`validate_redirect_chain()` existed but had zero production call sites
(grep-verified) despite docs claiming it was wired.

Now enforced at both checkpoints:
1. **Submit time** (unchanged) — `api/routes.py`, before enqueue.
2. **Fetch time** (new, round 22) — every fetcher re-validates immediately
   before connecting, and again per redirect hop:
   - `Level1Fetcher` (httpx): `follow_redirects=True` replaced with a manual
     redirect loop, validating each hop before following it.
   - `Level2Fetcher`/`Level3Fetcher` (Camoufox/Playwright): a `page.route()`
     handler (`fetcher/_content_utils.py::SSRFRouteGuard`) validates every
     request/redirect at the browser layer, aborting blocked ones and
     surfacing the real `SSRFBlockedError` (Playwright itself only reports a
     generic aborted-request error).

Also fixed in `core/ssrf_guard.py`: `_resolve_host` checked only the first
`getaddrinfo()` result, so a multi-record DNS answer (public IP first,
private second) could slip through — renamed to `_resolve_hosts`, checks
every resolved address.

Live-proven inside the real `worker-l2` container: a genuine private-network
navigation attempt (docker-network IP, `172.18.0.13`) was correctly blocked.
Full evidence: `.claude/knowledge/decisions.md`.

---

## Fetcher Construction & Shared Content Helpers (Round 13-16)

- **DI factory:** `fetcher/factory.py::build_level1/2/3_fetcher(config)` is the ONLY
  production path to a fetcher (CI grep-gate enforces it). Reads `config.levels.level_N`
  (unified `LevelConfig`: goto/networkidle/max_total/post_load/retry_increment/scroll fields).
- **Shared helpers** (`fetcher/_content_utils.py`, used by L2 + L3):
  `safe_content` (mid-nav guard), `poll_until_solved` (ChallengeDetector-gated retry),
  `autoscroll` (lazy-load/infinite-scroll, consecutive-stable stop).
- **`fetcher/_failure.py::classify_fetch_exception`** maps DNS errors → HOST_UNREACHABLE.
- **Escalation additions:** worker escalates JS-gated L1 shells (`looks_javascript_gated`)
  and dead-letters HOST_UNREACHABLE immediately (no futile L1→L2→L3).

## CAPTCHA Solving (Round 19 provider layer, Round 20 fetch-path wiring)

Provider-abstracted, primary-with-fallback, **wired into the L2/L3 fetch path**
(round 20 — was provider-only in round 19).

```
CaptchaSolver(primary=NoCaptchaAI, fallback=CapSolver)   [services/captcha_solver.py]
  ├─ solve_recaptcha_v2 / solve_turnstile / solve_hcaptcha / solve_aws_waf
  │  / solve_geetest / solve_mtcaptcha  → try primary, on None → fallback
  └─ build_captcha_solver(budget)  ← env keys (NOCAPTCHA_AI_API_KEY, CAPSOLVER_API_KEY)

services/_anticaptcha.py   — shared createTask/getTaskResult (arbitrary task dict),
                              solve_image_to_text (OCR, sync), get_balance
services/nocaptcha.py      — NoCaptchaAIClient (primary)  [provider-specific task types]
services/capsolver.py      — CapSolverClient (fallback; also covers hCaptcha)
```

Both gated by `CapSolverBudget` (per-tenant $/day, BD-03) + `CAPSOLVER_CONCURRENCY`.
Task-type strings are provider-specific and were live-corrected from stale docs
(see troubleshooting.md).

**Fetch-path wiring (round 20):** the worker builds the solver once
(`build_captcha_solver(CapSolverBudget(redis))`) and threads it through the
factory into L2/L3. After `poll_until_solved`, if the page still classifies as a
challenge, `Level*Fetcher._maybe_solve_captcha` calls
`fetcher/_captcha.solve_captcha_on_page`:

```
solve_captcha_on_page(page, solver, tenant_id, url)      [fetcher/_captcha.py]
  detect widget (page.evaluate → {kind, sitekey})  # recaptcha_v2 | hcaptcha | turnstile
    → solver.solve_<kind>(tenant, sitekey, url)  → token
    → inject token (kind-specific JS; recaptcha also fires ___grecaptcha_cfg callback)
    → caller waits, re-polls; ChallengeDetector still gates success
```

Best-effort: returns False (never raises) on no widget / no sitekey / no token /
inject failure → degrades to "still a challenge", never a false positive. Null-safe:
no provider key → solver is None → fetch runs with solving skipped. Observable via
`captcha_solve_attempts_total{kind}` / `captcha_solved_total{kind}`. DOM detect/inject
is unit-tested with a fake page; live-verified end to end round 22 (real Camoufox
+ real DOM detect + real inject — see `.archive/evidence/round-20-evidence.md`
(local-only, not tracked in git) for the mechanics). The one thing NOT proven live is a target actually *accepting* a
solved token, because no NoCaptchaAI solve has produced a token yet on this
account — root-caused round 22 as an account-side gap (no subscription plan,
not a code bug); see `.claude/knowledge/troubleshooting.md` → "Captcha task
accepted but stuck idle forever" and `decisions.md` for the full evidence.

**Not integrated with the round-25 Botasaurus path.** `Level2Fetcher`'s
Botasaurus-first attempt (see "Botasaurus Integration" below) does not call
`solve_captcha_on_page` — Botasaurus's Selenium-style `Driver` has no live
Playwright `page` for that pipeline to run against. A challenge-page result
from Botasaurus just falls back to the Camoufox pipeline above, which does
solve captchas normally.

---

## Observability & Tracing (Round 24)

**Single entry point:** `observability/bootstrap.py::bootstrap_observability(cfg)`
— called once per process (api's `create_app()`, `cli/entrypoint.py`'s `main()`,
`proxy/harvester_daemon.py`'s `run()`, and at module import time in
`orchestrator/tasks.py` so it fires once per rq worker process). Calls
`configure_logging()` always, `configure_tracing()` when
`observability.tracing_enabled`.

**Logging:** `observability/logging.py::configure_logging()` bridges stdlib
`logging.getLogger(__name__)` (100% of this codebase's log calls — nothing
calls `get_logger()`) through structlog via `structlog.stdlib.ProcessorFormatter`
on the root logger's handler, rendering every log call (including third-party
libraries like botocore/httpcore) as JSON. `logging_level` per-environment via
`config/{env}.yaml` (`staging.yaml`: DEBUG, `production.yaml`: WARNING).

**Tracing:** `observability/tracing.py::configure_tracing()` builds a real
`TracerProvider` (tagged with `service.name` via `Resource`) exporting to
`observability.otlp_endpoint` (default `http://jaeger:4317` — the `jaeger`
docker-compose service, Jaeger's all-in-one image, native OTLP receiver + UI
on `:16686`, no separate otel-collector). Same function also arms three
process-wide auto-instrumentors — `HTTPXClientInstrumentor`,
`AsyncPGInstrumentor`, `RedisInstrumentor` — so every outbound httpx/Postgres/
Redis call automatically nests as a child span under whatever's currently
active, with zero per-call-site changes. `api/main.py` separately calls
`FastAPIInstrumentor.instrument_app(app)` (needs the `app` object, so it can't
live in `configure_tracing()`) for one span per HTTP request.

**Two manual root spans** give every background code path something for the
auto-instrumented children to nest under:
- `orchestrator/tasks.py::_run_scrape_job` — one `scrape_job` span
  (`job_id`/`tenant_id` attributes) per rq job.
- `proxy/harvester_daemon.py::_run_periodic` — one `proxy_daemon_{name}` span
  per cycle (harvest/promotion/health/retention).

**The one non-obvious gotcha:** rq's work-horse process exits via `os._exit()`
(confirmed in rq's own source), which skips `atexit` entirely, and
`BatchSpanProcessor`'s background export thread doesn't survive `fork()`
either — so `orchestrator/tasks.py`'s job span needs an explicit, timeout-
bounded `force_flush()` in its `finally` block (not just `atexit.register
(provider.shutdown)`, which is what `configure_tracing()` still does for
every *non*-forking process — api, cli, harvester daemon). Full story:
`.claude/knowledge/troubleshooting.md` → "BatchSpanProcessor + fork()".

**Known limitation:** only the API process and the two spots above create
spans. The rq workers' and harvester daemon's *outbound* calls (httpx/pg/
redis) are instrumented and nest correctly under those two root spans, but
nothing else in those processes currently starts its own spans — this is
by design (matches what was asked for), not a gap.

---

## Botasaurus Integration (Round 25)

**What:** `fetcher/botasaurus_wrapper.py::BotasaurusWrapper` — real
Botasaurus fetch, gated by the same `core.budget.BROWSER_SEMAPHORE` as the
Camoufox path (spec §3.6's F-32 fix: Botasaurus's own `@browser(parallel=N)`
manages its own multiprocessing pool internally, so `parallel=1` is always
forced, never caller-configurable — our semaphore stays the single
concurrency authority).

**Where it sits in the escalation:** `Level2Fetcher.fetch()` tries
Botasaurus first (when `fetcher/factory.py` constructed one — gated on
`"botasaurus"` appearing in `config.levels.level_2.engine`), falling back to
the existing full Camoufox pipeline (challenge-detection, captcha-solve,
scroll) on either an exception or a detected challenge page. This split
exists because Botasaurus's driver is Selenium-style with no live Playwright
`page`/`context` — the existing challenge-detection/captcha-solve/scroll
helpers in `fetcher/_content_utils.py` can't run against it. L3 has no
Botasaurus attempt at all (spec: L3 is "Camoufox-only, nuclear").

**Config:** `headless=False` + `enable_xvfb_virtual_display=True` (never
`headless=True` — botasaurus rejects that combination outright, confirmed
live: `ValueError`, and headless is the more easily fingerprinted mode
regardless). `profile=session_id` (`f"{tenant_id}:{domain}"`) — botasaurus's
own persistent-Chrome-profile mechanism, separate from Camoufox's
`storage_state` serialization.

**One-shot per-fetch, opt-in pooled for same-domain jobs (round 26):** a
`BotasaurusWrapper` fetch still launches and tears down its own driver per
call (`reuse_driver=False` — see the pool-safety finding below for why this
stays hardcoded). When `browser/botasaurus_pool.py::BotasaurusPool` is wired
in (opt-in via `Level2Fetcher`'s `botasaurus_pool` param, one instance per
rq job, same lifetime as `BrowserPool`), a 2nd+ fetch for the same
(proxy, domain) within that job reuses the live driver instead.

**History:** this file was deleted as dead code earlier in round 25 (never
imported, `botasaurus` not a declared dependency, and — discovered only
after restoring it — the deleted version called a nonexistent
`driver.page_source` instead of the real `driver.page_html`, so it would
have crashed on its first real fetch even if it had been wired). Restored
and wired for real per an explicit follow-up ask. Full reasoning for both
the deletion and the reversal: `.claude/knowledge/decisions.md` →
"Botasaurus".

### Botasaurus Capability Upgrade (Round 26)

Six capabilities identified by a research pass (`.claude/MEMORY.md` →
Technical Debt, round 25 follow-up) were implemented, every API re-verified
against the real installed source (`pip download botasaurus==4.0.97
botasaurus-driver==4.0.93 botasaurus-requests==4.0.38`, not just the READMEs)
before wiring anything.

**Per-fetch upgrades (`fetcher/botasaurus_wrapper.py`, config-driven via the
new `config.botasaurus: BotasaurusConfig`, `config/schema.py`):**
- `driver.google_get(url, bypass_cloudflare=True)` replaces plain
  `driver.get(url)` — free Cloudflare-tier bypass (Google-referrer spoofing +
  human-like Turnstile solving), no CapSolver spend. Default on.
- `tiny_profile=True` (~1KB vs ~100MB per persisted profile) — **only sent
  when a profile (`session_id`) is actually present.** Verified live: the
  real `botasaurus_driver.core.config.Config.__init__` raises
  `ValueError("Profile must be given when using tiny profile")` if
  `tiny_profile` is set without one — this surfaced as a real crash during
  this round's own live smoke test (a caller with no `session_id`, e.g. an
  ad-hoc/anonymous fetch, would otherwise hard-fail). Same gate applied in
  `BotasaurusPool`.
- `remove_default_browser_check_argument=True`, `close_on_crash=True` —
  concrete anti-detection/reliability `@browser` kwargs, default on.
- `driver.short_random_sleep()` after every load — default on
  (`random_sleep_enabled`).
- `max_retry` — botasaurus's own internal retry+backoff loop; default `0`
  (off, unchanged behavior), only sent to the decorator when `> 0`.
- `UserAgent.HASHED`/`WindowSize.HASHED` (real string constants,
  deterministic per-profile) — only paired with a profile, same gate as
  `tiny_profile`. Never `RANDOM` (botasaurus's own maintainers advise against
  it as a default).

**Same-domain driver reuse (`browser/botasaurus_pool.py::BotasaurusPool`,
item 2 from the research, redesigned):** the original research proposed
botasaurus's own `reuse_driver=True`. Reading
`botasaurus/browser_decorator.py` directly during planning found its
internal `_driver_pool` is a **bare, unkeyed module-level list**
(`.pop()`/`.append()`, no matching on proxy, profile, or tenant at all) —
enabling it as-is would let one tenant's fetch silently receive a driver
still configured with a *different* tenant's proxy/profile, a direct hit on
the tenant-isolation invariant (spec §1.1 #3). `BotasaurusPool` instead
constructs raw `botasaurus.browser.Driver` instances itself (bypassing the
`@browser` decorator and botasaurus's pool entirely) and keys reuse the same
safe way `browser/pool.py::BrowserPool` already keys Camoufox contexts:
proxy + domain match → reuse via `driver.requests.get(url)` (verified: this
runs as an in-page JS `fetch()` through the driver's own tab, so it inherits
that tab's live cookies/TLS session natively — no separate cookie-jar
plumbing needed); mismatch → close the old driver, build a new one. One
instance per rq job, same construction/shutdown bracket as `BrowserPool` in
`orchestrator/tasks.py::_run_scrape`. Wired opt-in through
`Worker`/`fetcher/factory.py::build_level2_fetcher()`/`Level2Fetcher` —
`None` (default off in tests) preserves exactly the pre-round-26 one-shot
behavior.

**L1 JA3 client (`services/botasaurus_requests_client.py`, item 6,
independent of the above):** `botasaurus_requests`' JA3-TLS-fingerprint-
matched `firefox` session, config-gated off by default
(`config.botasaurus.l1_ja3_client_enabled` — a brand-new code path with no
live-traffic validation yet). Wired into `Level1Fetcher` as an optional
first-attempt client, same first-attempt/fallback shape as L2's
Botasaurus-then-Camoufox pipeline. Always calls with `allow_redirects=False`
— `Level1Fetcher` owns the redirect-following loop so every hop still gets
SSRF-revalidated (spec §1.1 #4); letting the client follow redirects
internally would skip that. `botasaurus-requests` was already a transitive
dependency of `botasaurus` but is now declared directly (`pyproject.toml`,
`Dockerfile`, `.github/workflows/test.yml`) since this module imports it
directly — same reasoning as every other direct-import dependency in those
lists (`.claude/knowledge/operations.md` #12).

**Found during PR review, before merge:** the original design called
`self._ja3_client.get(url)` per redirect hop inside `Level1Fetcher.
_fetch_via_ja3`, and each `.get()` constructed a brand-new
`firefox.Session()` — so a cookie set by an intermediate redirect hop (a
common consent/session-redirect pattern) never reached the next hop. Fixed
by adding `BotasaurusRequestsClient.open_session()` → `Ja3Session`, opened
once per top-level `Level1Fetcher.fetch()` call and reused across every hop
of that call's own redirect loop — never shared across separate fetches, so
this doesn't reintroduce the cross-tenant-state class of bug the
`reuse_driver` finding above already ruled out. `BotasaurusRequestsClient.
get()` still exists as a one-shot convenience for callers that don't need
cross-hop continuity.

**Live verification (round 26, corrected):** end-to-end browser verification
against the local `challenge-mirror` container (`http://localhost:8090/`,
confirmed via `/proc/net/tcp` + reading its own `server.py` — it isn't
published on a predictable docker port) initially failed with
`ChromeException: Invalid parameters [code: -32602]` on `Page.navigate`, and
was first misdiagnosed as a Chrome/CDP version mismatch (Chrome 149 vs.
`botasaurus_driver==4.0.93`) — **that diagnosis was wrong.** Isolating the
exact CDP error data (`'Failed to deserialize params.url - ... string value
expected'`) and comparing a raw `Driver().get()` call (worked) against the
`@browser`-decorated path (failed) found the real cause: botasaurus's own
decorator always invokes the wrapped function positionally —
`func(driver, data)` (`browser_decorator.py`'s `run_task`) — where `data` is
`None` for a bare `_fetch()` call. `fetcher/botasaurus_wrapper.py`'s inner
`_fetch(driver, target_url: str = url)` relied on a keyword *default* for
the URL, which that positional call silently clobbers with `None` — so
every fetch was navigating to `None`, not the target URL. Fixed by reading
`url` from the outer closure directly and giving `_fetch` a throwaway
`_data` parameter instead of a same-named default. The existing mocked unit
tests didn't catch this because the test harness's fake decorator called
`fn(_FakeDriver(), URL)` — passing the real URL positionally, unlike
botasaurus's actual `fn(driver, None)` — so it was fixed too (now calls with
`None`, matching production). After the fix: `BotasaurusWrapper.fetch_html()`
against `challenge-mirror` returns real content
(`<h1>Verified Content</h1>`) with `google_get(bypass_cloudflare=True)` and
`short_random_sleep()` both genuinely executing, and a 2-URL same-domain
`BotasaurusPool.fetch()` run confirms exactly one `Driver()` construction
across both calls (the 2nd fetch used `driver.requests.get()`, not a new
browser launch) — both are now real, not just source-cited + mocked.

**Silent-false-success on a Chromium internal error page (Round 57).**
`botasaurus_driver`'s navigation never raises when Chromium lands on its own
internal `chrome-error://` page (a DNS failure, a connection reset, etc.) —
it returns normally, so a genuinely failed navigation looked identical to a
successful one to every caller. Fixed with a dedicated post-navigation
check, `browser/_botasaurus_nav_check.py::raise_if_navigation_failed()`
(checks `driver.current_url` for the `chrome-error://` scheme), called from
both real-navigation Botasaurus paths (`fetcher/botasaurus_wrapper.py`,
`browser/botasaurus_pool.py`). Live-verified with a real Chromium launch
against a deliberately unreachable host.

**Missing autoscroll (Round 58).** `Level2Fetcher._fetch_via_botasaurus`
never autoscrolled, silently dropping any lazy-loaded content below the
fold — every other fetch path already autoscrolled. Fixed via
`browser/_botasaurus_scroll.py::botasaurus_autoscroll()`, a sync port of
the same height-stability algorithm the Playwright/Camoufox paths use
(`driver.run_js` scroll + height-poll loop, since Botasaurus's `Driver` API
is synchronous, not `page.evaluate()`). Live-verified against a real
infinite-scroll page.

**RAM-aware concurrency cap + image/CSS blocking (Round 59).**
`core/budget.py::resolve_browser_max_total_instances()` — opt-in
(`camoufox.ram_aware_concurrency_enabled`, default off) ceiling on live
browser instances, delegating to Botasaurus's own
`calc_max_parallel_browsers()` (reads `psutil.virtual_memory().available`);
can only reduce the configured `BROWSER_SEMAPHORE` size, never raise it.
Calibrated against a real measured Botasaurus/Chromium headful launch RSS
(804.7MB on the host measured, isolated via before/after PID diff) rather
than Camoufox's much lighter figure, since the semaphore is shared across
both engines and Botasaurus is the heavier one. Separately,
`block_images`/`block_images_and_css` (`BotasaurusConfig`, opt-in) wired
into both real-navigation Botasaurus paths as real `botasaurus_driver.
Driver` kwargs — live-verified: a real launch with `block_images=True`
showed the page's `<img>` tag present in the DOM but never loaded
(`naturalWidth` stayed 0).

**Extensions, lang/locale/timezone, mouse simulation, CDP network capture
(Round 60).** Four more opt-in `BotasaurusConfig` fields, each independently
implemented and live-verified: `extensions` via new
`browser/_botasaurus_extension.py::LocalExtension` (Driver needs
`.load()`-exposing objects, not raw paths); `driver.
set_locale_and_timezone()` for locale/timezone spoof (live-verified
correct); `driver.enable_human_mode()` + humanized per-scroll-pass
`move_mouse_to_point()`; and raw CDP request/response capture via new
`browser/_botasaurus_network_capture.py`, persisted as
`FetchResult.network_events` / `scrape_results.network_events` (migration
`010`). **Known, documented limitation:** `Driver(lang=...)` was live-tested
to have zero effect on `navigator.language`/`Accept-Language` despite its
own docstring's claim — kept as a real but ineffective config field rather
than silently dropped, since a JS-injection workaround hit a separate
confirmed upstream CDP bug (`Page.enable()` CBOR error), out of scope to
chase. A same-session independent review before merge found and fixed 3
real defects: a resource leak in `_new_driver_fetch` (3 new post-launch
calls sat outside the existing try/except, reintroducing round-41's
display-contention precondition on failure), `GET /v1/jobs/{id}` never
returning the new `network_events` column, and reused-driver fetches
silently dropping network-event capture into a dead first-call list
(CDP hooks are tab-scoped, registered once at launch — fixed with a
redirect indirection).

Full detail for rounds 57-60: `.claude/knowledge/technical-debt.md`'s
per-round entries.

---

## Scrapling Engine + Structured Extraction (Round 28)

Two modules — `fetcher/scrapling_wrapper.py::ScraplingWrapper` and
`fetcher/adaptive_selector.py::AdaptiveSelector` — existed fully unit-tested
but with zero production callers until this round; both are now real.

**Scrapling engine (`Level1Fetcher`'s third first-attempt path):**
`base.yaml`'s `levels.level_1.engine: scrapling` was declared config but
never read — L1 always used plain httpx regardless. `fetcher/factory.py::
build_level1_fetcher` now constructs a `ScraplingWrapper` whenever
`engine == "scrapling"` and threads it in as `scrapling_client`. Dispatch
order in `Level1Fetcher.fetch()`: JA3 client (if enabled) → Scrapling (if
engine says so) → plain httpx fallback — same first-attempt/fallback shape
as every other engine chain in this codebase (L2's Botasaurus-then-
Camoufox, L1's own JA3-then-httpx).

`ScraplingWrapper.fetch()` always calls `scrapling.fetchers.AsyncFetcher.
get(..., follow_redirects=False)` and returns a raw `ScraplingResponse
(status_code, text, location)` — it does **not** follow redirects itself.
`Level1Fetcher._fetch_via_scrapling` drives its own redirect loop over
that response, revalidating `self._ssrf_guard` on every hop before
following it (spec §1.1 #4 — see `.claude/knowledge/decisions.md` →
"Scrapling Engine — Manual Redirect Loop" for the full why/alternatives).
Real dependency gotcha: `scrapling==0.4.11` alone doesn't install
`curl_cffi`, which `AsyncFetcher` needs at import time — fixed by
declaring `curl_cffi>=0.15.0` directly rather than the `scrapling
[fetchers]` extra, which pins a `playwright` version that conflicts with
`camoufox`. See `.claude/knowledge/operations.md` Known Operational Gaps
#12 and `.claude/knowledge/technical-debt.md`.

**Structured extraction (`Worker.process_job`'s post-fetch step):**
`core.models.FetchResult.extracted` and `ConfigOverrides.extraction_schema`
were both declared and even already *persisted*
(`orchestrator/tasks.py` already `json.dumps`'d `result.extracted`) but
nothing ever populated the field. Wired once, centrally, in
`orchestrator/worker.py::Worker.process_job`, immediately after any
level's fetch succeeds and before the result is appended — applies
uniformly regardless of which level (L1/L2/L3) actually won, with zero
duplication across the three fetcher classes. Calls `AdaptiveSelector()
.extract(result.html, schema=request.config_overrides.extraction_schema
if request.config_overrides else None)` — content/title/link extraction
via bs4 selectors (falls back to regex if `bs4` isn't importable), `schema`
just echoed back into the result if the caller provided one (the class
doesn't do schema-guided extraction beyond that yet).

**Live-verified for real**, not just unit-tested:
`tests/live/test_scrapling_engine_wiring.py` proves the factory-built
client is real, a plain GET/2-hop redirect/404 all work against real
`httpbin.org`/`example.com` traffic, and `AdaptiveSelector` correctly
extracts title+content from real HTML (and correctly omits `title` when a
real page genuinely has none). Full story, including the live-testing
process that found the `curl_cffi` gap: `.claude/knowledge/
technical-debt.md`.

**Round 29 addendum — markdown conversion moved here too.** Firecrawl
markdown conversion (`services/firecrawl_client.py`) previously lived
entirely inside `fetcher/level_1.py` (three separate inline call sites,
one per L1 internal code path) and so never ran for a URL that had to
escalate to L2/L3. It's now called from the exact same spot as
`AdaptiveSelector` above — right after any level's fetch succeeds, before
`results.append(result)` — for the identical "applies regardless of which
level won" reason. `Level1Fetcher` no longer references Firecrawl at all.
`build_firecrawl_client()` also now accepts `FIRECRAWL_BASE_URL` (a
self-hosted Firecrawl instance) as an alternative to `FIRECRAWL_API_KEY` —
a self-hosted instance typically needs no key, so either one alone is
enough to build a working client. See `.claude/knowledge/decisions.md` →
"Markdown Conversion Centralized in Worker.process_job" for the full
before/after and alternatives considered.

---

## Metrics: Cross-Process Emission Pattern (Round 25)

**The problem, generalized:** this project runs several distinct process
types — the `api` process (serves `/metrics`), rq worker processes (execute
jobs), the `proxy-harvester` daemon. `prometheus_client`'s `REGISTRY` is
in-process global state. A `Counter`/`Gauge` incremented or set inside a
worker or harvester process is invisible to `/metrics`, because that's a
different process's memory entirely. This is worse than it sounds for rq
specifically: rq forks a brand-new "work horse" process **per job** that
`os._exit()`s immediately after — even a well-intentioned in-process metric
there is gone before the next Prometheus scrape could ever see it. This bit
round 25 twice: the 7 originally-dead alert metrics, and — missed by the
first pass, found only via a live `/metrics` cross-check — `proxy_source_healthy`.

**The fix, applied consistently:** never rely on in-process Prometheus
objects for anything set outside the `api` process. Instead:
1. At event time (inside whichever process the event happens in), write a
   plain value to Redis (`redis.raw.set`/`incr`) or query Postgres directly.
2. At scrape time (inside `api/routes.py`'s `/metrics` handler, the *only*
   process that matters here), read that Redis/Postgres state back and set
   the local `Gauge` right before `generate_latest(REGISTRY)` runs.

This is not a new pattern invented in round 25 — `observability/metrics.py`'s
original `proxy_pool_validated_count` already worked this way (a live
Postgres `COUNT(*)` query at scrape time). Round 25 just applied it
everywhere a metric's event and its scrape don't share a process:
`dlq_size`, `capsolver_daily_spend`/`capsolver_daily_ceiling`,
`circuit_breaker_trips_total`, `proxy_exhausted_total`,
`job_duration_seconds_count`/`_sum`, `proxy_source_healthy`. The one
exception is `http_requests_total` — a normal in-process `Counter`, because
HTTP requests and the `/metrics` scrape both happen in the same long-lived
`api` process; no cross-process problem to work around there.

**When adding a new metric:** ask first which process the event happens in.
If it's not the `api` process, this pattern is required — see
`observability/metrics.py`'s `refresh_*` functions for the exact shape to
copy. Also see `.claude/knowledge/standards.md` → "Prometheus Gauges".

---

## Data Flow

```
API Client → FastAPI (/v1/scrape) → TenantResolver → SSRFGuard → QuotaManager → DB Insert → RQ Queue → Worker
                                                ├─ CircuitBreaker.allow_request()
                                                ├─ PolitenessController.acquire_slot()
                                                ├─ ProxyManager.get_proxy() → ProxyHarvester.harvest_once()
                                                ├─ Level1/2/3Fetcher.fetch() → FetchResult
                                                └─ DedupEngine → PostgresClient → proxy_pool
```

---

## Repository Layout (Round 27)

All application code lives under one installable package,
`src/scraper_engine/` — a consolidation of what were previously 12 separate
top-level packages (`api/`, `browser/`, `cli/`, `config/`, `core/`,
`fetcher/`, `observability/`, `orchestrator/`, `proxy/`, `services/`,
`storage/`, `scrapy_project/`). Import as `scraper_engine.core`,
`scraper_engine.proxy`, etc. Full rationale, alternatives considered, and
the real bugs found doing the move: `.claude/knowledge/decisions.md` →
"src/ Layout Over Flat Top-Level Packages"; the specific gotchas (bare
dotted-import rebinding, string-based module references, stub/real-type
drift): `.claude/knowledge/troubleshooting.md`, round-27 entries.

`tests/` and `migrations/` are project-level, not part of the installable
package — they stay at repo root, unmoved, per standard src-layout
convention. `tests/fixtures/` holds real, actively-used test
infrastructure: `challenge_mirror/` (self-hosted Cloudflare-like test
target, BD-05) — a genuine working component, not scratch, which is why it
lives under `tests/` rather than being archived. `judge_server.py` used to
live here too, but round 32 found it wasn't actually test-only — it's a
real runtime dependency of `proxy/harvester.py`'s production validation
path (nothing else ever started it, which is exactly why that path was
silently broken in every real deployment). Promoted to
`src/scraper_engine/proxy/judge_server.py`; the promotion integration test
now imports and starts it directly from there instead of via a
subprocess pointed at a fixture path.

Two directories exist purely as local, gitignored scratch space — never
pushed to GitHub, but not deleted either:
- **`.archive/{evidence,directive,closure,other}/`** — 60+ historical
  per-round evidence/directive/closure reports, categorized by type.
  Point-in-time snapshots, not living documentation; `.claude/MEMORY.md` and
  this knowledge base are the living record.
- **`.local/`** — the authoritative design spec
  (`.local/specs/scraper-engine-blueprint-v2.md`), a confirmed-duplicate directory,
  and unused manual scripts. Deliberately kept separate from `.archive/`
  (see the decisions.md entry above for why the split, not a single
  merged folder).

`docs/` (tracked, real) now holds only living reference material:
`docs/reference/api-reference.md`, `docs/guides/deployment.md`. Root-level
`README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE` (Apache 2.0),
`NOTICE` are standard OSS hygiene files, added round 27.

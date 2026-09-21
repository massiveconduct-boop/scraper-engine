# config/schema.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LevelConfig(BaseModel):
    # Round 25: Botasaurus is real again (fetcher/factory.py constructs a
    # BotasaurusWrapper whenever "botasaurus" appears in this value) — L2
    # tries it first, falling back to Camoufox on failure or a detected
    # challenge page. "camoufox" alone is still valid: it skips the
    # Botasaurus attempt entirely, same as L3.
    engine: Literal["scrapling", "camoufox", "botasaurus+camoufox"]
    proxy_tier_min_score: float
    timeout_seconds: int
    # Round 47 — base.yaml's level_2/level_3 values are now a shared
    # ${CAPSOLVER_ENABLED:true} placeholder, not a hardcoded literal: this
    # gates real spend (CapSolver's $1.00/day ceiling), and a source-blind
    # consuming service had no way to turn it off without a rebuild.
    capsolver_enabled: bool = False
    # L2/L3 wait strategy — config-driven, not hardcoded (round 12.1)
    goto_wait_until: str = "load"
    networkidle_timeout_ms: int = 5000
    max_total_wait_ms: int = 30000
    post_load_fixed_wait_ms: int = 10000
    retry_wait_increment_ms: int = 5000
    # Lazy-load / infinite-scroll (round 15 follow-up). scroll_passes=0 disables;
    # >0 scrolls to bottom up to N times, early-exiting when height stops growing.
    scroll_passes: int = 0
    scroll_wait_ms: int = 1500


class LevelsConfig(BaseModel):
    level_1: LevelConfig = LevelConfig(
        engine="scrapling", proxy_tier_min_score=40.0, timeout_seconds=20
    )
    level_2: LevelConfig = LevelConfig(
        engine="botasaurus+camoufox",
        proxy_tier_min_score=70.0,
        timeout_seconds=40,
        capsolver_enabled=True,
    )
    level_3: LevelConfig = LevelConfig(
        engine="camoufox",
        proxy_tier_min_score=90.0,
        timeout_seconds=60,
        capsolver_enabled=True,
    )


class CamoufoxConfig(BaseModel):
    geoip: bool = True
    humanize: float = Field(default=1.5, ge=0.0, le=5.0)
    headless_mode: str = "virtual"
    max_total_instances: int = 8
    # Round 59 — RAM-aware concurrency cap (core/budget.py::
    # resolve_browser_max_total_instances). Default off: a brand-new,
    # unvalidated-in-production capability that changes semaphore sizing
    # from a live runtime reading instead of a static number, same
    # opt-in-by-default convention as l1_ja3_client_enabled above. Can
    # only ever REDUCE max_total_instances at runtime, never raise it
    # above this configured ceiling.
    ram_aware_concurrency_enabled: bool = False
    # Measured 2026-08-17 (this session): one real headful Botasaurus/
    # Chromium launch (headless=False, enable_xvfb_virtual_display=True —
    # the exact shape production uses), full process tree (main + renderer/
    # GPU/utility subprocesses, isolated via before/after PID diff) =
    # 804.7MB RSS (~0.79GB), rounded up slightly for margin. Deliberately
    # NOT Camoufox's own measured 80.1MB headless figure (see
    # core/budget.py's "Measured 2026-07-22" comment) — BROWSER_SEMAPHORE
    # is shared across both engines, and Botasaurus (headful, via Xvfb) is
    # the heavier of the two, so calibrating against it is the
    # conservative choice.
    ram_aware_avg_instance_gb: float = 0.8
    # Round 46 — verified against Camoufox's own docs (Context7
    # /daijro/camoufox): for Firefox 149+ (we run 152), the library's own
    # README/docs explicitly recommend fingerprint_preset=True — it samples
    # a REAL, captured browser fingerprint (312 real presets bundled for
    # our version) instead of a synthetic/statistically-generated one,
    # officially described as more convincing evasion. `os` is pinned to
    # "linux" (not randomized to windows/macos) because Camoufox's own
    # "Known Limitations" doc explicitly warns the opposite is
    # counterproductive: "it is recommended to run Camoufox on the OS that
    # matches the fingerprint profile... the per-context patches are
    # designed to make each context appear as a different person on the
    # same OS, not to impersonate a different OS" — every worker here runs
    # Linux (Docker), so a Windows/macOS fingerprint would create exactly
    # the OS-level/JS-fingerprint mismatch that advanced bot detection
    # looks for.
    fingerprint_preset: bool = True
    os: str = "linux"


class BotasaurusConfig(BaseModel):
    """Round 26 capability-upgrade knobs — every field here maps to a real,
    verified botasaurus/botasaurus_driver kwarg or call (verified against the
    installed 4.0.97/4.0.93/4.0.38 source, not just the README — see
    .claude/knowledge/architecture.md -> "Botasaurus Integration").

    Defaults turn on the strict upgrades over the round-25 baseline
    (bypass_cloudflare, tiny_profile, the anti-detection args, hashed
    fingerprint pairing) since they're free wins with no behavior regression.
    max_retry defaults to 0 (off) to keep today's single-attempt behavior
    unless explicitly opted into. l1_ja3_client_enabled defaults to False —
    a brand-new L1 code path with no live-traffic validation yet.

    Round 47 — l1_ja3_client_enabled is now a base.yaml ${VAR:default}
    placeholder (BOTASAURUS_L1_JA3_CLIENT_ENABLED), same fix as
    DataImpulseConfig/LevelConfig.capsolver_enabled below: a
    source-blind consuming service couldn't opt into this without a
    rebuild.
    """

    bypass_cloudflare: bool = True
    tiny_profile: bool = True
    remove_default_browser_check_argument: bool = True
    close_on_crash: bool = True
    random_sleep_enabled: bool = True
    hashed_fingerprint: bool = True
    max_retry: int = 0
    l1_ja3_client_enabled: bool = False
    # Round 59 — real botasaurus_driver.Driver kwargs (verified against the
    # installed 4.0.93 source: core/browser.py applies them independently,
    # block_images_and_css is not a superset flag). Default off: blocking
    # images/CSS can break sites whose content or lazy-load/JS behavior
    # depends on them, so this is opt-in, not a default-on "free win" like
    # bypass_cloudflare above.
    block_images: bool = False
    block_images_and_css: bool = False
    # Round 60 — Driver(extensions=[...]) is real (installed
    # botasaurus_driver 4.0.100 driver.py:2074), but each item must be an
    # object exposing .load(with_command_line_option=False) -> str
    # (core/config.py:83-89's create_extensions_string), not a raw path
    # string — see browser/_botasaurus_extension.py::LocalExtension, which
    # wraps one of these directory paths. Default empty: no extension
    # artifact ships with this repo, so this is pure capability wiring
    # until a caller configures a path.
    extensions: list[str] = Field(default_factory=list)
    # Round 60 — three genuinely independent settings, not one value in two
    # formats. `lang` is the Driver ctor's real `--lang=` Chrome flag,
    # forwarded correctly (confirmed on the real chrome://version command
    # line) — but driver.py:2153's own docstring claim that it drives JS-
    # visible `navigator.language` did NOT hold up live: on the installed
    # botasaurus_driver 4.0.100 / Playwright Chromium 1228 build, neither
    # `--lang=de-DE` nor `--lang=de` changed navigator.language,
    # navigator.languages, or the Accept-Language request header (checked
    # via a real CDP before_request_sent hook) — verified live, not a docs
    # guess. A JS-injection workaround (CDP Page.addScriptToEvaluateOnNew
    # Document via driver.run_on_new_document()) was tried and hits a
    # separate real upstream bug: driver.run_cdp_command(cdp.page.enable())
    # itself throws ChromeException("Invalid parameters ... CBOR: map start
    # expected") in this installed version — confirmed not a general zero-
    # param-command issue (cdp.dom.enable()/cdp.runtime.enable() both
    # succeed the same way), so this is Page-domain-specific breakage in
    # the installed package, out of scope to patch here. Field kept and
    # wired anyway since it's a real, correctly-forwarded kwarg that may
    # behave differently on other Chromium builds — just don't rely on it
    # for navigator.language spoofing against this stack today.
    # `locale`/`timezone` are a separate, fully-working per-tab CDP
    # override via driver.set_locale_and_timezone() (ICU underscore locale,
    # e.g. "en_US", IANA timezone e.g. "America/New_York") driving
    # Intl/Date.toLocaleString formatting and the JS timezone, called before
    # first navigation on the fresh-launch path only (browser/botasaurus_
    # pool.py's _reuse_fetch does an in-page JS fetch(), not a navigation —
    # nothing to (re)apply there, same reasoning as round 58's autoscroll).
    lang: str | None = None
    locale: str | None = None
    timezone: str | None = None
    # Round 60 — driver.enable_human_mode() (driver.py:2123-2137) makes every
    # subsequent mouse call (move_mouse_to_point/click_at_point/etc.) route
    # through botasaurus_humancursor's curved-movement simulation instead of
    # an instant CDP jump. Explicit per-scroll-pass movement is wired via
    # browser/_botasaurus_scroll.py::botasaurus_autoscroll(humanize=...).
    # Default off: no live-traffic validation yet, and headless mouse-move
    # support isn't guaranteed identical to headful.
    humanize_mouse: bool = False
    # Round 60 — driver.before_request_sent()/after_response_received()
    # (driver.py:760,793) are real CDP hooks, live-verified to actually fire
    # with real request/response headers. Opt-in: captures full request/
    # response metadata for every network request a fetch makes, which can
    # be large and isn't needed by default.
    capture_network_events: bool = False
    # Round 64 — how many live Botasaurus drivers one job may keep. It was
    # exactly one, behind one lock, so a job's concurrent URLs queued
    # single-file at L2 (live: 43s, 65s, 89s, 58s, 134s for five URLs that
    # each took well under a minute alone). Parked drivers hold no
    # BROWSER_SEMAPHORE permit (only an in-flight fetch does), so this is
    # the bound on idle Chrome processes per job.
    max_pooled_drivers: int = Field(default=2, ge=1)


class ProxyHarvesterConfig(BaseModel):
    sources: list[str] = Field(
        default_factory=lambda: ["proxifly", "proxyscrape", "iplocate", "proxripper"]
    )
    interval_seconds: int = 600
    # The daemon runs three loops on independent timers (proxy/harvester_daemon.py).
    promotion_interval_seconds: int = 900
    health_interval_seconds: int = 300


class ProxyTierConfig(BaseModel):
    """Reliability-score gate ProxyManager.get_proxy() requires per escalation
    level. L3's own ceiling (min_score_level_3) can be genuinely unreachable
    with free-only proxy sources — allow_tier2_fallback_for_tier3 is a
    togglable stopgap letting L3 borrow a tier-2-caliber proxy instead of
    hard-exhausting, only tried after a real tier-3-caliber proxy search
    comes up empty. Meant to be flipped off again once paid/higher-quality
    proxy sources make min_score_level_3 reliably reachable on its own."""

    min_score_level_1: float = 40.0
    min_score_level_2: float = 70.0
    min_score_level_3: float = 90.0
    allow_tier2_fallback_for_tier3: bool = False
    # Round 39 — same single-hop stopgap as allow_tier2_fallback_for_tier3,
    # one tier down: L2 tries a real tier-2-caliber proxy first and only
    # falls back to a tier-1-caliber one if that search comes up genuinely
    # empty. Added after round 39's scoring-race/GREATEST-ratchet fixes
    # corrected years of silently-inflated scores back down to their real
    # values pool-wide — tier 2's *honest* supply crashed from a
    # fake-inflated ~45 to a real 4 in the same session, live-observed
    # starving a real production job (research_agent tenant, 47-URL batch,
    # only 10 succeeded, remainder DLQ'd as proxy_exhausted). Does not
    # cascade into a second hop (a tier-2 fallback never further falls to
    # tier... there is no tier 0) — same bounded shape as the tier-3 case.
    allow_tier1_fallback_for_tier2: bool = False
    # Pool-health thresholds (round 34, proxy/pool_health.py) — validated-proxy
    # counts per tier below which the tier is DEGRADED / CRITICAL. Independent
    # of the score gates above: those decide whether one request can find a
    # proxy, these decide whether the pool as a whole is healthy enough to
    # keep serving requests without emptying out.
    degraded_below_count: int = 20
    critical_below_count: int = 5


class PolitenessConfig(BaseModel):
    default_concurrency: int = 2
    default_delay_seconds: float = 5.0
    slot_ttl_seconds: int = 120
    # Round 61 — orchestrator/worker.py's per-level slot-acquisition retry
    # budget. Before this, a busy slot got exactly one 1s nap before the
    # level loop moved on to the NEXT level (wrong: a busy slot means "wait,"
    # not "this level failed") — under concurrent same-domain dispatch
    # (max_concurrent_urls_per_job URLs racing default_concurrency slots,
    # which is usually a much smaller number), a URL could burn through all
    # 3 levels in ~3s of napping without a single real fetch attempt, then
    # permanently DLQ as "no attempt ever made." Now retries the SAME level
    # with slot_retry_interval_seconds backoff until slot_wait_timeout_seconds
    # of real wall-clock elapses, giving concurrent siblings genuine time to
    # finish and release their slot before conceding.
    # Round 63 — raised from 30.0. 30s could not cover even ONE slot-holder:
    # a worst-case Level-3 attempt is ~85s of configured waits alone
    # (post_load_fixed_wait_ms + max_total_wait_ms + 10 scroll passes + a
    # post-captcha re-poll) on top of level_3.timeout_seconds for the
    # navigation itself. So under concurrent same-domain dispatch the losing
    # siblings reliably ran out the budget on EVERY level and the URL DLQ'd
    # having never once been fetched — round 61 made the wait real but left
    # it an order of magnitude too short, which is what an external consumer
    # hit as "a 5-URL job never completed". 300s covers two full L3 holders
    # deep. Round 63 also stopped a timeout here from advancing to the next
    # level (a busy slot never said anything about the current level), so
    # this budget is now the URL's whole politeness allowance, not a
    # per-level one.
    slot_wait_timeout_seconds: float = 300.0
    slot_retry_interval_seconds: float = 1.0
    # Round 63 — ceilings for the per-request politeness overrides on
    # core/models.py::ConfigOverrides. A caller running a trusted bulk crawl
    # can raise concurrency and drop the delay for its own job, but only
    # within these operator-set bounds: orchestrator/worker.py clamps every
    # request against them, so the blast radius of a caller asking for "as
    # fast as possible" stays something the operator chose.
    max_request_concurrency: int = 10
    min_request_delay_seconds: float = 0.5
    # Round 49 — orchestrator/worker.py::Worker.process_job's per-job URL
    # dispatch semaphore size. Was strictly sequential before this (root
    # cause of slow large-batch job runs, round 45). Deliberately below
    # core.budget.BROWSER_SEMAPHORE's size (8) so one job doesn't already
    # saturate the whole worker process's browser budget on its own —
    # this bounds "how many URLs from THIS job are in flight at once,"
    # BROWSER_SEMAPHORE separately bounds "how many live browsers exist in
    # this process across every job," and a concurrent task simply queues
    # on BROWSER_SEMAPHORE once this job's own budget is saturated.
    max_concurrent_urls_per_job: int = 5


class EscalationConfig(BaseModel):
    """Round 63 — cross-job memory of which level actually works for a domain.

    The L1->L2->L3 ladder was entered at L1 for every URL of every job, with
    no record anywhere of what had just worked. For a domain that only ever
    succeeds at L3 that means paying a doomed L1 attempt plus a doomed L2
    browser launch before the one attempt that can work — measured by an
    external consumer at ~140s of the 169s a single Jumia product page spent
    in PROCESSING, against a 27.6s real fetch. `level_used` was already
    written to scrape_results; nothing read it back to decide anything.

    orchestrator/level_memory.py stores the hint. It only ever SKIPS levels
    that recently failed for this domain — escalation above the hint is
    untouched, so the hint can make a job faster but never make a fetch that
    would have succeeded fail.
    """

    level_memory_enabled: bool = True
    # Round 64: 3600 -> 86400. Round 63 kept this short so a stale hint
    # could cost at most an hour of unnecessary high levels, but that job is
    # already done by `reprobe_every` (every Nth URL runs the full ladder and
    # rewrites the hint), independently of the TTL. The short TTL only
    # bought a full-ladder climb on every URL of any crawl that started more
    # than an hour after the last one — live, a 10-URL Jumia rerun paid
    # L1+L2 on every URL for exactly that reason. A day covers the common
    # "same site again later today" shape; the re-probe covers staleness.
    level_memory_ttl_seconds: int = 86400
    # Staleness guard: every Nth URL for a domain ignores the hint and runs
    # the full ladder, so a target that gets EASIER (challenge lifted, WAF
    # rule relaxed) is rediscovered instead of paying L3 forever. Without
    # this the hint is a one-way ratchet — the TTL alone would only re-probe
    # after a full hour of inactivity, which a continuous crawl never has.
    # 0 disables re-probing.
    reprobe_every: int = 20


class ExtractionConfig(BaseModel):
    """Round 63 — limits for fetcher/adaptive_selector.py's extraction."""

    # Was a hardcoded `links[:100]` slice of the raw DOM order. On a real
    # Jumia catalog page rendered at L3 the hydrated nav mega-menu alone
    # supplies ~100 links before the first product link, so the cap filled
    # with site navigation and returned ZERO product URLs — while the same
    # page snapshotted earlier at L2 (before hydration) returned them all.
    # That read as "L3 loses product links"; it was the cap. Kept as a limit
    # rather than removed so a pathological page can't return a
    # multi-megabyte link list, but set well above any real page's nav.
    max_links: int = 1000


class CircuitBreakerConfig(BaseModel):
    failure_threshold: float = 0.95
    attempt_threshold: int = 20
    cooldown_seconds: int = 600
    # Round 43 — reduced from 3600s (1hr). A scraping job stalled for an
    # hour on a domain that's likely recovered within minutes is a heavy
    # cost for a system whose job timeouts are already scaled in single-
    # digit minutes per URL; 1hr was calibrated for a much higher-stakes
    # circuit (e.g. a payments API) than "come back and try this domain
    # again." 20 minutes still gives 2 full exponential doublings of
    # meaningful backoff (10min -> 20min) before capping, still enough to
    # break a thundering-herd re-attack pattern.
    max_cooldown_seconds: int = 1200
    # Round 43 — how long a failure streak stays "live" before Redis expires
    # it. Without this, failures from one job (e.g. a crashed or hard-killed
    # run) sit forever and silently feed an unrelated later job's trip
    # decision. See orchestrator/circuit_breaker.py's constructor docstring.
    failure_streak_ttl_seconds: int = 600


class CapSolverConfig(BaseModel):
    # Per-tenant ceiling default lives in the `tenants.capsolver_daily_credit_ceiling`
    # DB column (round 25) — not duplicated here to avoid two conflicting sources
    # of truth. CapSolverBudget falls back to its own DEFAULT_DAILY_CEILING ($1.00)
    # only when no tenant row / pg client is available.
    max_concurrent_solves: int = 10


class SSRFGuardConfig(BaseModel):
    additional_denied_cidrs: list[str] = Field(default_factory=list)


class ObservabilityConfig(BaseModel):
    metrics_enabled: bool = True
    tracing_enabled: bool = True
    logging_level: str = "INFO"
    # OTLPSpanExporter's own default (localhost:4317) resolves inside whichever
    # container is exporting, never reaching the separate jaeger service — this
    # is the single source of truth every process points at instead.
    otlp_endpoint: str = "http://jaeger:4317"


class PgBouncerConfig(BaseModel):
    """Informational only (round 25) — nothing reads these fields at runtime.

    The real PgBouncer process is configured entirely by the static
    infra/pgbouncer/pgbouncer.ini file plus docker-compose.yml env vars.
    Editing base.yaml's pgbouncer: section has zero effect on the deployed
    pooler; these values exist to document what pgbouncer.ini is set to, kept
    in sync by hand. Templating pgbouncer.ini from this config would be the
    real fix, but that's a deploy-tooling change, out of scope here."""

    pool_mode: str = "transaction"
    max_client_conn: int = 500  # [CONFIRMED — BD-06]
    default_pool_size: int = 20


class WebhookConfig(BaseModel):
    """Retry/timeout knobs for orchestrator/webhook.py::WebhookDispatcher
    (round 34 — previously hardcoded in the class's own __init__ defaults).
    ops_webhook_url is a distinct, operator-level sink (proxy pool health
    transitions, see proxy/pool_health.py) — not scoped to any one tenant's
    job, so it lives here rather than on the per-job scrape_jobs.webhook_url
    column."""

    max_retries: int = 3
    timeout_seconds: int = 10
    backoff_base_seconds: float = 2.0
    ops_webhook_url: str | None = None


class DlqReaperConfig(BaseModel):
    """proxy/dlq_reaper.py tuning (round 34) — auto-retries DLQ entries in
    orchestrator/worker.py's TRANSIENT_FAILURE_CATEGORIES once the condition
    that DLQ'd them has since cleared. max_auto_retries caps re-attempts per
    entry so a flapping pool/circuit can't loop a job forever."""

    interval_seconds: int = 60
    max_auto_retries: int = 3
    batch_size_per_tenant: int = 20


class SessionRetentionConfig(BaseModel):
    browser_sessions_ttl_days: int = 30
    domain_ban_history_retention_days: int = 7
    cleanup_interval_seconds: int = 3600


class StorageConfig(BaseModel):
    """Single source of truth for the database and Redis connection strings.

    The application connects to Postgres with raw asyncpg, which needs a plain
    ``postgresql://`` DSN. Alembic/SQLAlchemy (in alembic.ini) use the
    ``postgresql+asyncpg://`` form instead — a different consumer with a
    different format. The validator below strips any ``+driver`` suffix so a
    value written in the SQLAlchemy form still works for asyncpg here.

    Defaults point at the docker-compose service names and route the database
    through PgBouncer (invariant G-05). Override per environment via the
    ``${DATABASE_URL}`` / ``${REDIS_URL}`` placeholders in base.yaml.
    """

    database_url: str = "postgresql://scraper:scraper@pgbouncer:6432/scraper_engine"
    redis_url: str = "redis://redis:6379/0"

    @field_validator("database_url")
    @classmethod
    def _strip_sqlalchemy_driver(cls, v: str) -> str:
        # asyncpg.create_pool rejects the SQLAlchemy "postgresql+asyncpg://" form;
        # normalise it down to the plain scheme asyncpg expects.
        if v.startswith("postgresql+"):
            return "postgresql://" + v.split("://", 1)[1]
        return v


class S3Config(BaseModel):
    """Object storage for HTML snapshots (scrape_results.html_snapshot_url).

    Defaults point at the docker-compose MinIO service. Override per
    environment via the ``${S3_*}`` placeholders in base.yaml.
    """

    endpoint_url: str = "http://minio:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "scraper-snapshots"


class DataImpulseConfig(BaseModel):
    """Toggle for the paid rotating-gateway proxy source (round 40). Disabled
    by default so the free-pool system (proxy/manager.py) behaves exactly as
    before until explicitly turned on — see proxy/paid_gateway.py for the
    gateway itself and orchestrator/worker.py::_fetch_with_proxy for the
    strategy branch. Host/port/credentials are deliberately NOT here — same
    split as CapSolverConfig: tuning lives in config, secrets are read
    directly via os.environ.get() in the provider's own factory function.

    Round 47 — both fields below are ${VAR:default} placeholders in
    base.yaml (DATAIMPULSE_ENABLED / DATAIMPULSE_STRATEGY), not literal
    values, so a deployment can flip this purely via container env — no
    source edit, no image rebuild. Fixes a real gap: a consuming service
    with credentials already reaching its container via env had no way to
    actually turn the gateway on, since this was previously the one
    hardcoded, non-overridable setting in the whole config file."""

    enabled: bool = False
    # free_only: unchanged today's behavior. paid_only: L2/L3 skip the scored
    # free pool entirely, always use the gateway. free_first: try the free
    # pool as today, only fall to the gateway on ProxyPoolExhaustedError.
    strategy: Literal["free_only", "paid_only", "free_first"] = "free_only"
    # Round 62 — ISO-3166 alpha-2 exit country for the gateway, rendered as
    # DataImpulse's `__cr.<iso2>` username parameter (proxy/paid_gateway.py).
    # Empty means "no country pin, gateway picks" — the pre-round-62
    # behavior. Tuning, not a secret, so it lives here rather than in env
    # alongside the credentials, same split the module's docstring sets out.
    country: str = ""
    # Round 62 — how many EXTRA gateway attempts, each on a brand-new sticky
    # session (= a brand-new exit IP), a level gets after a DETECTION_BLOCK.
    # 0 restores the pre-round-62 behavior of accepting the first block as
    # final. The ceiling is deliberately low: every retry is a full browser
    # render through paid residential bandwidth, and a target that blocks 3
    # distinct residential IPs in a row is not blocking on IP reputation.
    rotate_on_block_retries: int = Field(default=2, ge=0, le=10)
    # Round 62 — pin the gateway's exit to one autonomous system, as
    # DataImpulse's `__asn.<number>` username parameter (bare AS number, no
    # "AS" prefix). None is the default and costs nothing; setting it DOUBLES
    # the bandwidth bill, per DataImpulse's own docs, so it is opt-in per
    # deployment rather than a global default. proxy/paid_gateway.py's module
    # docstring has the measurement: on the Jumia target from
    # DEVELOPER_REPORT.md one ASN was 0-for-9 against Cloudflare and made up
    # most of the country's pool, which took the unpinned success rate to
    # 1-in-12; pinning a clean ASN took it to 12-of-12. The documented
    # `noasn` exclusion parameter is deliberately NOT modelled here — it is
    # accepted by the gateway and then ignored, verified live.
    asn: int | None = None

    @field_validator("asn", mode="before")
    @classmethod
    def _empty_asn_is_none(cls, v: object) -> object:
        """base.yaml renders this as `${DATAIMPULSE_ASN:}`, and an unset env
        var leaves the empty STRING, not None — the loader substitutes text
        and never re-types it. Without this, the default config fails
        validation outright ("unable to parse string as an integer"), i.e.
        every process refuses to start unless the var happens to be set."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _asn_requires_country(self) -> DataImpulseConfig:
        """An ASN pin without a country pin is rejected by the gateway.

        Verified live 2026-09-20: `login__asn.29465;sessid.N` fails proxy
        auth on 6 of 6 attempts, while `login__cr.ng;asn.29465;sessid.N`
        succeeds on 6 of 6. DataImpulse will not resolve an `asn.` parameter
        that has no `cr.` alongside it.

        Caught the hard way — `DATAIMPULSE_ASN` was set without
        `DATAIMPULSE_COUNTRY` and every gateway request 407'd at fetch time,
        which surfaces as an opaque per-request proxy failure rather than
        anything pointing at config. Failing at load time instead means the
        process refuses to start with a message naming the actual fix, in
        the same spirit as Worker.__init__'s eager build_gateway_proxy()
        check (a bad gateway config should fail loud once, not degrade every
        fetch silently).
        """
        if self.asn is not None and not self.country.strip():
            raise ValueError(
                "dataimpulse.asn is set but dataimpulse.country is empty. "
                "DataImpulse rejects an `asn.` username parameter with no `cr.` "
                "alongside it (407). Set DATAIMPULSE_COUNTRY (e.g. 'ng' for "
                "AS29465 MTN Nigeria), or unset DATAIMPULSE_ASN."
            )
        return self


class AppConfig(BaseModel):
    """Root configuration schema matching config/base.yaml."""

    tenant_id: str | None = None  # only for log enrichment (ContextVar), never for routing
    storage: StorageConfig = Field(default_factory=StorageConfig)
    s3: S3Config = Field(default_factory=S3Config)
    levels: LevelsConfig = Field(default_factory=LevelsConfig)
    camoufox: CamoufoxConfig = Field(default_factory=CamoufoxConfig)
    botasaurus: BotasaurusConfig = Field(default_factory=BotasaurusConfig)
    proxy_harvester: ProxyHarvesterConfig = Field(default_factory=ProxyHarvesterConfig)
    proxy_tiers: ProxyTierConfig = Field(default_factory=ProxyTierConfig)
    dataimpulse: DataImpulseConfig = Field(default_factory=DataImpulseConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    capsolver: CapSolverConfig = Field(default_factory=CapSolverConfig)
    ssrf_guard: SSRFGuardConfig = Field(default_factory=SSRFGuardConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    pgbouncer: PgBouncerConfig = Field(default_factory=PgBouncerConfig)
    session_retention: SessionRetentionConfig = Field(default_factory=SessionRetentionConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    dlq_reaper: DlqReaperConfig = Field(default_factory=DlqReaperConfig)

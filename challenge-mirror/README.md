# Challenge Mirror — Self-Hosted L2/L3 Test Target (BD-05)

## Changelog

- **2026-07-22 — Fixed strict-tier PoW performance bug.** The auditable verification
  report showed real Camoufox timing out at 60s on the strict tier, attributed to
  "VPS CPU-bound... Camoufox JS engine throughput insufficient." That framing was
  wrong: the original solver called `crypto.subtle.digest` (Web Crypto's async
  SubtleCrypto API, which has no synchronous form) once per PoW attempt. At ~2^20
  expected attempts for the strict tier, that's ~1M awaited microtasks — the
  scheduling overhead, not hash throughput, was what timed out. Replaced with a
  synchronous, from-scratch SHA-256 (verified bit-for-bit against Python's
  `hashlib.sha256` across padding-boundary edge cases — see `../sha256-verification/`
  — before being ported to JS), run in a tight loop with an occasional yield every
  200,000 attempts. Re-verified by executing the actual server-emitted `<script>` in
  a real V8 context (`tests/node_real_js_verify.js`, not a reimplementation):
  **strict tier now solves in ~11.6s, standard tier in ~1.5s**, both round-trips
  included. 7/7 existing pytest-suite tests still pass unchanged.



Resolves BD-05 from blueprint v2.0 and closes G-01/G-07 from the production-readiness
gap audit: a real, owned, legally-clean HTTP service that requires actual JavaScript
execution to pass, so the escalation ladder's Level 2 and Level 3 paths can be proven
to work end-to-end instead of only Level 1 (which is all the prior "5/5 live scraping"
evidence covered).

## What it actually verified (not just what it's designed to do)

Run directly against this container, with no mocks:

```
$ python3 tests/manual_verify.py
=== difficulty=standard bad_signals=False ===
  [ok] plain HTTP client correctly blocked (challenge page served, not content)
  solved nonce=10933 in 0.016s
  [ok] verification accepted
  [ok] authenticated session now sees real content

=== difficulty=strict bad_signals=False ===
  [ok] plain HTTP client correctly blocked
  solved nonce=407639 in 0.440s
  [ok] verification accepted
  [ok] authenticated session now sees real content

=== difficulty=standard bad_signals=True ===
  [ok] plain HTTP client correctly blocked
  [ok] verification correctly REJECTED for bot-like signals: navigator_webdriver_true

ALL MANUAL VERIFICATION FLOWS PASSED
```

7/7 tests in `tests/test_challenge_mirror.py` pass against the live server (run
without pytest available in this sandbox by executing the test functions directly —
see the file for the exact commands; wire into real `pytest` in CI where it's on the
path).

## Design

- **Two difficulty tiers**, mapped to the two levels they're meant to exercise:
  - `standard` (4 hex-zero PoW prefix, no minimum delay) → targets **Level 2**
  - `strict` (5 hex-zero PoW prefix, 3s minimum solve-to-submit delay) → targets **Level 3**,
    on the theory that Level 2's shorter per-request timeout budget should legitimately
    fail against the forced delay, giving the orchestrator a real (not simulated) reason
    to escalate.
- **No JS engine = no content.** A plain `requests.get()` (i.e., what Scrapling's Level 1
  does) will only ever see the challenge HTML, never the real content — this is
  structural, not a heuristic, so `test_l1_correctly_fails_against_standard_challenge`
  has a hard guarantee behind it.
- **Automation-tell checks** (`navigator.webdriver`, `navigator.languages`,
  `navigator.plugins.length`) are evaluated server-side on the signals the client JS
  reports. A correctly configured Camoufox session (per blueprint v2 §3.4 — real
  `AsyncCamoufox`, `geoip=True`, no manual JS injection) should satisfy these by
  construction. A regression back to raw Playwright with hand-rolled overrides (the
  original v1.0 defect, F-02/F-03) would fail here, which is the point — see
  `test_naive_undetected_automation_signal_is_correctly_rejected` in
  `tests/test_escalation_ladder.py` (currently `xfail`/skip pending a test seam in
  `Level2Fetcher` — see that file's docstring; do not silently delete this test).

## What this does NOT prove

- It does not prove the system defeats any *specific* commercial anti-bot product
  (Cloudflare, DataDome, PerimeterX, Akamai). Those are materially harder and change
  frequently; passing this mirror is a necessary, not sufficient, condition for
  "the escalation ladder works." Treat a pass here as "the plumbing is correct,"
  not as "Level 3 will beat Cloudflare."
- It does not exercise CAPTCHA-solving (CapSolver) — the PoW mechanism here is
  intentionally solvable by any JS engine without a third-party solver, so it can run
  in CI without external network calls or spend. CapSolver still needs its own
  sandbox-mode live test (see gap audit G-08) — separate from this fixture.

## Running locally

```bash
cd challenge-mirror
export CHALLENGE_MIRROR_SECRET_KEY=$(openssl rand -hex 32)
python3 -m app.server                 # listens on :8090
python3 tests/manual_verify.py        # end-to-end proof, no pytest needed
```

## Running in Docker / CI

```bash
docker build -t challenge-mirror .
docker run --rm -p 8090:8090 -e CHALLENGE_MIRROR_SECRET_KEY=$(openssl rand -hex 32) challenge-mirror
```

See `docker-compose.snippet.yml` for wiring it into the main scraper-engine
`docker-compose.yml` as an internal-only service (never expose port 8090 publicly —
it has no rate limiting or abuse protection; it is a test fixture, not a hardened
service).

## Files

```
challenge-mirror/
├── app/
│   ├── __init__.py
│   └── server.py                    # the mirror itself — stdlib only + itsdangerous
├── tests/
│   ├── manual_verify.py             # dependency-light end-to-end proof (no pytest needed)
│   ├── test_challenge_mirror.py     # pytest suite for the mirror itself
│   └── test_escalation_ladder.py    # DROP INTO scraper-engine/tests/live/ — tests L1/L2/L3 against this mirror
├── Dockerfile
├── docker-compose.snippet.yml
└── README.md (this file)
```

## Closure mapping

| Gap audit finding | How this closes it |
|---|---|
| G-01 (no L2/L3 execution evidence) | `test_l2_solves_standard_challenge`, `test_l2_times_out_against_strict_challenge_and_escalates_to_l3` give L2 and L3 each a concrete, runnable, non-mocked pass/fail signal |
| G-07 (BD-05 unresolved / report self-contradiction) | This *is* the mirror the resolution table claimed existed; wire `CHALLENGE_MIRROR_URL` into the CI live-test job and the resolution table becomes true instead of aspirational |
| G-11 partial (SSRF) | Not addressed here — still needs the redirect-chain test from the gap audit, run against a *different*, dedicated redirect-test fixture, not this one |

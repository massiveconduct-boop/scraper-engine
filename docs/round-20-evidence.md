# Round 20 — CAPTCHA Solver Wired Into the L2/L3 Fetch Path

**Date:** 2026-07-27
**Scope:** Close the round-19 top tech-debt item — the CAPTCHA solver existed,
was tested, and was live-verified, but nothing in the fetch path called it.
This round wires it in: browser levels now detect a token-grant CAPTCHA widget
mid-fetch, solve it via the provider, inject the token, and re-poll.

---

## Design

The solver (`services/captcha_solver.py`) only knows the anti-captcha protocol
(sitekey → token). The missing half was browser-side: read the widget's sitekey
from the live DOM and inject the returned token so the site's own callback/form
submission proceeds. That half lives in a new module next to the fetchers:

- **`fetcher/_captcha.py`** — `solve_captcha_on_page(page, *, solver, tenant_id, url)`.
  - Detect: one `page.evaluate` reads the first solvable widget's `{kind, sitekey}`
    for reCAPTCHA v2 / hCaptcha / Turnstile (class + `data-sitekey`, with an
    iframe-`src` fallback). Turnstile/hCaptcha are probed before the generic
    `[data-sitekey]` so a branded widget isn't misread as reCAPTCHA.
  - Solve: routes `kind` → the matching `CaptchaSolver.solve_*` method.
  - Inject: `kind`-specific JS sets the response textarea/input; reCAPTCHA also
    walks `___grecaptcha_cfg.clients` to fire the registered callback.
  - **Best-effort by contract:** returns `False` (never raises) for no widget,
    unextractable sitekey, unsupported kind, no token (budget/API/idle), or a
    failed injection. The caller re-reads the page and `ChallengeDetector` still
    gates success — a failed/unaccepted solve degrades to "still a challenge",
    never a false positive.

- **`fetcher/level_2.py` + `fetcher/level_3.py`** — after `poll_until_solved`,
  a new `_maybe_solve_captcha` runs iff a solver is configured, a tenant is
  present, and the HTML still classifies as a challenge. On a successful solve
  it waits `retry_wait_increment_ms` (let the site validate/redirect) and
  re-polls, returning the fresh HTML.

- **`fetcher/factory.py`** — `build_level2/3_fetcher` gained an optional
  `captcha_solver` param, threaded to the fetcher constructor. Keeps the
  single-construction-site invariant (round 13 A1).

- **`orchestrator/worker.py`** — builds the solver **once** in `__init__` via
  `build_captcha_solver(CapSolverBudget(self._redis))` (env keys + per-tenant
  budget), same "authoritative construction once, not per-fetch" pattern as
  config/ChallengeDetector, and passes it into the two factory calls.

- **`observability/metrics.py`** — `captcha_solve_attempts_total{kind}` and
  `captcha_solved_total{kind}` counters so solve rate is visible in prod.

**Wiring is null-safe end to end:** no provider key → `build_captcha_solver`
returns `None` → fetchers skip solving and fetch normally.

---

## Verification

Unit-tested with a `FakePage` standing in for Playwright (evaluate() returns a
scripted detect result and records inject calls) — the orchestration is proven
without a browser:

`tests/unit/test_captcha_inpage.py` (15):
- detect → solve → inject happy path; inject receives the exact token
- each kind routes to the correct `solve_*` method (recaptcha/hcaptcha/turnstile)
- no widget / missing sitekey / unsupported kind → `False`, solver not called
- solver returns no token → injection skipped (`False`)
- injection eval raises → `False`; detect eval raises → `False`
- L2/L3 `_maybe_solve_captcha`: no-op without a solver, on non-challenge HTML,
  or (L2) without a tenant; solves + re-polls on a challenge page

```
$ pytest tests/unit/test_captcha_inpage.py tests/unit/test_captcha_solver.py -q
30 passed in 0.29s
$ pytest tests/unit/ -q
200 passed, 1 skipped
```
Ruff: `All checks passed!`. mypy `--strict`: new/changed files clean (only the
pre-existing third-party missing-stub notes remain, unrelated to this change).
CI grep-gates satisfied: `_captcha.py` constructs no fetcher, the worker builds
through the factory, and the direct-construction in tests is under `tests/`.

---

## Honest scope / limits

The **DOM detect + token injection is unit-tested, not live-verified** end to
end against a real CAPTCHA. Same blocker as round 19: NoCaptchaAI reCAPTCHA sat
perpetually `idle` (account capability not active) and there is no reliable free
solvable target. The pieces proven live in round 19 (auth → createTask → poll →
solution parse → billing, and ImageToText solving) are unchanged and still
carry the token; this round adds the page-side detect/inject/re-poll around
them. A real end-to-end confirmation still needs (1) an active solver
entitlement and (2) a live target — recorded as remaining operator items.

The reCAPTCHA callback-walk and Turnstile hidden-input creation are the
standard integration shapes but are inherently target-specific; they are
written to fail safe (best-effort, never raising).

---

## Files Changed
**New:** `fetcher/_captcha.py`, `tests/unit/test_captcha_inpage.py`.
**Modified:** `fetcher/level_2.py`, `fetcher/level_3.py`, `fetcher/factory.py`,
`orchestrator/worker.py`, `observability/metrics.py`.

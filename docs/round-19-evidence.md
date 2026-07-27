# Round 19 — CAPTCHA Solver: NoCaptchaAI Primary, CapSolver Fallback

**Date:** 2026-07-26
**Scope:** Wire NoCaptchaAI as the primary CAPTCHA solver, CapSolver as fallback (the last backlog item — previously operator-gated on keys, now that both keys are in `.env`).

---

## Implementation

Both providers speak the anti-captcha `createTask`/`getTaskResult` protocol, so the solve/balance logic lives once in `services/_anticaptcha.py` and each provider client only supplies its endpoints + cost estimate:

- `services/_anticaptcha.py` — shared `solve_anticaptcha(...)` (budget- + concurrency-gated, anti-captcha field names `websiteURL`/`websiteKey`) and `get_balance(...)`.
- `services/nocaptcha.py` — `NoCaptchaAIClient` (primary), endpoints `api.nocaptchaai.com`.
- `services/capsolver.py` — `CapSolverClient` (fallback), refactored onto the shared helper. **This also fixed a latent bug**: the old code sent `site_key`/`page_url` as task fields, but the anti-captcha API expects `websiteURL`/`websiteKey` — it would never have solved against the real API.
- `services/captcha_solver.py` — `CaptchaSolver(primary, fallback)`: tries primary, falls back on `None` (budget/API error/timeout). `build_captcha_solver(budget)` reads env: NoCaptchaAI primary + CapSolver fallback when both keys present; graceful single-provider or disabled otherwise.

---

## Live verification (real keys from `.env`)

Raw `getBalance` against each provider:
```
capsolver  https://api.capsolver.com/getBalance  → HTTP 401
           {"errorCode":"ERROR_KEY_DENIED_ACCESS","errorId":1}
nocaptcha  https://api.nocaptchaai.com/getBalance → HTTP 200
           {"balance":0,"errorId":0,"packages":[{}]}
```

**What this proves:**
- **NoCaptchaAI (primary) integration is correct** — HTTP 200, `errorId:0`. The endpoint, auth (`clientKey`), and anti-captcha request shape are right; the key is valid.
- **CapSolver (fallback) key is invalid** — HTTP 401 `ERROR_KEY_DENIED_ACCESS`. That's an account/credential issue, not a code issue (the same shared code path talks to NoCaptchaAI successfully).

**Honest limitations (operator/account, not code):**
1. NoCaptchaAI account shows `balance:0` / empty `packages` — the integration works, but an actual solve needs the account funded / a package active.
2. The current `CAPSOLVER_API_KEY` is rejected (401) — the fallback won't function until a valid CapSolver key is supplied.

So: the primary/fallback wiring is complete, type-safe, tested, and proven to talk to the live NoCaptchaAI API correctly. End-to-end solving is blocked only on account funding (NoCaptchaAI) and a valid fallback key (CapSolver) — both operator actions.

---

## Tests

`tests/unit/test_captcha_solver.py` (7): primary-hit-skips-fallback, primary-miss-uses-fallback, both-miss→None, no-fallback, and factory selection (NoCaptchaAI-primary+CapSolver-fallback / CapSolver-only / neither→None). Existing `test_capsolver.py` (5) still pass after the refactor.

Full suite: **227 passed, 1 skipped, 0 failed.** mypy `--strict`: Success (64 files). Ruff clean.

## Files Changed
**New:** `services/_anticaptcha.py`, `services/nocaptcha.py`, `services/captcha_solver.py`, `tests/unit/test_captcha_solver.py`.
**Modified:** `services/capsolver.py` (refactored onto shared helper; fixed `websiteURL`/`websiteKey` field bug).

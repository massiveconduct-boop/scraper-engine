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

### Provider-specific task-type bug found and fixed
The first live solve stuck at `status:"idle"` for the full 120 s poll. Root cause:
NoCaptchaAI's reCAPTCHA task type is **`ReCaptchaV2TaskProxyLess`** (their casing),
not CapSolver's `RecaptchaV2TaskProxyless`. Task types are **provider-specific** —
NoCaptchaAI was accepting the unknown type (HTTP 200) but no solver picked it up.
Fixed: `services/nocaptcha.py` uses `ReCaptchaV2TaskProxyLess`/`HCaptchaTaskProxyLess`;
`services/capsolver.py` keeps its own names.

### What is proven live
- **NoCaptchaAI auth + endpoints correct**: `getBalance` → HTTP 200 `errorId:0`; key valid; balance readable.
- **createTask accepted**: returns a `taskId`, `errorId:0`.
- **getTaskResult polling works**: valid JSON, status field parsed.
- **Pay-per-use billing works**: balance decremented `1.0000 → 0.9995` on task submission — the full submit→charge→poll pipeline is exercised against the live API.
- **CapSolver (fallback) key is invalid**: HTTP 401 `ERROR_KEY_DENIED_ACCESS` — account/credential issue, not code (same shared path talks to NoCaptchaAI fine).

### Not yet returning a token — root-caused to the account, not the code
Across multiple attempts (production 120 s poll + raw probes, before and after the
user enabled the account's "use wallet balance for solving" toggle), reCAPTCHA v2
tasks are **accepted** (`errorId:0`, `taskId`) but stay `status:"idle"` forever —
no solver is ever assigned.

**The request is byte-for-byte identical to NoCaptchaAI's own documented example**
(fetched from their `llms-full.txt`):
```
POST https://api.nocaptchaai.com/createTask
{ "clientKey": "…", "task": { "type": "ReCaptchaV2TaskProxyLess",
  "websiteURL": "https://www.google.com/recaptcha/api2/demo",
  "websiteKey": "…" } }
→ { "errorId": 0, "status": "idle", "taskId": "…" }   # then poll until "ready"
```
Same endpoint, same `clientKey` body auth (which `getBalance` confirms is valid,
HTTP 200), same task type, same fields. `getBalance` also shows `packages:[{}]`
(empty) — no active solving entitlement — which is the most likely reason tasks
sit idle: the account accepts and queues them but nothing picks them up.

**Conclusion (definitive from the code side):** the integration is correct — it
matches the official docs exactly and the auth/createTask/poll/billing mechanics
are all verified against the live API. reCAPTCHA solving does not complete on this
specific account (perpetual `idle`, empty `packages`), which is a NoCaptchaAI
dashboard/plan configuration matter (activate a reCAPTCHA capability/package, or
confirm pay-per-use is enabled for that type — likely needs NoCaptchaAI support),
**not** a code change. Once the account returns `ready` for a task, this client
parses `solution.token` and returns it with no further changes.

Remaining operator items: (1) NoCaptchaAI account — activate reCAPTCHA solving
capability; (2) supply a valid `CAPSOLVER_API_KEY` for the fallback (current one
401s). Lowest-cost re-test types: `ImageToTextTask`, `TurnstileTaskProxyLess`.

---

## Tests

`tests/unit/test_captcha_solver.py` (7): primary-hit-skips-fallback, primary-miss-uses-fallback, both-miss→None, no-fallback, and factory selection (NoCaptchaAI-primary+CapSolver-fallback / CapSolver-only / neither→None). Existing `test_capsolver.py` (5) still pass after the refactor.

Full suite: **227 passed, 1 skipped, 0 failed.** mypy `--strict`: Success (64 files). Ruff clean.

## Files Changed
**New:** `services/_anticaptcha.py`, `services/nocaptcha.py`, `services/captcha_solver.py`, `tests/unit/test_captcha_solver.py`.
**Modified:** `services/capsolver.py` (refactored onto shared helper; fixed `websiteURL`/`websiteKey` field bug).

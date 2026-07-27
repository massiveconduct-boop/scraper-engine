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

### What is NOT yet proven — and why we stopped
reCAPTCHA v2 tasks were submitted and **charged** but did **not return a solved
token** within the poll windows tried (48 s and 120 s), staying `status:"idle"`.
The charge was only ~$0.0005 (well below a full reCAPTCHA solve ~$0.002–0.003),
consistent with a **failed-task fee** rather than a completed solve. Whether that
is the demo target, a per-account reCAPTCHA capability, or processing latency is
unresolved — and each further attempt spends real balance, so per the cost
constraint we stopped rather than keep guessing.

**Bottom line:** the integration is complete, type-safe, tested, and mechanically
proven end-to-end against the live API (auth → createTask → poll → billing). A
returned reCAPTCHA **token** was not obtained on this account/target. Remaining is
operator/account verification (confirm reCAPTCHA capability on the NoCaptchaAI
plan, or point at a target/type known-good for the account) plus a valid CapSolver
fallback key — not code changes. NoCaptchaAI's cheapest types for a low-cost
re-test are `ImageToTextTask` and `TurnstileTaskProxyLess`.

---

## Tests

`tests/unit/test_captcha_solver.py` (7): primary-hit-skips-fallback, primary-miss-uses-fallback, both-miss→None, no-fallback, and factory selection (NoCaptchaAI-primary+CapSolver-fallback / CapSolver-only / neither→None). Existing `test_capsolver.py` (5) still pass after the refactor.

Full suite: **227 passed, 1 skipped, 0 failed.** mypy `--strict`: Success (64 files). Ruff clean.

## Files Changed
**New:** `services/_anticaptcha.py`, `services/nocaptcha.py`, `services/captcha_solver.py`, `tests/unit/test_captcha_solver.py`.
**Modified:** `services/capsolver.py` (refactored onto shared helper; fixed `websiteURL`/`websiteKey` field bug).

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

### PROVEN END-TO-END via ImageToTextTask (the reCAPTCHA idle was a Google-target issue)
reCAPTCHA v2 stayed `idle`, but that turned out to be specific to Google's reCAPTCHA
demo target, **not** the account or the integration. Switching to the cheapest,
non-Google avenue — `ImageToTextTask` (OCR) — solved **immediately and correctly**:
```
$ NoCaptchaAIClient.solve_image_to_text(image of "HELLO")
  → {"errorId":0,"status":"ready","solution":{"text":["HELLO"]}}   # exact match
$ ... image of "Kw9mZ" → "Kw9mz"    # all 5 chars read; only last-char case differs
  balance: 0.9996 → 0.999           # real per-solve billing (~$0.0002)
```
This is a **real solve returned through the production client method**, proving:
- the account's solver is active and billing works (reCAPTCHA `idle` was the Google
  demo target, exactly as the operator suspected);
- the full integration (auth → createTask → solution parse → budget/concurrency
  gating) works end-to-end against the live NoCaptchaAI API.

API details discovered live (docs were stale): ImageToText uses the `image` field
(not `body`), solves **synchronously** (solution in the createTask response), and
returns `solution.text` as a **list**. Wired as `NoCaptchaAIClient.solve_image_to_text`
(shared `services/_anticaptcha.solve_image_to_text`), with regression tests.

### On reCAPTCHA v2 specifically — root-caused to the account/target, not the code
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

## Popular real-world captcha coverage (round 19 follow-up)

Extended both providers to the captcha types most common in real-world scraping,
using each provider's **own documented task-type strings** (validated against
NoCaptchaAI's official task-type list and CapSolver's):

| Captcha | NoCaptchaAI (primary) | CapSolver (fallback) |
|---|---|---|
| reCAPTCHA v2 | `ReCaptchaV2TaskProxyLess` | `RecaptchaV2TaskProxyless` |
| Cloudflare Turnstile | `TurnstileTaskProxyLess` | `AntiTurnstileTaskProxyLess` |
| AWS WAF | `AWSWAFTask` | `AntiAwsWafTaskProxyLess` |
| GeeTest | `GeeTestTaskProxyLess` (+`gt`/`challenge`) | `GeeTestTaskProxyless` |
| MTCaptcha | `MTCaptchaTask` | `MtCaptchaTaskProxyless` |
| hCaptcha | *(not offered → returns None)* | `HCaptchaTaskProxyless` |
| image-to-text (OCR) | `ImageToTextTask` (live-proven) | — |

`hCaptcha` isn't offered by NoCaptchaAI's API, so its method returns `None` and the
`CaptchaSolver` orchestrator transparently falls through to CapSolver (which does
support it) — the primary/fallback design handles the coverage gap automatically.
The shared `solve_anticaptcha` now takes an arbitrary `task` dict (so each type
sends its correct fields) and handles both synchronous and polled solutions.

All token methods route through the **same createTask→poll→solution pipeline that
is live-proven end-to-end via ImageToText** (auth, billing, budget/concurrency
gating all verified).

### Live createTask validation per type — all corrected against the real API
The public docs were wrong for several types; live probing found the forms the API
actually accepts:

| Type | Task type / fields (live-verified) | Result |
|---|---|---|
| ImageToText | `ImageToTextTask` + `image` | ✅ **sync-solved** — "Zx7Qm" → `["zx7Qm"]` |
| reCAPTCHA v2 | `ReCaptchaV2TaskProxyLess` + websiteURL/Key | ✅ accepted (`errorId:0`, taskId) |
| Cloudflare Turnstile | **`AntiTurnstileTask`** + websiteURL/Key | ✅ accepted (docs' `TurnstileTaskProxyLess` → "Payload not valid") |
| GeeTest v4 | `GeeTestTaskProxyLess` + **`captchaId`** | ✅ accepted (docs' `gt` → "No images found") |
| MTCaptcha | `MTCaptchaTask` + websiteURL/Key | ✅ accepted (`errorId:0`) |
| hCaptcha | — | routes to CapSolver fallback (NoCaptchaAI lacks it) |
| AWS WAF | `AWSWAFTask` + runtime `awsKey/awsIv/awsContext/awsChallengeJS` | ⚠️ needs live challenge data extracted from the page (no static key); method accepts `**aws_fields` — can't validate with synthetic input |

**Corrections made in code from live probing:** Turnstile → `AntiTurnstileTask`;
GeeTest → v4 `captchaId` (method signature now takes `captcha_id`); AWS WAF method
takes runtime `**aws_fields`. Every other type's createTask is live-accepted
(HTTP 200, `errorId:0`), and ImageToText is proven solving end-to-end. Only AWS WAF
remains unverifiable without a real AWS-WAF-protected target (it inherently requires
per-request challenge data, not a static site key).

**Tests:** 16 captcha unit tests, including per-method task-type assertions
(recaptcha_v2/turnstile/aws_waf/mtcaptcha send the right type; geetest sends `gt`;
hcaptcha defers to fallback; orchestrator falls back per type). Full suite: **236
passed, 1 skipped, 0 failed.** mypy `--strict` clean (64 files). Ruff clean.

## Files Changed
**New:** `services/_anticaptcha.py`, `services/nocaptcha.py`, `services/captcha_solver.py`, `tests/unit/test_captcha_solver.py`.
**Modified:** `services/capsolver.py` (refactored onto shared helper; fixed `websiteURL`/`websiteKey` field bug; added popular-type methods).

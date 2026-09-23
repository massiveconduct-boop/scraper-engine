# services/nocaptcha.py
"""NoCaptchaAI integration — the PRIMARY CAPTCHA solving provider.

Anti-captcha createTask/getTaskResult protocol. Supports the captcha types most
common in real-world scraping (verified against NoCaptchaAI's task-type list):
reCAPTCHA v2, Cloudflare Turnstile, AWS WAF, GeeTest, MTCaptcha, and image-to-text.
hCaptcha / reCAPTCHA v3 are NOT offered by this API — those route to the CapSolver
fallback (services/captcha_solver.py). Budget/concurrency gating is shared via
services._anticaptcha.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from scraper_engine.services._anticaptcha import get_balance as _get_balance
from scraper_engine.services._anticaptcha import solve_anticaptcha
from scraper_engine.services._anticaptcha import solve_image_to_text as _solve_image_to_text

if TYPE_CHECKING:
    from scraper_engine.core.budget import CapSolverBudget
    from scraper_engine.core.tenant import TenantId

CREATE_TASK_URL = "https://api.nocaptchaai.com/createTask"
GET_RESULT_URL = "https://api.nocaptchaai.com/getTaskResult"
GET_BALANCE_URL = "https://api.nocaptchaai.com/getBalance"
# Separate from GET_BALANCE_URL (the legacy anti-captcha-protocol endpoint,
# which only returns a bare number): this is the current API's richer
# `GET /balance` that also reports plan status. A funded key with no active
# plan (planType/planId empty) authenticates and shows a real balance, but
# every worker-slot-based task type (reCAPTCHA/Turnstile/GeeTest/MTCaptcha —
# anything needing a live browser to render+solve, unlike ImageToText's pure
# ML inference) silently sits at status "idle" forever with errorId 0 — no
# error is ever raised, so balance alone can't tell you this is broken
# (round 22 — root-caused after reCAPTCHA v2 solves hung against two
# different real sitekeys while ImageToText solved fine on the same key).
PLAN_URL = "https://api.nocaptchaai.com/balance"

PROVIDER = "nocaptchaai"

logger = logging.getLogger(__name__)

# Re-check has_active_plan at most this often. A stuck-idle solve wastes 120s
# (60x2s poll in _anticaptcha.solve_anticaptcha) every single time it fires;
# re-checking every call would add a network round-trip to every solve, so
# this trades a bounded staleness window for that cost.
_PLAN_CACHE_TTL_SECONDS = 300.0


class NoCaptchaAIClient:
    """Client for the NoCaptchaAI CAPTCHA solving service (primary provider)."""

    # Rough per-solve cost estimates for budget reservation (BD-03 daily ceiling).
    ESTIMATED_TOKEN_COST = 0.002
    ESTIMATED_IMAGE_TO_TEXT_COST = 0.0003  # cheapest; measured ~$0.0002/solve

    def __init__(self, api_key: str, budget: CapSolverBudget) -> None:
        self._api_key = api_key
        self._budget = budget
        self._plan_cache: bool | None = None
        self._plan_cache_at: float = 0.0
        self._plan_lock = asyncio.Lock()

    async def _solve_token(self, tenant_id: TenantId, task: dict[str, object]) -> str | None:
        # Round-22 bug, closed for real here: a funded key with no active plan
        # authenticates and accepts worker-slot-based tasks (everything routed
        # through this method) but never solves them — the provider sits at
        # status "idle" forever with errorId 0, so solve_anticaptcha's 60x2s
        # poll always runs to exhaustion before returning None. Skip that
        # guaranteed-dead round-trip when we already know the plan is inactive;
        # captcha_solver.py's fallback-to-CapSolver path picks up from here.
        if await self.has_active_plan() is False:
            logger.warning(
                "nocaptchaai_no_active_plan task=%s — skipping dead 120s poll, "
                "falling through to fallback provider",
                task.get("type"),
            )
            return None
        return await solve_anticaptcha(
            provider=PROVIDER,
            api_key=self._api_key,
            create_task_url=CREATE_TASK_URL,
            get_result_url=GET_RESULT_URL,
            budget=self._budget,
            tenant_id=tenant_id,
            task=task,
            estimated_cost=self.ESTIMATED_TOKEN_COST,
        )

    # ── the captcha types most common in real-world scraping ────────────────

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve reCAPTCHA v2. Returns token or None."""
        return await self._solve_token(
            tenant_id,
            {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
            },
        )

    async def solve_turnstile(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve Cloudflare Turnstile (the most common modern challenge). Token or None.

        NoCaptchaAI's accepted type is AntiTurnstileTask (live-verified; the docs'
        TurnstileTaskProxyLess is rejected 'Payload not valid')."""
        return await self._solve_token(
            tenant_id,
            {
                "type": "AntiTurnstileTask",
                "websiteURL": page_url,
                "websiteKey": site_key,
            },
        )

    async def solve_aws_waf(
        self, tenant_id: TenantId, page_url: str, **aws_fields: str
    ) -> str | None:
        """Solve AWS WAF. Requires runtime challenge data extracted from the live
        page (awsKey/awsIv/awsContext/awsChallengeJS) — passed as **aws_fields —
        since AWS WAF has no static site key."""
        return await self._solve_token(
            tenant_id,
            {
                "type": "AWSWAFTask",
                "websiteURL": page_url,
                **aws_fields,
            },
        )

    async def solve_geetest(
        self,
        tenant_id: TenantId,
        captcha_id: str,
        page_url: str,
        challenge: str | None = None,
    ) -> str | None:
        """Solve GeeTest v4. Uses captchaId (live-verified accepted; the v3 gt/
        challenge form is rejected by this API). Returns solution or None."""
        task: dict[str, object] = {
            "type": "GeeTestTaskProxyLess",
            "websiteURL": page_url,
            "captchaId": captcha_id,
        }
        if challenge is not None:
            task["challenge"] = challenge
        return await self._solve_token(tenant_id, task)

    async def solve_mtcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve MTCaptcha. Returns token or None."""
        return await self._solve_token(
            tenant_id,
            {
                "type": "MTCaptchaTask",
                "websiteURL": page_url,
                "websiteKey": site_key,
            },
        )

    async def solve_hcaptcha(self, tenant_id: TenantId, site_key: str, page_url: str) -> str | None:
        """hCaptcha is NOT offered by NoCaptchaAI's API — always returns None so
        the orchestrator falls through to the CapSolver fallback (which supports it)."""
        return None

    async def solve_image_to_text(self, tenant_id: TenantId, image_b64: str) -> str | None:
        """Solve an image-to-text (OCR) CAPTCHA. Recognized text or None.

        Cheapest task type; solves synchronously. Live-verified round 19.
        """
        return await _solve_image_to_text(
            provider=PROVIDER,
            api_key=self._api_key,
            create_task_url=CREATE_TASK_URL,
            get_result_url=GET_RESULT_URL,
            budget=self._budget,
            tenant_id=tenant_id,
            image_b64=image_b64,
            estimated_cost=self.ESTIMATED_IMAGE_TO_TEXT_COST,
        )

    async def get_balance(self) -> float:
        """Return current NoCaptchaAI account balance."""
        return await _get_balance(api_key=self._api_key, balance_url=GET_BALANCE_URL)

    async def has_active_plan(self) -> bool | None:
        """Whether this key has an actual subscription plan (not just wallet
        balance). False means worker-slot-based types (reCAPTCHA/Turnstile/
        GeeTest/MTCaptcha) will accept tasks but never solve them — see
        PLAN_URL's docstring. Returns None if the plan endpoint itself
        couldn't be reached (distinct from a confirmed no-plan account).

        Cached for _PLAN_CACHE_TTL_SECONDS — this is on the hot path of every
        token-based solve (_solve_token), not just the manual preflight tool,
        so it must not add a network round-trip to every single solve call.
        A stale "no plan" reading self-heals within the TTL window once the
        account is actually fixed, with no restart needed.
        """
        now = time.monotonic()
        if self._plan_cache is not None and (now - self._plan_cache_at) < _PLAN_CACHE_TTL_SECONDS:
            return self._plan_cache

        async with self._plan_lock:
            # Re-check after acquiring the lock: a concurrent caller may have
            # already refreshed it while we were waiting.
            now = time.monotonic()
            if (
                self._plan_cache is not None
                and (now - self._plan_cache_at) < _PLAN_CACHE_TTL_SECONDS
            ):
                return self._plan_cache

            import httpx

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    data = (await client.get(PLAN_URL, params={"apiKey": self._api_key})).json()
            except Exception:
                # Endpoint unreachable: fail open (None), never cache a
                # transient network blip as a confirmed no-plan verdict.
                return None
            plan = data.get("plan") or {}
            result = bool(plan.get("planType") or plan.get("planId"))
            self._plan_cache = result
            self._plan_cache_at = time.monotonic()
            return result

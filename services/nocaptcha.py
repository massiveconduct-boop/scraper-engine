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

from typing import TYPE_CHECKING

from services._anticaptcha import get_balance as _get_balance
from services._anticaptcha import solve_anticaptcha
from services._anticaptcha import solve_image_to_text as _solve_image_to_text

if TYPE_CHECKING:
    from core.budget import CapSolverBudget
    from core.tenant import TenantId

CREATE_TASK_URL = "https://api.nocaptchaai.com/createTask"
GET_RESULT_URL = "https://api.nocaptchaai.com/getTaskResult"
GET_BALANCE_URL = "https://api.nocaptchaai.com/getBalance"

PROVIDER = "nocaptchaai"


class NoCaptchaAIClient:
    """Client for the NoCaptchaAI CAPTCHA solving service (primary provider)."""

    # Rough per-solve cost estimates for budget reservation (BD-03 daily ceiling).
    ESTIMATED_TOKEN_COST = 0.002
    ESTIMATED_IMAGE_TO_TEXT_COST = 0.0003  # cheapest; measured ~$0.0002/solve

    def __init__(self, api_key: str, budget: CapSolverBudget) -> None:
        self._api_key = api_key
        self._budget = budget

    async def _solve_token(
        self, tenant_id: TenantId, task: dict[str, object]
    ) -> str | None:
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
        return await self._solve_token(tenant_id, {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def solve_turnstile(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve Cloudflare Turnstile (the most common modern challenge). Token or None.

        NoCaptchaAI's accepted type is AntiTurnstileTask (live-verified; the docs'
        TurnstileTaskProxyLess is rejected 'Payload not valid')."""
        return await self._solve_token(tenant_id, {
            "type": "AntiTurnstileTask",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def solve_aws_waf(
        self, tenant_id: TenantId, page_url: str, **aws_fields: str
    ) -> str | None:
        """Solve AWS WAF. Requires runtime challenge data extracted from the live
        page (awsKey/awsIv/awsContext/awsChallengeJS) — passed as **aws_fields —
        since AWS WAF has no static site key."""
        return await self._solve_token(tenant_id, {
            "type": "AWSWAFTask",
            "websiteURL": page_url,
            **aws_fields,
        })

    async def solve_geetest(
        self, tenant_id: TenantId, captcha_id: str, page_url: str,
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
        return await self._solve_token(tenant_id, {
            "type": "MTCaptchaTask",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def solve_hcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """hCaptcha is NOT offered by NoCaptchaAI's API — always returns None so
        the orchestrator falls through to the CapSolver fallback (which supports it)."""
        return None

    async def solve_image_to_text(
        self, tenant_id: TenantId, image_b64: str
    ) -> str | None:
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

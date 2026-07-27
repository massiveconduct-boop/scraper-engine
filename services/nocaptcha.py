# services/nocaptcha.py
"""NoCaptchaAI integration — the PRIMARY CAPTCHA solving provider.

Same anti-captcha createTask/getTaskResult protocol as CapSolver; only the
endpoints and cost estimate differ. Budget/concurrency gating is shared via
services._anticaptcha. CapSolver is the fallback (services/captcha_solver.py).
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

    ESTIMATED_RECAPTCHA_COST = 0.002
    ESTIMATED_HCAPTCHA_COST = 0.002
    ESTIMATED_IMAGE_TO_TEXT_COST = 0.0003  # cheapest; measured ~$0.0002/solve

    def __init__(self, api_key: str, budget: CapSolverBudget) -> None:
        self._api_key = api_key
        self._budget = budget

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve reCAPTCHA v2. Returns token or None on budget/error."""
        return await solve_anticaptcha(
            provider=PROVIDER,
            api_key=self._api_key,
            create_task_url=CREATE_TASK_URL,
            get_result_url=GET_RESULT_URL,
            budget=self._budget,
            tenant_id=tenant_id,
            task_type="ReCaptchaV2TaskProxyLess",
            website_url=page_url,
            website_key=site_key,
            estimated_cost=self.ESTIMATED_RECAPTCHA_COST,
        )

    async def solve_hcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve hCaptcha. Returns token or None on budget/error."""
        return await solve_anticaptcha(
            provider=PROVIDER,
            api_key=self._api_key,
            create_task_url=CREATE_TASK_URL,
            get_result_url=GET_RESULT_URL,
            budget=self._budget,
            tenant_id=tenant_id,
            task_type="HCaptchaTaskProxyLess",
            website_url=page_url,
            website_key=site_key,
            estimated_cost=self.ESTIMATED_HCAPTCHA_COST,
        )

    async def solve_image_to_text(
        self, tenant_id: TenantId, image_b64: str
    ) -> str | None:
        """Solve an image-to-text (OCR) CAPTCHA. Returns recognized text or None.

        The cheapest task type; solves synchronously. Live-verified round 19.
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

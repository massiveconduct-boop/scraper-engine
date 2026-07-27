# services/capsolver.py
"""CapSolver integration — the FALLBACK CAPTCHA provider (primary is NoCaptchaAI;
see services/captcha_solver.py). CapSolver additionally covers hCaptcha, which
NoCaptchaAI's API does not.

All solve tasks are gated by:
  - core.budget.CapSolverBudget (per-tenant daily $1.00 ceiling, BD-03)
  - core.budget.CAPSOLVER_CONCURRENCY (bounds long-polls, closes F-13)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services._anticaptcha import get_balance as _get_balance
from services._anticaptcha import solve_anticaptcha

if TYPE_CHECKING:
    from core.budget import CapSolverBudget
    from core.tenant import TenantId

CREATE_TASK_URL = "https://api.capsolver.com/createTask"
GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"
GET_BALANCE_URL = "https://api.capsolver.com/getBalance"

PROVIDER = "capsolver"


class CapSolverClient:
    """Client for CapSolver CAPTCHA solving service (fallback provider)."""

    ESTIMATED_TOKEN_COST = 0.002

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

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._solve_token(tenant_id, {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def solve_hcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._solve_token(tenant_id, {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def solve_turnstile(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._solve_token(tenant_id, {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def solve_aws_waf(
        self, tenant_id: TenantId, page_url: str, **aws_fields: str
    ) -> str | None:
        return await self._solve_token(tenant_id, {
            "type": "AntiAwsWafTaskProxyLess",
            "websiteURL": page_url,
            **aws_fields,
        })

    async def solve_geetest(
        self, tenant_id: TenantId, captcha_id: str, page_url: str,
        challenge: str | None = None,
    ) -> str | None:
        task: dict[str, object] = {
            "type": "GeeTestTaskProxyless",
            "websiteURL": page_url,
            "captchaId": captcha_id,
        }
        if challenge is not None:
            task["challenge"] = challenge
        return await self._solve_token(tenant_id, task)

    async def solve_mtcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._solve_token(tenant_id, {
            "type": "MtCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        })

    async def get_balance(self) -> float:
        """Return current CapSolver account balance."""
        return await _get_balance(api_key=self._api_key, balance_url=GET_BALANCE_URL)

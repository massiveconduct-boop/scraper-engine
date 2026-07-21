# services/capsolver.py
"""CapSolver integration for CAPTCHA solving.

All solve tasks are gated by:
  - core.budget.CapSolverBudget (per-tenant daily $1.00 ceiling, BD-03)
  - core.budget.CAPSOLVER_CONCURRENCY (bounds long-polls, closes F-13)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from core.budget import CAPSOLVER_CONCURRENCY

if TYPE_CHECKING:
    from core.budget import CapSolverBudget
    from core.tenant import TenantId

logger = logging.getLogger(__name__)

CREATE_TASK_URL = "https://api.capsolver.com/createTask"
GET_RESULT_URL = "https://api.capsolver.com/getTaskResult"


class CapSolverClient:
    """Client for CapSolver CAPTCHA solving service."""

    ESTIMATED_RECAPTCHA_COST = 0.002  # ~$0.002 per reCAPTCHA v2 solve
    ESTIMATED_HCAPTCHA_COST = 0.002

    def __init__(self, api_key: str, budget: CapSolverBudget) -> None:
        self._api_key = api_key
        self._budget = budget

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve reCAPTCHA v2. Returns token or None if budget/concurrency exhausted."""
        return await self._solve(
            tenant_id,
            task_type="RecaptchaV2TaskProxyless",
            site_key=site_key,
            page_url=page_url,
            estimated_cost=self.ESTIMATED_RECAPTCHA_COST,
        )

    async def solve_hcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Solve hCaptcha. Returns token or None if budget/concurrency exhausted."""
        return await self._solve(
            tenant_id,
            task_type="HCaptchaTaskProxyless",
            site_key=site_key,
            page_url=page_url,
            estimated_cost=self.ESTIMATED_HCAPTCHA_COST,
        )

    async def get_balance(self) -> float:
        """Return current CapSolver account balance."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    "https://api.capsolver.com/getBalance",
                    json={"clientKey": self._api_key},
                )
                data = response.json()
                return float(data.get("balance", 0.0))
        except Exception:
            return 0.0

    async def _solve(
        self,
        tenant_id: TenantId,
        task_type: str,
        estimated_cost: float,
        **task_params: str,
    ) -> str | None:
        """Internal: solve a CAPTCHA with budget + concurrency gating."""
        if not await self._budget.check_and_reserve(tenant_id, estimated_cost):
            logger.warning("capsolver_budget_exceeded: %s", str(tenant_id))
            return None

        async with CAPSOLVER_CONCURRENCY:
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    create_resp = await client.post(
                        CREATE_TASK_URL,
                        json={
                            "clientKey": self._api_key,
                            "task": {"type": task_type, **task_params},
                        },
                    )
                    create_data = create_resp.json()
                    task_id = create_data.get("taskId")
                    if not task_id:
                        return None

                    for _ in range(60):
                        await asyncio.sleep(2)
                        result_resp = await client.post(
                            GET_RESULT_URL,
                            json={"clientKey": self._api_key, "taskId": task_id},
                        )
                        result_data = result_resp.json()

                        if result_data.get("status") == "ready":
                            token = result_data.get("solution", {}).get("gRecaptchaResponse")
                            return str(token) if token else None

                        if result_data.get("errorId") != 0:
                            return None

                    return None
            except Exception:
                return None

# services/_anticaptcha.py
"""Shared anti-captcha-protocol solve/balance helpers.

CapSolver and NoCaptchaAI both speak the anti-captcha createTask/getTaskResult
protocol (clientKey + task, poll for a "ready" solution). This module holds the
one implementation both provider clients call, so the only per-provider
differences are the endpoints, the cost estimate, and the account-key.
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


async def solve_anticaptcha(
    *,
    provider: str,
    api_key: str,
    create_task_url: str,
    get_result_url: str,
    budget: CapSolverBudget,
    tenant_id: TenantId,
    task_type: str,
    website_url: str,
    website_key: str,
    estimated_cost: float,
) -> str | None:
    """Solve a token CAPTCHA via the anti-captcha protocol.

    Budget- and concurrency-gated. Returns the solved token, or None on budget
    exhaustion, API error, or timeout (caller may then try a fallback provider).
    Uses the anti-captcha field names websiteURL / websiteKey (both CapSolver and
    NoCaptchaAI expect these).
    """
    if not await budget.check_and_reserve(tenant_id, estimated_cost):
        logger.warning("%s_budget_exceeded: %s", provider, str(tenant_id))
        return None

    async with CAPSOLVER_CONCURRENCY:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                create_resp = await client.post(
                    create_task_url,
                    json={
                        "clientKey": api_key,
                        "task": {
                            "type": task_type,
                            "websiteURL": website_url,
                            "websiteKey": website_key,
                        },
                    },
                )
                task_id = create_resp.json().get("taskId")
                if not task_id:
                    logger.warning("%s_create_task_failed", provider)
                    return None

                for _ in range(60):
                    await asyncio.sleep(2)
                    result_data = (
                        await client.post(
                            get_result_url,
                            json={"clientKey": api_key, "taskId": task_id},
                        )
                    ).json()

                    if result_data.get("status") == "ready":
                        solution = result_data.get("solution", {})
                        # providers vary: gRecaptchaResponse (recaptcha) or token
                        token = solution.get("gRecaptchaResponse") or solution.get("token")
                        return str(token) if token else None

                    if result_data.get("errorId") not in (0, None):
                        logger.warning(
                            "%s_solve_error: %s", provider, result_data.get("errorCode")
                        )
                        return None

                return None
        except Exception:
            logger.warning("%s_solve_exception", provider, exc_info=True)
            return None


async def get_balance(*, api_key: str, balance_url: str) -> float:
    """Return the provider account balance, or 0.0 on error."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            data = (await client.post(balance_url, json={"clientKey": api_key})).json()
            return float(data.get("balance", 0.0))
    except Exception:
        return 0.0

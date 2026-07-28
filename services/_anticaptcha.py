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

import core.budget

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
    task: dict[str, object],
    estimated_cost: float,
) -> str | None:
    """Solve a token CAPTCHA via the anti-captcha protocol.

    `task` is the full provider-specific task object (e.g.
    {"type": "TurnstileTaskProxyLess", "websiteURL": ..., "websiteKey": ...}).
    Budget- and concurrency-gated. Returns the solved token, or None on budget
    exhaustion, API error, or timeout (caller may then try a fallback provider).
    """
    if not await budget.check_and_reserve(tenant_id, estimated_cost):
        logger.warning("%s_budget_exceeded: %s", provider, str(tenant_id))
        return None

    def _token(solution: dict[str, object]) -> str | None:
        # providers/captcha-types vary in the solution field name
        val = (
            solution.get("gRecaptchaResponse")
            or solution.get("token")
            or solution.get("captcha_token")
            or solution.get("cookie")
        )
        return str(val) if val else None

    async with core.budget.CAPSOLVER_CONCURRENCY:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                create = (
                    await client.post(
                        create_task_url,
                        json={"clientKey": api_key, "task": task},
                    )
                ).json()

                if create.get("errorId") not in (0, None):
                    logger.warning("%s_create_task_error: %s", provider, create.get("errorCode"))
                    return None
                # some tasks solve synchronously (solution in the createTask response)
                if create.get("status") == "ready":
                    return _token(create.get("solution", {}))

                task_id = create.get("taskId")
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
                        return _token(result_data.get("solution", {}))

                    if result_data.get("errorId") not in (0, None):
                        logger.warning(
                            "%s_solve_error: %s", provider, result_data.get("errorCode")
                        )
                        return None

                return None
        except Exception:
            logger.warning("%s_solve_exception", provider, exc_info=True)
            return None


async def solve_image_to_text(
    *,
    provider: str,
    api_key: str,
    create_task_url: str,
    get_result_url: str,
    budget: CapSolverBudget,
    tenant_id: TenantId,
    image_b64: str,
    estimated_cost: float,
) -> str | None:
    """Solve an image-to-text (OCR) CAPTCHA — the cheapest task type.

    NoCaptchaAI's ImageToTextTask takes the base64 image in the `image` field
    (NOT the docs' stale `body`) and typically solves synchronously: createTask
    returns status "ready" with solution.text (a list) right away. Falls back to
    polling if a taskId is returned instead. Budget-gated; returns the recognized
    text or None. (Live-verified round 19: image of "HELLO" → solution.text
    ["HELLO"].)
    """
    if not await budget.check_and_reserve(tenant_id, estimated_cost):
        logger.warning("%s_budget_exceeded: %s", provider, str(tenant_id))
        return None

    def _extract(solution: dict[str, object]) -> str | None:
        text = solution.get("text")
        if isinstance(text, list):
            return str(text[0]) if text else None
        return str(text) if text else None

    async with core.budget.CAPSOLVER_CONCURRENCY:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = (
                    await client.post(
                        create_task_url,
                        json={
                            "clientKey": api_key,
                            "task": {"type": "ImageToTextTask", "image": image_b64},
                        },
                    )
                ).json()

                if resp.get("errorId") not in (0, None):
                    logger.warning("%s_ocr_error: %s", provider, resp.get("errorCode"))
                    return None

                # synchronous: solution present immediately
                if resp.get("status") == "ready":
                    return _extract(resp.get("solution", {}))

                # async fallback: poll by taskId
                task_id = resp.get("taskId")
                if not task_id:
                    return None
                for _ in range(30):
                    await asyncio.sleep(2)
                    j = (
                        await client.post(
                            get_result_url,
                            json={"clientKey": api_key, "taskId": task_id},
                        )
                    ).json()
                    if j.get("status") == "ready":
                        return _extract(j.get("solution", {}))
                    if j.get("errorId") not in (0, None):
                        return None
                return None
        except Exception:
            logger.warning("%s_ocr_exception", provider, exc_info=True)
            return None


async def get_balance(*, api_key: str, balance_url: str) -> float:
    """Return the provider account balance, or 0.0 on error."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            data = (await client.post(balance_url, json={"clientKey": api_key})).json()
            return float(data.get("balance", 0.0))
    except Exception:
        return 0.0

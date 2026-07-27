# services/captcha_solver.py
"""CAPTCHA solving orchestrator — NoCaptchaAI primary, CapSolver fallback.

Tries the primary provider; if it returns None (budget/API error/timeout),
transparently retries the same solve on the fallback provider. Both providers
share the anti-captcha protocol and the per-tenant budget.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.budget import CapSolverBudget
    from core.tenant import TenantId

logger = logging.getLogger(__name__)


class CaptchaProvider(Protocol):
    """Common interface both provider clients satisfy — the captcha types most
    common in real-world scraping."""

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None: ...

    async def solve_hcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None: ...

    async def solve_turnstile(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None: ...

    async def solve_aws_waf(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None: ...

    async def solve_mtcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None: ...

    async def solve_geetest(
        self, tenant_id: TenantId, gt: str, page_url: str, challenge: str | None = ...
    ) -> str | None: ...

    async def get_balance(self) -> float: ...


class CaptchaSolver:
    """Primary-with-fallback CAPTCHA solver covering the common real-world types."""

    def __init__(
        self, primary: CaptchaProvider, fallback: CaptchaProvider | None = None
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def _key_url(
        self, method: str, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Try primary.<method>(tenant, site_key, page_url), then fallback."""
        token: str | None = await getattr(self._primary, method)(
            tenant_id, site_key, page_url
        )
        if token is not None:
            return token
        if self._fallback is not None:
            logger.info("captcha_primary_miss %s — trying fallback", method)
            fb: str | None = await getattr(self._fallback, method)(
                tenant_id, site_key, page_url
            )
            return fb
        return None

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_recaptcha_v2", tenant_id, site_key, page_url)

    async def solve_hcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_hcaptcha", tenant_id, site_key, page_url)

    async def solve_turnstile(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_turnstile", tenant_id, site_key, page_url)

    async def solve_aws_waf(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_aws_waf", tenant_id, site_key, page_url)

    async def solve_mtcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_mtcaptcha", tenant_id, site_key, page_url)

    async def solve_geetest(
        self, tenant_id: TenantId, gt: str, page_url: str, challenge: str | None = None
    ) -> str | None:
        token = await self._primary.solve_geetest(tenant_id, gt, page_url, challenge)
        if token is not None:
            return token
        if self._fallback is not None:
            logger.info("captcha_primary_miss solve_geetest — trying fallback")
            return await self._fallback.solve_geetest(tenant_id, gt, page_url, challenge)
        return None


def build_captcha_solver(budget: CapSolverBudget) -> CaptchaSolver | None:
    """Construct the solver from env keys: NoCaptchaAI primary, CapSolver fallback.

    - Both keys set  → NoCaptchaAI primary, CapSolver fallback.
    - Only NoCaptchaAI → NoCaptchaAI, no fallback.
    - Only CapSolver   → CapSolver as the sole provider (graceful degrade).
    - Neither          → None (CAPTCHA solving disabled).
    """
    from services.capsolver import CapSolverClient
    from services.nocaptcha import NoCaptchaAIClient

    nocaptcha_key = os.environ.get("NOCAPTCHA_AI_API_KEY")
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY")

    if nocaptcha_key:
        primary: CaptchaProvider = NoCaptchaAIClient(nocaptcha_key, budget)
        fallback: CaptchaProvider | None = (
            CapSolverClient(capsolver_key, budget) if capsolver_key else None
        )
        logger.info(
            "captcha solver: primary=nocaptchaai fallback=%s",
            "capsolver" if fallback else "none",
        )
        return CaptchaSolver(primary, fallback)

    if capsolver_key:
        logger.info("captcha solver: primary=capsolver (NoCaptchaAI key absent)")
        return CaptchaSolver(CapSolverClient(capsolver_key, budget))

    logger.warning("captcha solver: no provider keys set — solving disabled")
    return None

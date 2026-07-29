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
    from scraper_engine.core.budget import CapSolverBudget
    from scraper_engine.core.tenant import TenantId

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
        self, tenant_id: TenantId, page_url: str, **aws_fields: str
    ) -> str | None: ...

    async def solve_mtcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None: ...

    async def solve_geetest(
        self,
        tenant_id: TenantId,
        captcha_id: str,
        page_url: str,
        challenge: str | None = ...,
    ) -> str | None: ...

    async def get_balance(self) -> float: ...


class CaptchaSolver:
    """Primary-with-fallback CAPTCHA solver covering the common real-world types."""

    def __init__(self, primary: CaptchaProvider, fallback: CaptchaProvider | None = None) -> None:
        self._primary = primary
        self._fallback = fallback

    async def _key_url(
        self, method: str, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        """Try primary.<method>(tenant, site_key, page_url), then fallback."""
        token: str | None = await getattr(self._primary, method)(tenant_id, site_key, page_url)
        if token is not None:
            return token
        if self._fallback is not None:
            logger.info("captcha_primary_miss %s — trying fallback", method)
            fb: str | None = await getattr(self._fallback, method)(tenant_id, site_key, page_url)
            return fb
        return None

    async def solve_recaptcha_v2(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_recaptcha_v2", tenant_id, site_key, page_url)

    async def solve_hcaptcha(self, tenant_id: TenantId, site_key: str, page_url: str) -> str | None:
        return await self._key_url("solve_hcaptcha", tenant_id, site_key, page_url)

    async def solve_turnstile(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_turnstile", tenant_id, site_key, page_url)

    async def solve_mtcaptcha(
        self, tenant_id: TenantId, site_key: str, page_url: str
    ) -> str | None:
        return await self._key_url("solve_mtcaptcha", tenant_id, site_key, page_url)

    async def solve_aws_waf(
        self, tenant_id: TenantId, page_url: str, **aws_fields: str
    ) -> str | None:
        token = await self._primary.solve_aws_waf(tenant_id, page_url, **aws_fields)
        if token is not None:
            return token
        if self._fallback is not None:
            logger.info("captcha_primary_miss solve_aws_waf — trying fallback")
            return await self._fallback.solve_aws_waf(tenant_id, page_url, **aws_fields)
        return None

    async def solve_geetest(
        self,
        tenant_id: TenantId,
        captcha_id: str,
        page_url: str,
        challenge: str | None = None,
    ) -> str | None:
        token = await self._primary.solve_geetest(tenant_id, captcha_id, page_url, challenge)
        if token is not None:
            return token
        if self._fallback is not None:
            logger.info("captcha_primary_miss solve_geetest — trying fallback")
            return await self._fallback.solve_geetest(tenant_id, captcha_id, page_url, challenge)
        return None


def build_captcha_solver(budget: CapSolverBudget) -> CaptchaSolver | None:
    """Construct the solver from env keys: NoCaptchaAI primary, CapSolver fallback.

    - Both keys set  → NoCaptchaAI primary, CapSolver fallback.
    - Only NoCaptchaAI → NoCaptchaAI, no fallback.
    - Only CapSolver   → CapSolver as the sole provider (graceful degrade).
    - Neither          → None (CAPTCHA solving disabled).
    """
    from scraper_engine.services.capsolver import CapSolverClient
    from scraper_engine.services.nocaptcha import NoCaptchaAIClient

    nocaptcha_key = os.environ.get("NOCAPTCHA_AI_API_KEY")
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY")

    # Surface configuration state in monitoring: a present key sets the gauge to
    # 1, an absent one to 0. This is "configured", NOT "verified" — a key can be
    # present but rejected by the provider (inactive capability / 401). The
    # tools/validate_captcha_keys.py preflight confirms the deeper "accepted" case.
    try:
        from scraper_engine.observability.metrics import captcha_provider_configured

        captcha_provider_configured.labels(provider="nocaptchaai").set(1 if nocaptcha_key else 0)
        captcha_provider_configured.labels(provider="capsolver").set(1 if capsolver_key else 0)
    except Exception:  # pragma: no cover - metrics must never break startup
        pass

    if nocaptcha_key:
        primary: CaptchaProvider = NoCaptchaAIClient(nocaptcha_key, budget)
        fallback: CaptchaProvider | None = (
            CapSolverClient(capsolver_key, budget) if capsolver_key else None
        )
        logger.info(
            "captcha solver: primary=nocaptchaai fallback=%s "
            "(keys present — run tools/validate_captcha_keys.py to confirm accepted)",
            "capsolver" if fallback else "none",
        )
        return CaptchaSolver(primary, fallback)

    if capsolver_key:
        logger.info(
            "captcha solver: primary=capsolver (NoCaptchaAI key absent) "
            "— run tools/validate_captcha_keys.py to confirm accepted"
        )
        return CaptchaSolver(CapSolverClient(capsolver_key, budget))

    logger.warning(
        "captcha solver: NO provider keys set — CAPTCHA solving is DISABLED. "
        "Set NOCAPTCHA_AI_API_KEY and/or CAPSOLVER_API_KEY to enable it."
    )
    return None


async def validate_captcha_keys() -> dict[str, dict[str, object]]:
    """Actively check each configured provider key by calling its balance endpoint.

    A present key that the provider *accepts* returns a balance; a present-but-
    rejected key (inactive capability / expired / 401) raises. This is the honest
    "does the key actually work" check that build_captcha_solver can't do without
    a network call. Used by tools/validate_captcha_keys.py.

    Returns ``{provider: {"configured": bool, "ok": bool, "detail": str}}``.
    Never raises — provider errors are captured per-provider.
    """
    from typing import Any, cast

    from scraper_engine.services.capsolver import CapSolverClient
    from scraper_engine.services.nocaptcha import NoCaptchaAIClient

    # get_balance() does not use the budget; pass a null placeholder.
    null_budget = cast("CapSolverBudget", None)
    providers: list[tuple[str, str, Any]] = [
        ("nocaptchaai", "NOCAPTCHA_AI_API_KEY", NoCaptchaAIClient),
        ("capsolver", "CAPSOLVER_API_KEY", CapSolverClient),
    ]
    out: dict[str, dict[str, object]] = {}
    for name, env_var, cls in providers:
        key = os.environ.get(env_var)
        if not key:
            out[name] = {
                "configured": False,
                "ok": False,
                "balance": None,
                "detail": "no key set",
            }
            continue
        try:
            # get_balance() proves the key AUTHENTICATES; it does not prove the
            # specific captcha capability is active. A funded, authenticating key
            # is the strongest signal we can get without spending on a real solve.
            client = cls(key, null_budget)
            balance = await client.get_balance()
            out[name] = {
                "configured": True,
                "ok": True,
                "balance": float(balance),
                "detail": f"balance={balance}",
            }
        except Exception as exc:
            out[name] = {
                "configured": True,
                "ok": False,
                "balance": None,
                "detail": str(exc)[:160],
            }
            continue

        # NoCaptchaAI-specific, isolated from the balance check above so a
        # failure here (e.g. the plan endpoint being unreachable) can never
        # clobber an already-successful balance result. A funded key with no
        # active plan (wallet-only / pay-as-you-go) authenticates and shows
        # real balance, but every worker-slot-based type (reCAPTCHA/
        # Turnstile/GeeTest/MTCaptcha) silently never solves — round-22 root
        # cause, see services/nocaptcha.py::has_active_plan docstring.
        if name == "nocaptchaai":
            has_plan = getattr(client, "has_active_plan", None)
            if has_plan is not None:
                try:
                    active = await has_plan()
                except Exception:
                    active = None
                if active is False:
                    out[name]["has_active_plan"] = False
                    out[name]["detail"] = (
                        f"balance={balance} — NO ACTIVE PLAN: ImageToText will "
                        f"work, but reCAPTCHA/Turnstile/GeeTest/MTCaptcha will "
                        f"accept tasks and never solve them (no worker slot)"
                    )
    return out

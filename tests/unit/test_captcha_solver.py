# tests/unit/test_captcha_solver.py
"""CaptchaSolver orchestrator — NoCaptchaAI primary, CapSolver fallback."""

from unittest.mock import AsyncMock

import pytest

from core.tenant import TenantId
from services.captcha_solver import CaptchaSolver, build_captcha_solver

TENANT = TenantId("captchatest")


def _provider(recaptcha=None, hcaptcha=None):
    p = AsyncMock()
    p.solve_recaptcha_v2.return_value = recaptcha
    p.solve_hcaptcha.return_value = hcaptcha
    return p


class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_primary_hit_no_fallback_call(self):
        primary = _provider(recaptcha="tok-primary")
        fallback = _provider(recaptcha="tok-fallback")
        solver = CaptchaSolver(primary, fallback)

        assert await solver.solve_recaptcha_v2(TENANT, "sk", "http://x") == "tok-primary"
        fallback.solve_recaptcha_v2.assert_not_called()  # primary succeeded

    @pytest.mark.asyncio
    async def test_primary_miss_uses_fallback(self):
        primary = _provider(recaptcha=None)          # primary fails
        fallback = _provider(recaptcha="tok-fallback")
        solver = CaptchaSolver(primary, fallback)

        assert await solver.solve_recaptcha_v2(TENANT, "sk", "http://x") == "tok-fallback"
        primary.solve_recaptcha_v2.assert_awaited_once()
        fallback.solve_recaptcha_v2.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_miss_returns_none(self):
        solver = CaptchaSolver(_provider(hcaptcha=None), _provider(hcaptcha=None))
        assert await solver.solve_hcaptcha(TENANT, "sk", "http://x") is None

    @pytest.mark.asyncio
    async def test_no_fallback_configured(self):
        solver = CaptchaSolver(_provider(hcaptcha=None))  # fallback=None
        assert await solver.solve_hcaptcha(TENANT, "sk", "http://x") is None


class TestFactory:
    def test_nocaptcha_primary_capsolver_fallback(self, monkeypatch):
        monkeypatch.setenv("NOCAPTCHA_AI_API_KEY", "nk")
        monkeypatch.setenv("CAPSOLVER_API_KEY", "ck")
        solver = build_captcha_solver(AsyncMock())
        assert solver is not None
        from services.capsolver import CapSolverClient
        from services.nocaptcha import NoCaptchaAIClient
        assert isinstance(solver._primary, NoCaptchaAIClient)
        assert isinstance(solver._fallback, CapSolverClient)

    def test_only_capsolver_becomes_sole_provider(self, monkeypatch):
        monkeypatch.delenv("NOCAPTCHA_AI_API_KEY", raising=False)
        monkeypatch.setenv("CAPSOLVER_API_KEY", "ck")
        solver = build_captcha_solver(AsyncMock())
        assert solver is not None
        from services.capsolver import CapSolverClient
        assert isinstance(solver._primary, CapSolverClient)
        assert solver._fallback is None

    def test_no_keys_returns_none(self, monkeypatch):
        monkeypatch.delenv("NOCAPTCHA_AI_API_KEY", raising=False)
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        assert build_captcha_solver(AsyncMock()) is None

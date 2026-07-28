# tests/unit/test_captcha_solver.py
"""CaptchaSolver orchestrator — NoCaptchaAI primary, CapSolver fallback."""

from unittest.mock import AsyncMock

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.services.captcha_solver import CaptchaSolver, build_captcha_solver

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
        from scraper_engine.services.capsolver import CapSolverClient
        from scraper_engine.services.nocaptcha import NoCaptchaAIClient
        assert isinstance(solver._primary, NoCaptchaAIClient)
        assert isinstance(solver._fallback, CapSolverClient)

    def test_only_capsolver_becomes_sole_provider(self, monkeypatch):
        monkeypatch.delenv("NOCAPTCHA_AI_API_KEY", raising=False)
        monkeypatch.setenv("CAPSOLVER_API_KEY", "ck")
        solver = build_captcha_solver(AsyncMock())
        assert solver is not None
        from scraper_engine.services.capsolver import CapSolverClient
        assert isinstance(solver._primary, CapSolverClient)
        assert solver._fallback is None

    def test_no_keys_returns_none(self, monkeypatch):
        monkeypatch.delenv("NOCAPTCHA_AI_API_KEY", raising=False)
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        assert build_captcha_solver(AsyncMock()) is None


class TestImageToText:
    @pytest.mark.asyncio
    async def test_ocr_extracts_list_text(self, monkeypatch):
        """NoCaptchaAI ImageToText solves synchronously; solution.text is a list
        — the client must return its first element (round 19 live-verified)."""
        import scraper_engine.services._anticaptcha as ac

        class _Resp:
            def json(self):
                return {"errorId": 0, "status": "ready", "solution": {"text": ["HELLO"]}}

        class _Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return _Resp()

        monkeypatch.setattr(ac.httpx, "AsyncClient", _Client)

        from scraper_engine.services.nocaptcha import NoCaptchaAIClient
        budget = AsyncMock()
        budget.check_and_reserve.return_value = True
        client = NoCaptchaAIClient("k", budget)
        assert await client.solve_image_to_text(TENANT, "b64img") == "HELLO"

    @pytest.mark.asyncio
    async def test_ocr_budget_gate_blocks(self):
        from scraper_engine.services.nocaptcha import NoCaptchaAIClient
        budget = AsyncMock()
        budget.check_and_reserve.return_value = False
        client = NoCaptchaAIClient("k", budget)
        assert await client.solve_image_to_text(TENANT, "b64img") is None


class TestProviderTaskTypes:
    """Each solve method must send the provider's correct task type + fields.
    Task-type strings validated against NoCaptchaAI's official task-type list;
    the solve pipeline itself is live-proven end-to-end via image-to-text."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,expected_type", [
        # live-verified NoCaptchaAI-accepted task types
        ("solve_recaptcha_v2", "ReCaptchaV2TaskProxyLess"),
        ("solve_turnstile", "AntiTurnstileTask"),
        ("solve_mtcaptcha", "MTCaptchaTask"),
    ])
    async def test_nocaptcha_sends_correct_type(self, monkeypatch, method, expected_type):
        import scraper_engine.services.nocaptcha as nc
        captured = {}

        async def fake_solve(**kw):
            captured.update(kw["task"])
            return "tok"

        monkeypatch.setattr(nc, "solve_anticaptcha", fake_solve)
        budget = AsyncMock()
        budget.check_and_reserve.return_value = True
        client = nc.NoCaptchaAIClient("k", budget)
        tok = await getattr(client, method)(TENANT, "sk", "http://x")
        assert tok == "tok"
        assert captured["type"] == expected_type
        assert captured["websiteURL"] == "http://x"

    @pytest.mark.asyncio
    async def test_nocaptcha_geetest_captcha_id_field(self, monkeypatch):
        import scraper_engine.services.nocaptcha as nc
        captured = {}

        async def fake_solve(**kw):
            captured.update(kw["task"])
            return "gtok"

        monkeypatch.setattr(nc, "solve_anticaptcha", fake_solve)
        budget = AsyncMock()
        budget.check_and_reserve.return_value = True
        client = nc.NoCaptchaAIClient("k", budget)
        assert await client.solve_geetest(TENANT, "CID123", "http://x") == "gtok"
        assert captured["type"] == "GeeTestTaskProxyLess"
        assert captured["captchaId"] == "CID123"

    @pytest.mark.asyncio
    async def test_nocaptcha_hcaptcha_defers_to_fallback(self):
        # NoCaptchaAI has no hCaptcha — returns None so orchestrator falls through
        from scraper_engine.services.nocaptcha import NoCaptchaAIClient
        budget = AsyncMock()
        client = NoCaptchaAIClient("k", budget)
        assert await client.solve_hcaptcha(TENANT, "sk", "http://x") is None

    @pytest.mark.asyncio
    async def test_orchestrator_turnstile_fallback(self):
        primary = _provider()
        primary.solve_turnstile = AsyncMock(return_value=None)
        fallback = _provider()
        fallback.solve_turnstile = AsyncMock(return_value="cf-tok")
        solver = CaptchaSolver(primary, fallback)
        assert await solver.solve_turnstile(TENANT, "sk", "http://x") == "cf-tok"

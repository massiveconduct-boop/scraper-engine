# tests/unit/test_captcha_provider_validation.py
"""Active CAPTCHA provider-key validation (services.captcha_solver.validate_captcha_keys).

The point is to catch the silent failure the worker can't see: a key that is
present but rejected by the provider. Fake provider clients stand in for the
network — get_balance() returning a number == accepted, raising == rejected."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.services import capsolver, nocaptcha
from scraper_engine.services.captcha_solver import validate_captcha_keys


@pytest.mark.asyncio
async def test_reports_working_and_rejected(monkeypatch):
    monkeypatch.setenv("NOCAPTCHA_AI_API_KEY", "nk-present")
    monkeypatch.setenv("CAPSOLVER_API_KEY", "ck-present")

    good = MagicMock(get_balance=AsyncMock(return_value=12.5))
    rejected = MagicMock(get_balance=AsyncMock(side_effect=RuntimeError("401 KEY_DENIED")))
    monkeypatch.setattr(nocaptcha, "NoCaptchaAIClient", MagicMock(return_value=good))
    monkeypatch.setattr(capsolver, "CapSolverClient", MagicMock(return_value=rejected))

    r = await validate_captcha_keys()

    assert r["nocaptchaai"]["ok"] is True
    assert r["nocaptchaai"]["balance"] == 12.5
    assert r["capsolver"]["configured"] is True
    assert r["capsolver"]["ok"] is False
    assert r["capsolver"]["balance"] is None
    assert "401" in r["capsolver"]["detail"]  # error surfaced, not swallowed


@pytest.mark.asyncio
async def test_zero_balance_authenticates_but_is_not_solve_capable(monkeypatch):
    """A key that authenticates with $0 balance reports ok=True, balance=0 — the
    tool downgrades that to NO FUNDS."""
    monkeypatch.setenv("NOCAPTCHA_AI_API_KEY", "nk")
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    broke = MagicMock(get_balance=AsyncMock(return_value=0.0))
    monkeypatch.setattr(nocaptcha, "NoCaptchaAIClient", MagicMock(return_value=broke))

    r = await validate_captcha_keys()
    assert r["nocaptchaai"]["ok"] is True
    assert r["nocaptchaai"]["balance"] == 0.0


@pytest.mark.asyncio
async def test_reports_no_active_plan_without_clobbering_balance(monkeypatch):
    """A NoCaptchaAI key that authenticates with real balance but has no
    subscription plan (round-22 root cause: worker-slot types accept tasks
    and silently never solve them) must surface has_active_plan=False
    without losing the already-successful ok/balance fields."""
    monkeypatch.setenv("NOCAPTCHA_AI_API_KEY", "nk-present")
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)

    no_plan = MagicMock(
        get_balance=AsyncMock(return_value=0.9982),
        has_active_plan=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(nocaptcha, "NoCaptchaAIClient", MagicMock(return_value=no_plan))

    r = await validate_captcha_keys()

    assert r["nocaptchaai"]["ok"] is True
    assert r["nocaptchaai"]["balance"] == 0.9982
    assert r["nocaptchaai"]["has_active_plan"] is False
    assert "NO ACTIVE PLAN" in r["nocaptchaai"]["detail"]


@pytest.mark.asyncio
async def test_absent_keys_report_not_configured(monkeypatch):
    monkeypatch.delenv("NOCAPTCHA_AI_API_KEY", raising=False)
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)

    r = await validate_captcha_keys()

    assert r["nocaptchaai"] == {
        "configured": False,
        "ok": False,
        "balance": None,
        "detail": "no key set",
    }
    assert r["capsolver"] == {
        "configured": False,
        "ok": False,
        "balance": None,
        "detail": "no key set",
    }

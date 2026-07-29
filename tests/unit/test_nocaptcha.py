# tests/unit/test_nocaptcha.py
"""NoCaptchaAIClient — task types not already covered by test_captcha_solver.py
(solve_aws_waf, solve_geetest's challenge branch, get_balance), plus
has_active_plan (round 22's no-active-plan detection). Was 76% covered."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.services import nocaptcha as nc

TENANT = TenantId("nocaptchatest")


def _budget():
    b = AsyncMock()
    b.check_and_reserve.return_value = True
    return b


class TestSolveAwsWaf:
    @pytest.mark.asyncio
    async def test_sends_aws_task_type_and_fields(self, monkeypatch):
        captured = {}

        async def fake_solve(**kw):
            captured.update(kw["task"])
            return "aws-tok"

        monkeypatch.setattr(nc, "solve_anticaptcha", fake_solve)
        client = nc.NoCaptchaAIClient("k", _budget())

        result = await client.solve_aws_waf(
            TENANT, "http://x", awsKey="key1", awsIv="iv1", awsContext="ctx1"
        )

        assert result == "aws-tok"
        assert captured["type"] == "AWSWAFTask"
        assert captured["websiteURL"] == "http://x"
        assert captured["awsKey"] == "key1"
        assert captured["awsIv"] == "iv1"
        assert captured["awsContext"] == "ctx1"


class TestSolveGeetestChallenge:
    @pytest.mark.asyncio
    async def test_challenge_field_included_when_provided(self, monkeypatch):
        captured = {}

        async def fake_solve(**kw):
            captured.update(kw["task"])
            return "gt-tok"

        monkeypatch.setattr(nc, "solve_anticaptcha", fake_solve)
        client = nc.NoCaptchaAIClient("k", _budget())

        result = await client.solve_geetest(TENANT, "cid", "http://x", challenge="ch123")

        assert result == "gt-tok"
        assert captured["challenge"] == "ch123"


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_returns_balance(self, monkeypatch):
        import scraper_engine.services._anticaptcha as ac

        class _Resp:
            def json(self):
                return {"balance": 12.5}

        class _Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return _Resp()

        monkeypatch.setattr(ac.httpx, "AsyncClient", _Client)
        client = nc.NoCaptchaAIClient("k", _budget())

        assert await client.get_balance() == 12.5


class TestHasActivePlan:
    @pytest.mark.asyncio
    async def test_true_when_plan_type_present(self, monkeypatch):
        resp = MagicMock()
        resp.json.return_value = {"plan": {"planType": "pro"}}
        http_client = AsyncMock()
        http_client.get.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = nc.NoCaptchaAIClient("k", _budget())
        assert await client.has_active_plan() is True

    @pytest.mark.asyncio
    async def test_false_when_no_plan(self, monkeypatch):
        resp = MagicMock()
        resp.json.return_value = {"plan": {}}
        http_client = AsyncMock()
        http_client.get.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = nc.NoCaptchaAIClient("k", _budget())
        assert await client.has_active_plan() is False

    @pytest.mark.asyncio
    async def test_none_when_plan_endpoint_unreachable(self, monkeypatch):
        http_client = AsyncMock()
        http_client.__aenter__.side_effect = RuntimeError("connection refused")

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=http_client))

        client = nc.NoCaptchaAIClient("k", _budget())
        assert await client.has_active_plan() is None

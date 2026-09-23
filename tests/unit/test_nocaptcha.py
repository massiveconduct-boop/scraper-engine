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
        monkeypatch.setattr(client, "has_active_plan", AsyncMock(return_value=True))

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
        monkeypatch.setattr(client, "has_active_plan", AsyncMock(return_value=True))

        result = await client.solve_geetest(TENANT, "cid", "http://x", challenge="ch123")

        assert result == "gt-tok"
        assert captured["challenge"] == "ch123"


class TestSolveTokenPlanGate:
    """_solve_token must skip the dead 120s poll when has_active_plan() is
    False — round-22 bug closed for real (previously has_active_plan was
    only wired into the manual validate_captcha_keys preflight tool)."""

    @pytest.mark.asyncio
    async def test_no_active_plan_skips_solve_anticaptcha(self, monkeypatch):
        solve_called = False

        async def fake_solve(**kw):
            nonlocal solve_called
            solve_called = True
            return "should-not-be-reached"

        monkeypatch.setattr(nc, "solve_anticaptcha", fake_solve)
        client = nc.NoCaptchaAIClient("k", _budget())
        monkeypatch.setattr(client, "has_active_plan", AsyncMock(return_value=False))

        result = await client.solve_recaptcha_v2(TENANT, "sk", "http://x")

        assert result is None
        assert solve_called is False

    @pytest.mark.asyncio
    async def test_plan_check_unreachable_fails_open(self, monkeypatch):
        """None (endpoint unreachable) must not be treated as a confirmed
        no-plan verdict — a transient blip shouldn't permanently disable
        solving."""
        solve_called = False

        async def fake_solve(**kw):
            nonlocal solve_called
            solve_called = True
            return "tok"

        monkeypatch.setattr(nc, "solve_anticaptcha", fake_solve)
        client = nc.NoCaptchaAIClient("k", _budget())
        monkeypatch.setattr(client, "has_active_plan", AsyncMock(return_value=None))

        result = await client.solve_recaptcha_v2(TENANT, "sk", "http://x")

        assert result == "tok"
        assert solve_called is True

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_network_call(self, monkeypatch):
        """Two has_active_plan() calls racing on a cold cache must not each
        make their own network call — the second must see the first's
        just-published result after acquiring the lock (double-checked
        locking's inner re-check)."""
        import asyncio

        resp = MagicMock()
        resp.json.return_value = {"plan": {"planType": "pro"}}
        http_client = AsyncMock()

        async def slow_get(*a, **k):
            await asyncio.sleep(0.05)
            return resp

        http_client.get = slow_get
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        get_mock = MagicMock(return_value=http_client)
        monkeypatch.setattr(httpx, "AsyncClient", get_mock)

        client = nc.NoCaptchaAIClient("k", _budget())

        results = await asyncio.gather(client.has_active_plan(), client.has_active_plan())

        assert results == [True, True]
        assert get_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_has_active_plan_result_is_cached_across_solve_calls(self, monkeypatch):
        """The plan endpoint must not be hit on every solve — that would add
        a network round-trip to every single captcha solve."""
        resp = MagicMock()
        resp.json.return_value = {"plan": {}}
        http_client = AsyncMock()
        http_client.get.return_value = resp
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False

        import httpx

        get_mock = MagicMock(return_value=http_client)
        monkeypatch.setattr(httpx, "AsyncClient", get_mock)

        client = nc.NoCaptchaAIClient("k", _budget())

        assert await client.has_active_plan() is False
        assert await client.has_active_plan() is False
        assert await client.has_active_plan() is False
        assert get_mock.call_count == 1


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_returns_balance(self, monkeypatch):
        import scraper_engine.services._anticaptcha as ac

        class _Resp:
            def json(self):
                return {"balance": 12.5}

        class _Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

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

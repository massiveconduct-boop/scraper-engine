# tests/unit/test_anticaptcha.py
"""services/_anticaptcha.py — shared anti-captcha-protocol helpers used by
both CapSolver and NoCaptchaAI. Was 50% covered: existing tests only ever hit
a real (invalid-key) endpoint, which short-circuits at the create-task-error
check — the synchronous-ready path, the polling loop (ready/error/exhaust),
and every exception handler were never exercised. httpx.AsyncClient and
asyncio.sleep are mocked here for deterministic, fast, network-free tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scraper_engine.core.tenant import TenantId
from scraper_engine.services import _anticaptcha
from scraper_engine.services._anticaptcha import (
    get_balance,
    solve_anticaptcha,
    solve_image_to_text,
)

_TENANT = TenantId("system")


def _budget(allow=True):
    b = AsyncMock()
    b.check_and_reserve.return_value = allow
    return b


def _fake_client(json_sequence):
    """httpx.AsyncClient replacement: .post() returns responses (in order)
    whose .json() yields each queued dict."""
    responses = [MagicMock(json=MagicMock(return_value=j)) for j in json_sequence]
    client = AsyncMock()
    client.post.side_effect = responses
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(_anticaptcha.asyncio, "sleep", AsyncMock())


class TestSolveAnticaptchaSyncReady:
    @pytest.mark.asyncio
    async def test_synchronous_ready_extracts_grecaptcha_response(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {
                            "errorId": 0,
                            "status": "ready",
                            "solution": {"gRecaptchaResponse": "tok"},
                        },
                    ]
                )
            ),
        )
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result == "tok"

    @pytest.mark.asyncio
    async def test_token_field_fallback_order(self, monkeypatch):
        for field in ("token", "captcha_token", "cookie"):
            monkeypatch.setattr(
                _anticaptcha.httpx,
                "AsyncClient",
                MagicMock(
                    return_value=_fake_client(
                        [
                            {"errorId": 0, "status": "ready", "solution": {field: "val-" + field}},
                        ]
                    )
                ),
            )
            result = await solve_anticaptcha(
                provider="test",
                api_key="k",
                create_task_url="http://c",
                get_result_url="http://g",
                budget=_budget(),
                tenant_id=_TENANT,
                task={},
                estimated_cost=0.001,
            )
            assert result == "val-" + field

    @pytest.mark.asyncio
    async def test_no_recognizable_token_field_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "ready", "solution": {}},
                    ]
                )
            ),
        )
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result is None


class TestSolveAnticaptchaNoTaskId:
    @pytest.mark.asyncio
    async def test_missing_task_id_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "processing"},
                    ]
                )
            ),
        )
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result is None


class TestSolveAnticaptchaPolling:
    @pytest.mark.asyncio
    async def test_polling_ready_extracts_token(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "processing", "taskId": "abc"},
                        {"status": "processing"},
                        {"status": "ready", "solution": {"token": "polled-tok"}},
                    ]
                )
            ),
        )
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result == "polled-tok"

    @pytest.mark.asyncio
    async def test_polling_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "processing", "taskId": "abc"},
                        {"errorId": 12, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"},
                    ]
                )
            ),
        )
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_polling_exhausts_and_gives_up(self, monkeypatch):
        responses = [{"errorId": 0, "status": "processing", "taskId": "abc"}]
        responses += [{"status": "processing"}] * 60
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(return_value=_fake_client(responses)),
        )
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result is None


class TestSolveAnticaptchaException:
    @pytest.mark.asyncio
    async def test_network_exception_logged_and_returns_none(self, monkeypatch):
        client = AsyncMock()
        client.__aenter__.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(_anticaptcha.httpx, "AsyncClient", MagicMock(return_value=client))
        result = await solve_anticaptcha(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            task={},
            estimated_cost=0.001,
        )
        assert result is None


class TestSolveImageToText:
    @pytest.mark.asyncio
    async def test_create_task_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST"},
                    ]
                )
            ),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_synchronous_ready_extracts_list_text(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "ready", "solution": {"text": ["HELLO"]}},
                    ]
                )
            ),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result == "HELLO"

    @pytest.mark.asyncio
    async def test_synchronous_ready_extracts_scalar_text(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "ready", "solution": {"text": "HELLO"}},
                    ]
                )
            ),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result == "HELLO"

    @pytest.mark.asyncio
    async def test_missing_task_id_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "processing"},
                    ]
                )
            ),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_polling_ready_extracts_text(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "processing", "taskId": "abc"},
                        {"status": "ready", "solution": {"text": ["WORLD"]}},
                    ]
                )
            ),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result == "WORLD"

    @pytest.mark.asyncio
    async def test_polling_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(
                return_value=_fake_client(
                    [
                        {"errorId": 0, "status": "processing", "taskId": "abc"},
                        {"errorId": 5},
                    ]
                )
            ),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_polling_exhausts_and_gives_up(self, monkeypatch):
        responses = [{"errorId": 0, "status": "processing", "taskId": "abc"}]
        responses += [{"status": "processing"}] * 30
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(return_value=_fake_client(responses)),
        )
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_network_exception_logged_and_returns_none(self, monkeypatch):
        client = AsyncMock()
        client.__aenter__.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(_anticaptcha.httpx, "AsyncClient", MagicMock(return_value=client))
        result = await solve_image_to_text(
            provider="test",
            api_key="k",
            create_task_url="http://c",
            get_result_url="http://g",
            budget=_budget(),
            tenant_id=_TENANT,
            image_b64="Zm9v",
            estimated_cost=0.001,
        )
        assert result is None


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_returns_balance_on_success(self, monkeypatch):
        monkeypatch.setattr(
            _anticaptcha.httpx,
            "AsyncClient",
            MagicMock(return_value=_fake_client([{"balance": 4.2}])),
        )
        result = await get_balance(api_key="k", balance_url="http://b")
        assert result == 4.2

    @pytest.mark.asyncio
    async def test_returns_zero_on_exception(self, monkeypatch):
        client = AsyncMock()
        client.__aenter__.side_effect = RuntimeError("connection refused")
        monkeypatch.setattr(_anticaptcha.httpx, "AsyncClient", MagicMock(return_value=client))
        result = await get_balance(api_key="k", balance_url="http://b")
        assert result == 0.0

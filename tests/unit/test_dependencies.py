# tests/unit/test_dependencies.py
"""api/dependencies.py — FastAPI DI getters for the module-level singletons
api/main.py's lifespan initializes at startup. Each getter must raise
RuntimeError before startup (None guard) and hand back the singleton once
set. Was 44% covered — only the happy path (module-level default) exercised,
none of the six getters' bodies actually called."""

from unittest.mock import MagicMock

import pytest

import scraper_engine.api.dependencies as deps

_GETTERS = [
    ("_tenant_resolver", "get_tenant_resolver"),
    ("_storage_pg", "get_postgres"),
    ("_storage_redis", "get_redis"),
    ("_storage_s3", "get_s3"),
    ("_queue", "get_queue"),
    ("_ssrf_guard", "get_ssrf_guard"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("attr, getter_name", _GETTERS)
async def test_getter_raises_runtime_error_when_not_initialized(monkeypatch, attr, getter_name):
    monkeypatch.setattr(deps, attr, None)
    getter = getattr(deps, getter_name)

    with pytest.raises(RuntimeError):
        await getter()


@pytest.mark.asyncio
@pytest.mark.parametrize("attr, getter_name", _GETTERS)
async def test_getter_returns_singleton_once_initialized(monkeypatch, attr, getter_name):
    sentinel = MagicMock(name=f"fake-{attr}")
    monkeypatch.setattr(deps, attr, sentinel)
    getter = getattr(deps, getter_name)

    result = await getter()

    assert result is sentinel

"""api/main.py lifespan — the paths the real-infra integration test cannot take.

Round 64 (branch burn-down). tests/integration/test_api_main.py drives the
lifespan from nothing against real Postgres/Redis/MinIO. These cover the
other two shapes: dependencies already wired before startup (tests, or a
process that injected them) must be reused, not replaced; and a dependency
that is gone by shutdown must be skipped, not stopped.
"""

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import scraper_engine.api.dependencies as deps
from scraper_engine.config.schema import AppConfig


def _config_without_tracing() -> AppConfig:
    cfg = AppConfig()
    cfg.observability.tracing_enabled = False
    return cfg


def _wire(monkeypatch):
    wired = {
        "_ssrf_guard": MagicMock(),
        "_storage_pg": MagicMock(stop=AsyncMock()),
        "_storage_redis": MagicMock(stop=AsyncMock()),
        "_storage_s3": MagicMock(stop=AsyncMock()),
        "_queue": MagicMock(),
    }
    for name, value in wired.items():
        monkeypatch.setattr(deps, name, value)
    monkeypatch.setattr("scraper_engine.config.loader.load_config", _config_without_tracing)
    return wired


def test_pre_wired_dependencies_are_reused_and_stopped(monkeypatch):
    wired = _wire(monkeypatch)
    from scraper_engine.api.main import create_app

    with TestClient(create_app()):
        for name, value in wired.items():
            assert getattr(deps, name) is value

    for name in ("_storage_pg", "_storage_redis", "_storage_s3"):
        wired[name].stop.assert_awaited_once()


def test_dependencies_gone_by_shutdown_are_skipped(monkeypatch):
    wired = _wire(monkeypatch)
    from scraper_engine.api.main import create_app

    with TestClient(create_app()):
        for name in ("_storage_pg", "_storage_redis", "_storage_s3"):
            monkeypatch.setattr(deps, name, None)

    for name in ("_storage_pg", "_storage_redis", "_storage_s3"):
        wired[name].stop.assert_not_awaited()

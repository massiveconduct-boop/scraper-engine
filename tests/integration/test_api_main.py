# tests/integration/test_api_main.py
"""api/main.py — create_app() + lifespan, against real Postgres/Redis/MinIO.

Was 0% covered: nothing ever called create_app() end-to-end, so the lifespan
startup/shutdown (deps wiring, tracing instrumentation) never executed.
"""

from fastapi.testclient import TestClient

import scraper_engine.api.dependencies as deps
from scraper_engine.config.schema import AppConfig


def _local_config() -> AppConfig:
    cfg = AppConfig()
    cfg.storage.database_url = "postgresql://scraper:scraper@localhost:5432/scraper_engine"
    cfg.storage.redis_url = "redis://localhost:6379/0"
    cfg.s3.endpoint_url = "http://localhost:9000"
    return cfg


class TestCreateApp:
    def test_lifespan_wires_dependencies_and_instruments_tracing(self, monkeypatch):
        monkeypatch.setattr(deps, "_ssrf_guard", None)
        monkeypatch.setattr(deps, "_storage_pg", None)
        monkeypatch.setattr(deps, "_storage_redis", None)
        monkeypatch.setattr(deps, "_storage_s3", None)
        monkeypatch.setattr(deps, "_queue", None)
        monkeypatch.setattr(deps, "_tenant_resolver", None)

        monkeypatch.setattr("scraper_engine.config.loader.load_config", lambda: _local_config())

        from scraper_engine.api.main import create_app

        app = create_app()

        with TestClient(app) as client:
            assert deps._ssrf_guard is not None
            assert deps._storage_pg is not None
            assert deps._storage_redis is not None
            assert deps._storage_s3 is not None
            assert deps._queue is not None
            assert deps._tenant_resolver is not None

            response = client.get("/v1/health")
            assert response.status_code == 200

        assert deps._storage_pg is not None

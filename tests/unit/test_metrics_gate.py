# tests/unit/test_metrics_gate.py
"""ObservabilityConfig.metrics_enabled was declared but never read anywhere —
/metrics was unconditionally mounted regardless of the flag. Confirms
register_routes() actually gates it now."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import register_routes
from config.schema import AppConfig


def test_metrics_enabled_mounts_route():
    app = FastAPI()
    register_routes(app, AppConfig())
    client = TestClient(app)
    assert client.get("/metrics").status_code != 404


def test_metrics_disabled_does_not_mount_route():
    cfg = AppConfig()
    cfg.observability.metrics_enabled = False
    app = FastAPI()
    register_routes(app, cfg)
    client = TestClient(app)
    assert client.get("/metrics").status_code == 404

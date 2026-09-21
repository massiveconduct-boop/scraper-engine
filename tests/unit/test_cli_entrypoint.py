# tests/unit/test_cli_entrypoint.py
"""CLI `api` subcommand tests (round 56).

cli/ is outside the coverage gate ([tool.coverage.run].source in pyproject.toml
doesn't list it), so these aren't coverage-blocking — but the dispatch logic
still needs correctness coverage: right HTTP method/path/params per subcommand,
missing-api-key and non-2xx-response both exit(1), matching `_check_health`'s
existing exit-code convention.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from scraper_engine.cli.entrypoint import (
    _api_client,
    _print_api_response,
    _run_api_command,
)


class FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._body = body
        self.text = str(body)

    def json(self) -> object:
        return self._body


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.response = FakeResponse(200, {"ok": True})
        self.closed = False

    def get(self, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", path, kwargs))
        return self.response

    def post(self, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", path, kwargs))
        return self.response

    def close(self) -> None:
        self.closed = True


def _args(api_command: str | None, **kwargs: object) -> argparse.Namespace:
    ns = argparse.Namespace(
        command="api",
        api_command=api_command,
        base_url="http://localhost:8000",
        api_key="sk-test",
    )
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_api_client_exits_when_no_key_provided(capsys):
    with pytest.raises(SystemExit) as ei:
        _api_client("http://localhost:8000", None)
    assert ei.value.code == 1
    assert "SCRAPER_ENGINE_API_KEY" in capsys.readouterr().err


def test_print_api_response_success_prints_json(capsys):
    _print_api_response(FakeResponse(200, {"job_id": "abc"}))  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert '"job_id"' in out


def test_print_api_response_failure_exits_1(capsys):
    with pytest.raises(SystemExit) as ei:
        _print_api_response(FakeResponse(404, {"detail": "not found"}))  # type: ignore[arg-type]
    assert ei.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_run_api_command_no_subcommand_exits_1(capsys):
    with pytest.raises(SystemExit) as ei:
        _run_api_command(_args(None))
    assert ei.value.code == 1


def test_run_api_command_scrape_posts_urls(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("scrape", urls=["https://example.com"], webhook=None))

    method, path, kwargs = fake.calls[0]
    assert method == "POST"
    assert path == "/v1/scrape"
    assert kwargs["json"] == {"urls": ["https://example.com"]}
    assert fake.closed


def test_run_api_command_scrape_includes_webhook_when_set(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(
        _args("scrape", urls=["https://example.com"], webhook="https://hook.example.com")
    )

    _, _, kwargs = fake.calls[0]
    assert kwargs["json"]["webhook"] == "https://hook.example.com"


def test_run_api_command_jobs_gets_with_params(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("jobs", status="FAILED", limit=10, offset=5))

    method, path, kwargs = fake.calls[0]
    assert method == "GET"
    assert path == "/v1/jobs"
    assert kwargs["params"] == {"limit": 10, "offset": 5, "status": "FAILED"}


def test_run_api_command_jobs_omits_status_when_unset(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("jobs", status=None, limit=50, offset=0))

    _, _, kwargs = fake.calls[0]
    assert "status" not in kwargs["params"]


def test_run_api_command_job_gets_by_id(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("job", job_id="abc-123", since=None))

    method, path, kwargs = fake.calls[0]
    assert method == "GET"
    assert path == "/v1/jobs/abc-123"
    assert kwargs["params"] is None


def test_run_api_command_job_forwards_since_cursor(monkeypatch):
    """Round 63 — polling a long job re-sent every result already seen. The
    cursor is what lets a caller fetch only what is new."""
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("job", job_id="abc-123", since="2026-09-20T12:00:00Z"))

    _method, _path, kwargs = fake.calls[0]
    assert kwargs["params"] == {"since": "2026-09-20T12:00:00Z"}


def test_run_api_command_quota_gets_quota(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("quota"))

    method, path, _kwargs = fake.calls[0]
    assert method == "GET"
    assert path == "/v1/quota"


def test_run_api_command_dlq_gets_with_params(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    _run_api_command(_args("dlq", limit=100, offset=0))

    method, path, kwargs = fake.calls[0]
    assert method == "GET"
    assert path == "/v1/dlq"
    assert kwargs["params"] == {"limit": 100, "offset": 0}


def test_run_api_command_closes_client_even_on_error_response(monkeypatch):
    fake = FakeClient()
    fake.response = FakeResponse(500, {"detail": "boom"})
    monkeypatch.setattr(
        "scraper_engine.cli.entrypoint._api_client", lambda base_url, api_key: fake
    )

    with pytest.raises(SystemExit):
        _run_api_command(_args("quota"))

    assert fake.closed

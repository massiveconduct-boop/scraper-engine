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


# --- Round 64: main() dispatch and the ops subcommands (round-62 audit T3) ---

import sys  # noqa: E402
import types  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import scraper_engine.cli.entrypoint as cli  # noqa: E402


@pytest.fixture
def no_bootstrap(monkeypatch):
    monkeypatch.setattr(
        "scraper_engine.observability.bootstrap.bootstrap_observability", lambda _cfg: None
    )


@pytest.mark.parametrize(
    ("argv", "target", "expected_args"),
    [
        (["create-tenant", "acme"], "_create_tenant", ("acme",)),
        (["harvest"], "_harvest_once", ()),
        (["reap"], "_reap_once", ()),
        (["check"], "_check_health", ()),
    ],
)
def test_main_dispatches_async_ops_commands(monkeypatch, no_bootstrap, argv, target, expected_args):
    seen = {}

    async def _fake(*args):
        seen["args"] = args

    monkeypatch.setattr(cli, target, _fake)
    cli.main(argv)
    assert seen["args"] == expected_args


def test_main_dispatches_worker(monkeypatch, no_bootstrap):
    run_worker = MagicMock()
    monkeypatch.setattr(cli, "_run_worker", run_worker)
    cli.main(["worker", "--queues", "a,b"])
    run_worker.assert_called_once_with("a,b")


def test_main_dispatches_api(monkeypatch, no_bootstrap):
    run_api = MagicMock()
    monkeypatch.setattr(cli, "_run_api_command", run_api)
    cli.main(["api", "quota", "--api-key", "k"])
    assert run_api.call_args.args[0].api_command == "quota"


def test_main_serve_runs_uvicorn(monkeypatch, no_bootstrap):
    fake_uvicorn = types.SimpleNamespace(run=MagicMock())
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    cli.main(["serve", "--port", "9001"])
    assert fake_uvicorn.run.call_args.kwargs["port"] == 9001
    assert fake_uvicorn.run.call_args.args[0] == "scraper_engine.api.main:app"


def test_main_without_a_command_exits_1(no_bootstrap, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main([])
    assert ei.value.code == 1
    assert "not yet implemented" in capsys.readouterr().out


def test_run_worker_execs_rq(monkeypatch):
    execvp = MagicMock()
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/rq")
    monkeypatch.setattr("os.execvp", execvp)
    cli._run_worker("q1,q2")
    execvp.assert_called_once_with("/usr/bin/rq", ["/usr/bin/rq", "worker", "q1", "q2"])


def test_run_worker_without_rq_exits_1(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(SystemExit):
        cli._run_worker("q")
    assert "'rq' executable not found" in capsys.readouterr().out


def _fake_pg(monkeypatch):
    pg = MagicMock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        "scraper_engine.storage.postgres_client.PostgresClient", MagicMock(return_value=pg)
    )
    return pg


@pytest.mark.asyncio
async def test_harvest_once_prints_count_and_closes_pg(monkeypatch, capsys):
    pg = _fake_pg(monkeypatch)
    harvester = MagicMock(harvest_once=AsyncMock(return_value=42))
    monkeypatch.setattr(
        "scraper_engine.proxy.harvester.ProxyHarvester", MagicMock(return_value=harvester)
    )
    monkeypatch.setattr("scraper_engine.proxy.asn_classifier.build_asn_classifier", lambda: None)
    await cli._harvest_once()
    assert "42 proxies collected" in capsys.readouterr().out
    pg.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_reap_once_prints_result_and_closes_pg(monkeypatch, capsys):
    pg = _fake_pg(monkeypatch)
    reaper = MagicMock(run_once=AsyncMock(return_value={"deleted": 3}))
    monkeypatch.setattr(
        "scraper_engine.proxy.retention_reaper.RetentionReaper", MagicMock(return_value=reaper)
    )
    await cli._reap_once()
    assert "Reap complete" in capsys.readouterr().out
    pg.stop.assert_awaited_once()


def _health(healthy, checks):
    return types.SimpleNamespace(
        healthy=healthy,
        pgbouncer_reachable=healthy,
        redis_reachable=True,
        s3_reachable=True,
        proxy_pool_size=5,
        checks=checks,
    )


def _fake_storage(monkeypatch):
    clients = {}
    for mod, cls in [
        ("postgres_client", "PostgresClient"),
        ("redis_client", "RedisClient"),
        ("s3_client", "S3Client"),
    ]:
        client = MagicMock(start=AsyncMock(), stop=AsyncMock())
        clients[cls] = client
        monkeypatch.setattr(f"scraper_engine.storage.{mod}.{cls}", MagicMock(return_value=client))
    return clients


@pytest.mark.asyncio
async def test_check_health_healthy_prints_ok(monkeypatch, capsys):
    clients = _fake_storage(monkeypatch)
    monkeypatch.setattr(
        "scraper_engine.api.health.check_health", AsyncMock(return_value=_health(True, {}))
    )
    await cli._check_health()
    out = capsys.readouterr().out
    assert "status: ok" in out
    assert "failures" not in out
    for client in clients.values():
        client.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_health_degraded_prints_failures_and_exits_1(monkeypatch, capsys):
    _fake_storage(monkeypatch)
    monkeypatch.setattr(
        "scraper_engine.api.health.check_health",
        AsyncMock(return_value=_health(False, {"pgbouncer": "refused"})),
    )
    with pytest.raises(SystemExit) as ei:
        await cli._check_health()
    assert ei.value.code == 1
    assert "failures:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_create_tenant_prints_key_and_closes_pg(monkeypatch, capsys):
    pg = _fake_pg(monkeypatch)
    resolver = MagicMock(create_tenant=AsyncMock(return_value=("t-1", "sk-new")))
    monkeypatch.setattr("scraper_engine.api.auth.TenantResolver", MagicMock(return_value=resolver))
    await cli._create_tenant("acme")
    assert "API key: sk-new" in capsys.readouterr().out
    pg.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_tenant_failure_still_closes_pg(monkeypatch):
    """Round 64 — stop() used to run only after a successful create."""
    pg = _fake_pg(monkeypatch)
    resolver = MagicMock(create_tenant=AsyncMock(side_effect=RuntimeError("duplicate slug")))
    monkeypatch.setattr("scraper_engine.api.auth.TenantResolver", MagicMock(return_value=resolver))
    with pytest.raises(RuntimeError):
        await cli._create_tenant("acme")
    pg.stop.assert_awaited_once()


def test_print_api_response_non_json_body_prints_text(capsys):
    class _TextResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    _print_api_response(_TextResponse(200, "plain body"))
    assert "plain body" in capsys.readouterr().out


def test_run_api_command_unknown_subcommand_exits_1(monkeypatch, capsys):
    client = FakeClient()
    monkeypatch.setattr(cli, "_api_client", lambda *_a: client)
    with pytest.raises(SystemExit):
        _run_api_command(_args("bogus"))
    assert "Unknown api subcommand" in capsys.readouterr().err
    assert client.closed


def test_api_client_sends_the_key_and_base_url():
    client = _api_client("http://api.example:8010", "sk-live")
    try:
        assert client.headers["X-API-Key"] == "sk-live"
        assert str(client.base_url) == "http://api.example:8010"
    finally:
        client.close()

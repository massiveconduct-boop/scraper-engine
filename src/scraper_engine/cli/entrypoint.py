# cli/entrypoint.py
"""CLI entry point for scraper-engine management commands."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point. `argv` defaults to sys.argv[1:] (argparse's own
    default); passing it explicitly is what lets every subcommand's dispatch
    be tested without a subprocess."""
    parser = argparse.ArgumentParser(
        prog="scraper-engine",
        description="Search & Scraper Engine management CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    serve = subparsers.add_parser("serve", help="Start the API server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    # worker
    worker = subparsers.add_parser("worker", help="Start an RQ worker")
    worker.add_argument("--queues", default="scraper-jobs")

    # harvest
    _harvest = subparsers.add_parser("harvest", help="Run proxy harvester once")

    # reap (session_retention enforcement)
    _reap = subparsers.add_parser("reap", help="Run retention reaper once")

    # check health
    _check = subparsers.add_parser("check", help="Run a health check")

    # create-tenant (BD-04)
    create = subparsers.add_parser("create-tenant", help="Create a new tenant")
    create.add_argument("tenant_slug")

    # api (round 56) — thin HTTP client wrapping /v1/* for a caller to smoke-test
    # or script against, without curl. Every existing subcommand above talks
    # directly to Postgres/Redis (ops tooling); these instead go over real HTTP
    # through the same auth/SSRF/quota path a real integrator uses.
    _api_common = argparse.ArgumentParser(add_help=False)
    _api_common.add_argument(
        "--base-url", default="http://localhost:8000", help="Scraper Engine API base URL"
    )
    _api_common.add_argument(
        "--api-key",
        default=None,
        help="Tenant API key (defaults to $SCRAPER_ENGINE_API_KEY)",
    )

    api_parser = subparsers.add_parser("api", help="Call the /v1 HTTP API")
    api_sub = api_parser.add_subparsers(dest="api_command")

    api_scrape = api_sub.add_parser(
        "scrape", parents=[_api_common], help="POST /v1/scrape"
    )
    api_scrape.add_argument("urls", nargs="+")
    api_scrape.add_argument("--webhook", default=None)

    api_jobs = api_sub.add_parser("jobs", parents=[_api_common], help="GET /v1/jobs")
    api_jobs.add_argument("--status", default=None)
    api_jobs.add_argument("--limit", type=int, default=50)
    api_jobs.add_argument("--offset", type=int, default=0)

    api_job = api_sub.add_parser("job", parents=[_api_common], help="GET /v1/jobs/{id}")
    api_job.add_argument("job_id")
    api_job.add_argument(
        "--since",
        default=None,
        help=(
            "ISO-8601 timestamp; return only results extracted after it. "
            "Poll a long job with the previous response's newest fetched_at "
            "to fetch just the new results instead of the whole set."
        ),
    )

    api_sub.add_parser("quota", parents=[_api_common], help="GET /v1/quota")

    api_dlq = api_sub.add_parser("dlq", parents=[_api_common], help="GET /v1/dlq")
    api_dlq.add_argument("--limit", type=int, default=100)
    api_dlq.add_argument("--offset", type=int, default=0)

    args = parser.parse_args(argv)

    from scraper_engine.config.loader import load_config
    from scraper_engine.observability.bootstrap import bootstrap_observability

    bootstrap_observability(load_config().observability)

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "scraper_engine.api.main:app",
            host=args.host,
            port=args.port,
            reload=True,
            server_header=False,
        )
    elif args.command == "create-tenant":
        asyncio.run(_create_tenant(args.tenant_slug))
    elif args.command == "harvest":
        asyncio.run(_harvest_once())
    elif args.command == "reap":
        asyncio.run(_reap_once())
    elif args.command == "worker":
        _run_worker(args.queues)
    elif args.command == "check":
        asyncio.run(_check_health())
    elif args.command == "api":
        _run_api_command(args)
    else:
        print(f"Command '{args.command}' not yet implemented. Use --help for available commands.")
        sys.exit(1)


def _run_worker(queues: str) -> None:
    """Start an RQ worker consuming the given (comma-separated) queues.

    Replaces this process with the `rq` CLI via exec — matches what
    docker-compose.yml's worker-* services already run directly; `cli worker`
    is the same entry point for host/manual use.
    """
    import os
    import shutil

    rq_bin = shutil.which("rq")
    if rq_bin is None:
        print("'rq' executable not found on PATH — is the rq package installed?")
        sys.exit(1)
    os.execvp(rq_bin, [rq_bin, "worker", *queues.split(",")])


async def _harvest_once() -> None:
    """Run a single proxy-harvest cycle (manual trigger; the daemon runs it on a
    timer). Builds the harvester from config and prints how many proxies it found."""
    from scraper_engine.config.loader import load_config
    from scraper_engine.proxy.asn_classifier import build_asn_classifier
    from scraper_engine.proxy.harvester import ProxyHarvester
    from scraper_engine.storage.postgres_client import PostgresClient

    cfg = load_config()
    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    try:
        harvester = ProxyHarvester(
            pg, sources=cfg.proxy_harvester.sources, asn_classifier=build_asn_classifier()
        )
        count = await harvester.harvest_once()
        print(f"Harvest complete: {count} proxies collected")
    finally:
        await pg.stop()


async def _reap_once() -> None:
    """Run a single retention-reap cycle (manual trigger; the daemon runs it on
    a timer). Deletes expired browser_sessions/domain_ban_history rows."""
    from scraper_engine.config.loader import load_config
    from scraper_engine.proxy.retention_reaper import RetentionReaper
    from scraper_engine.storage.postgres_client import PostgresClient

    cfg = load_config()
    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    try:
        result = await RetentionReaper(pg, cfg.session_retention).run_once()
        print(f"Reap complete: {result}")
    finally:
        await pg.stop()


async def _check_health() -> None:
    """One-shot composite health check (pg/redis/s3) — for container healthchecks
    and manual ops smoke tests. Exits non-zero if any dependency is unreachable."""
    from scraper_engine.api.health import check_health
    from scraper_engine.config.loader import load_config
    from scraper_engine.storage.postgres_client import PostgresClient
    from scraper_engine.storage.redis_client import RedisClient
    from scraper_engine.storage.s3_client import S3Client

    cfg = load_config()
    pg = PostgresClient(cfg.storage.database_url)
    redis = RedisClient(redis_url=cfg.storage.redis_url)
    s3 = S3Client(
        endpoint_url=cfg.s3.endpoint_url,
        access_key=cfg.s3.access_key,
        secret_key=cfg.s3.secret_key,
        bucket=cfg.s3.bucket,
    )
    await pg.start()
    await redis.start()
    await s3.start()
    try:
        status = await check_health(pg, redis, s3)
    finally:
        await s3.stop()
        await redis.stop()
        await pg.stop()

    print(f"status: {'ok' if status.healthy else 'degraded'}")
    print(f"pgbouncer_reachable: {status.pgbouncer_reachable}")
    print(f"redis_reachable: {status.redis_reachable}")
    print(f"s3_reachable: {status.s3_reachable}")
    print(f"proxy_pool_size: {status.proxy_pool_size}")
    if status.checks:
        print(f"failures: {status.checks}")
    if not status.healthy:
        sys.exit(1)


async def _create_tenant(tenant_slug: str) -> None:
    """Create a new tenant and print its API key (BD-04)."""
    from scraper_engine.api.auth import TenantResolver
    from scraper_engine.config.loader import load_config
    from scraper_engine.storage.postgres_client import PostgresClient

    pg = PostgresClient(load_config().storage.database_url)
    await pg.start()
    # Round 64 — stop() was after the prints, not in a finally, so a failed
    # create (duplicate slug, schema error) leaked the pool; every other
    # one-shot command here already used try/finally.
    try:
        tenant_id, api_key = await TenantResolver(pg).create_tenant(tenant_slug)
        print(f"Tenant created: {tenant_id}")
        print(f"API key: {api_key}")
    finally:
        await pg.stop()


def _api_client(base_url: str, api_key: str | None) -> httpx.Client:
    """Build the shared httpx client for `api` subcommands (round 56) — a
    curl replacement, not a second implementation of the request logic those
    routes already validate server-side."""
    import httpx

    if not api_key:
        print(
            "No API key provided — pass --api-key or set SCRAPER_ENGINE_API_KEY",
            file=sys.stderr,
        )
        sys.exit(1)
    return httpx.Client(base_url=base_url, headers={"X-API-Key": api_key}, timeout=30.0)


def _print_api_response(resp: httpx.Response) -> None:
    """Pretty-print the response body; non-2xx exits 1 (same convention
    `_check_health` already uses for its own failure exit code)."""
    import json

    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if resp.is_success:
        print(json.dumps(body, indent=2) if isinstance(body, dict | list) else body)
        return
    print(json.dumps(body, indent=2) if isinstance(body, dict | list) else body, file=sys.stderr)
    sys.exit(1)


def _run_api_command(args: argparse.Namespace) -> None:
    """Dispatch an `api <subcommand>` call to the matching /v1 route."""
    import os

    if args.api_command is None:
        print("Use 'scraper-engine api --help' for available subcommands.", file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get("SCRAPER_ENGINE_API_KEY")
    client = _api_client(args.base_url, api_key)
    try:
        if args.api_command == "scrape":
            payload: dict[str, object] = {"urls": args.urls}
            if args.webhook:
                payload["webhook"] = args.webhook
            resp = client.post("/v1/scrape", json=payload)
        elif args.api_command == "jobs":
            params: dict[str, str | int] = {"limit": args.limit, "offset": args.offset}
            if args.status:
                params["status"] = args.status
            resp = client.get("/v1/jobs", params=params)
        elif args.api_command == "job":
            resp = client.get(
                f"/v1/jobs/{args.job_id}",
                params={"since": args.since} if args.since else None,
            )
        elif args.api_command == "quota":
            resp = client.get("/v1/quota")
        elif args.api_command == "dlq":
            resp = client.get(
                "/v1/dlq", params={"limit": args.limit, "offset": args.offset}
            )
        else:
            print(f"Unknown api subcommand: {args.api_command}", file=sys.stderr)
            sys.exit(1)
    finally:
        client.close()

    _print_api_response(resp)


if __name__ == "__main__":  # pragma: no cover — `python -m` entry only
    main()

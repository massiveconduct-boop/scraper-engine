# cli/entrypoint.py
"""CLI entry point for scraper-engine management commands."""

from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> None:
    """Main CLI entry point."""
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

    # check health
    _check = subparsers.add_parser("check", help="Run a health check")

    # create-tenant (BD-04)
    create = subparsers.add_parser("create-tenant", help="Create a new tenant")
    create.add_argument("tenant_slug")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        uvicorn.run("api.main:app", host=args.host, port=args.port, reload=True)
    elif args.command == "create-tenant":
        asyncio.run(_create_tenant(args.tenant_slug))
    elif args.command == "harvest":
        asyncio.run(_harvest_once())
    elif args.command == "worker":
        _run_worker(args.queues)
    elif args.command == "check":
        asyncio.run(_check_health())
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
    from config.loader import load_config
    from proxy.asn_classifier import build_asn_classifier
    from proxy.harvester import ProxyHarvester
    from storage.postgres_client import PostgresClient

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


async def _check_health() -> None:
    """One-shot composite health check (pg/redis/s3) — for container healthchecks
    and manual ops smoke tests. Exits non-zero if any dependency is unreachable."""
    from api.health import check_health
    from config.loader import load_config
    from storage.postgres_client import PostgresClient
    from storage.redis_client import RedisClient
    from storage.s3_client import S3Client

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
    from api.auth import TenantResolver
    from config.loader import load_config
    from storage.postgres_client import PostgresClient

    pg = PostgresClient(load_config().storage.database_url)
    await pg.start()

    resolver = TenantResolver(pg)
    tenant_id, api_key = await resolver.create_tenant(tenant_slug)
    print(f"Tenant created: {tenant_id}")
    print(f"API key: {api_key}")

    await pg.stop()


if __name__ == "__main__":
    main()

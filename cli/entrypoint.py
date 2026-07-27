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
    worker.add_argument("--queues", default="scraper-level1,scraper-level2,scraper-level3")

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
    else:
        print(f"Command '{args.command}' not yet implemented. Use --help for available commands.")
        sys.exit(1)


async def _harvest_once() -> None:
    """Run a single proxy-harvest cycle (manual trigger; the daemon runs it on a
    timer). Builds the harvester from config and prints how many proxies it found."""
    from config.loader import load_config
    from proxy.harvester import ProxyHarvester
    from storage.postgres_client import PostgresClient

    cfg = load_config()
    pg = PostgresClient(cfg.storage.database_url)
    await pg.start()
    try:
        count = await ProxyHarvester(pg, sources=cfg.proxy_harvester.sources).harvest_once()
        print(f"Harvest complete: {count} proxies collected")
    finally:
        await pg.stop()


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

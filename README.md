# Scraper Engine

Async Python multi-level web scraping system with anti-detection, proxy management, and SSRF safety.

## Quick Start

```bash
pip install -e ".[dev]"
docker compose up -d postgres redis
alembic upgrade head
uvicorn api.main:app --reload
```

## Architecture

See `specs/scraper-engine-blueprint-v2.md` for the authoritative specification.

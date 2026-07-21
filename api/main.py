# api/main.py
"""FastAPI application entry point."""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from .middleware import configure_middleware
    from .routes import register_routes

    app = FastAPI(
        title="Scraper Engine",
        version="0.1.0",
        description="Multi-level web scraping with anti-detection + proxy management",
    )
    configure_middleware(app)
    register_routes(app)
    return app


app = create_app()

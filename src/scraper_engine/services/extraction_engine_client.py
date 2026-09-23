# services/extraction_engine_client.py
"""extraction-engine client for schema-driven structured extraction of scraped HTML.

extraction-engine is a separate, self-hosted service (own repo, own Docker
image) — there is no public hosted default the way Firecrawl has one, so a
client is only built when EXTRACTION_ENGINE_BASE_URL actually points at a
running instance.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# extraction-engine's wire schema requires a {"schema_version", "fields"} envelope
# (its own core/schema/compiler.py rejects anything else with a 422) -- but every
# real caller here (ConfigOverrides.extraction_schema, AdaptiveSelector's own
# shorthand convention) supplies a bare {"field_name": "type"} mapping. Detected
# live: a bare shorthand dict sent as-is 422'd against a real running
# extraction-engine instance. Auto-wrapping keeps callers ergonomic without
# needing to know extraction-engine's wire format.
_ENVELOPE_KEYS = frozenset({"schema_version", "fields"})
_DEFAULT_SCHEMA_VERSION = "1.0.0"


def _as_wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if _ENVELOPE_KEYS.issubset(schema.keys()):
        return schema
    return {"schema_version": _DEFAULT_SCHEMA_VERSION, "fields": schema}


class ExtractionEngineClient:
    """Thin client over extraction-engine's /v1/extract API for schema-driven
    structured extraction. Mirrors services/firecrawl_client.py's shape
    exactly, including its "never crash the caller, fail soft" contract —
    extract() returns None (not a raised exception) on any HTTP/network
    failure, so Worker.process_job can fall back to AdaptiveSelector instead
    of losing the whole job over a down/misconfigured extraction-engine."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def extract(
        self,
        html: str,
        schema: dict[str, Any],
        *,
        enable_smallmodel: bool = False,
        enable_llm: bool = False,
    ) -> dict[str, Any] | None:
        """Real extraction-engine call. `schema` may be a bare
        {"field_name": "type"} shorthand mapping (auto-wrapped into
        extraction-engine's real {schema_version, fields} envelope) or an
        already-complete envelope (passed through unchanged). Returns None
        (never raises) on any connection error, timeout, non-2xx response,
        or malformed JSON — same fail-soft contract as
        FirecrawlClient.convert_to_markdown."""
        try:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._base_url}/v1/extract",
                    json={
                        "html": html,
                        "schema": _as_wire_schema(schema),
                        "enable_smallmodel": enable_smallmodel,
                        "enable_llm": enable_llm,
                    },
                    headers=headers,
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except Exception:
            return None


def build_extraction_engine_client() -> ExtractionEngineClient | None:
    """Select the extraction-engine client for production use.

    EXTRACTION_ENGINE_BASE_URL must point at a running extraction-engine
    instance (e.g. http://extraction-engine:8080 on a shared Docker
    network) — unlike Firecrawl there's no hosted default to fall back to,
    so an unset base URL means schema-driven extraction is simply skipped
    (Worker.process_job falls back to AdaptiveSelector), same env-gated,
    gracefully-inert pattern as build_firecrawl_client/build_captcha_solver.
    EXTRACTION_ENGINE_API_KEY is optional — matches extraction-engine's own
    opt-in EXTRACTION_API_KEY auth (unset means its API is fully open).
    """
    base_url = os.environ.get("EXTRACTION_ENGINE_BASE_URL")
    if not base_url:
        return None
    return ExtractionEngineClient(base_url, api_key=os.environ.get("EXTRACTION_ENGINE_API_KEY"))

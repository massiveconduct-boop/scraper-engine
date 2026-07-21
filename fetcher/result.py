# fetcher/result.py
"""FetchResult contract shared by all fetch levels.

Re-exports core.models.FetchResult as the canonical fetch result type.
All three fetch levels (L1/L2/L3) MUST return this exact type.
"""

from __future__ import annotations

from core.models import FetchResult  # noqa: F401 — canonical re-export

__all__ = ["FetchResult"]

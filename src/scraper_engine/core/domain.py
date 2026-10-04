# core/domain.py
"""Registrable-domain helper (round 73).

A site refuses a scraper as a whole (`www.nejm.org` and `nejm.org` share one
WAF), so the per-site refusal memory keys on the registrable domain, not the
hostname the per-domain level hints use.

The public-suffix list is the snapshot shipped inside `tldextract`: no network
fetch at import or call time, so a Redis-side memory lookup can never block on
a download.
"""

from __future__ import annotations

import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def registrable_domain(host: str) -> str:
    """`www.pulse.ng` -> `pulse.ng`, `a.b.co.uk` -> `b.co.uk`.

    A host with no public suffix (`localhost`, an IP address, a bare label)
    comes back unchanged: it is its own site.
    """
    return _EXTRACT(host).top_domain_under_public_suffix or host

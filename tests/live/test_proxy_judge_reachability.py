# tests/live/test_proxy_judge_reachability.py
"""Confirms proxy/harvester.py's production proxy-validation targets
(JUDGE_URLS) include at least one real, correctly-shaped,
internet-reachable endpoint — the class of check that would have caught
the loopback-judge architectural bug (round 32) sooner: a self-hosted
judge on 127.0.0.1 cannot validate a real third-party proxy, because
loopback is resolved by whoever makes the request (the proxy itself),
never this machine.

Asserts "at least one of JUDGE_URLS works," not "all of them" — the whole
point of having multiple independent, differently-hosted candidates
(added in the same round after httpbin.org was found live-down,
persistent 503s, while building this fix) is that any single one being
down is expected and tolerated, not a failure.

Deliberately narrow scope beyond that: this does NOT validate a real
third-party proxy end to end — free proxy availability/reliability is
inherently too flaky for a deterministic test (see this session's own
live escalation-ladder check, which correctly treats PROXY_EXHAUSTED as
an expected, not-fixable-by-code outcome on this sandbox's free proxy
sources). This only proves at least one target endpoint itself is alive
and shaped as expected, which
tests/integration/test_promotion.py's local judge_server.py stand-in
structurally cannot (see that file's own docstring).
"""

import httpx
import pytest

from scraper_engine.proxy.harvester import JUDGE_URLS


@pytest.mark.live
async def test_at_least_one_judge_url_is_reachable_and_shaped_correctly():
    reachable = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in JUDGE_URLS:
            try:
                resp = await client.get(url)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if any(key in data for key in ("origin", "ip", "headers")):
                reachable.append(url)
    assert reachable, f"none of {JUDGE_URLS} were reachable and correctly shaped"

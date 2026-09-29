"""Round 73 — core/domain.py: the registrable domain a site refusal is keyed on."""

import pytest

from scraper_engine.core.domain import registrable_domain


@pytest.mark.parametrize(
    ("host", "site"),
    [
        ("www.pulse.ng", "pulse.ng"),
        ("pulse.ng", "pulse.ng"),
        ("www.nejm.org", "nejm.org"),
        ("a.b.co.uk", "b.co.uk"),
        ("namu.wiki", "namu.wiki"),
        ("localhost", "localhost"),
        ("10.0.0.1", "10.0.0.1"),
    ],
)
def test_registrable_domain(host, site):
    assert registrable_domain(host) == site

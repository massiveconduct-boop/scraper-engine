# tests/unit/test_asn_classifier.py
"""ASN classification — closes the permanent ASN_BONUS scoring gap.

NullAsnClassifier (always "unknown") is the honest do-nothing fallback.
ReverseDnsAsnClassifier is the real implementation, unconditionally
selected by build_asn_classifier() — it needs no external account or
database file, just a DNS PTR lookup.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from scraper_engine.proxy.asn_classifier import (
    NullAsnClassifier,
    ReverseDnsAsnClassifier,
    build_asn_classifier,
)


@pytest.mark.asyncio
async def test_null_classifier_always_returns_unknown():
    result = await NullAsnClassifier().classify("1.2.3.4")
    assert result == "unknown"


def test_build_asn_classifier_returns_reverse_dns_classifier():
    classifier = build_asn_classifier()
    assert isinstance(classifier, ReverseDnsAsnClassifier)


async def _classify_with_getnameinfo(getnameinfo, ip: str = "1.2.3.4") -> str:
    classifier = ReverseDnsAsnClassifier()
    loop = asyncio.get_running_loop()
    loop.getnameinfo = getnameinfo
    try:
        return await classifier.classify(ip)
    finally:
        del loop.getnameinfo


@pytest.mark.asyncio
async def test_reverse_dns_classifier_maps_datacenter_hostname():
    getnameinfo = AsyncMock(return_value=("ec2-1-2-3-4.compute-1.amazonaws.com", 0))
    assert await _classify_with_getnameinfo(getnameinfo) == "datacenter"


@pytest.mark.asyncio
async def test_reverse_dns_classifier_maps_mobile_hostname():
    getnameinfo = AsyncMock(return_value=("host.t-mobile.com", 0))
    assert await _classify_with_getnameinfo(getnameinfo) == "mobile"


@pytest.mark.asyncio
async def test_reverse_dns_classifier_defaults_to_residential():
    getnameinfo = AsyncMock(return_value=("c-73-1-2-3.hsd1.ca.comcast.net", 0))
    assert await _classify_with_getnameinfo(getnameinfo) == "residential"


@pytest.mark.asyncio
async def test_reverse_dns_classifier_returns_unknown_when_no_ptr_record():
    # getnameinfo() without a PTR record echoes the numeric IP back.
    getnameinfo = AsyncMock(return_value=("9.9.9.9", 0))
    assert await _classify_with_getnameinfo(getnameinfo, ip="9.9.9.9") == "unknown"


@pytest.mark.asyncio
async def test_reverse_dns_classifier_returns_unknown_on_os_error():
    getnameinfo = AsyncMock(side_effect=OSError("no PTR record"))
    assert await _classify_with_getnameinfo(getnameinfo) == "unknown"


@pytest.mark.asyncio
async def test_reverse_dns_classifier_returns_unknown_on_timeout():
    async def _slow(*args: object, **kwargs: object) -> tuple[str, int]:
        await asyncio.sleep(1)
        return ("irrelevant", 0)

    classifier = ReverseDnsAsnClassifier(timeout_seconds=0.01)
    loop = asyncio.get_running_loop()
    loop.getnameinfo = _slow
    try:
        result = await classifier.classify("11.11.11.11")
    finally:
        del loop.getnameinfo
    assert result == "unknown"

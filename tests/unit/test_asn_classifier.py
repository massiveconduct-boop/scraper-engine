# tests/unit/test_asn_classifier.py
"""ASN classification — closes the permanent ASN_BONUS scoring gap.

FakeClassifier (always "unknown") was the only classifier ever wired in
production. NullAsnClassifier keeps that honest fallback; MaxMindAsnClassifier
is the real implementation, auto-selected by build_asn_classifier() when
GEOIP_ASN_DB_PATH points at an existing file.
"""

from unittest.mock import MagicMock, patch

import pytest

from scraper_engine.proxy.asn_classifier import (
    MaxMindAsnClassifier,
    NullAsnClassifier,
    build_asn_classifier,
)


@pytest.mark.asyncio
async def test_null_classifier_always_returns_unknown():
    result = await NullAsnClassifier().classify("1.2.3.4")
    assert result == "unknown"


def test_build_asn_classifier_falls_back_to_null_when_env_unset(monkeypatch):
    monkeypatch.delenv("GEOIP_ASN_DB_PATH", raising=False)
    classifier = build_asn_classifier()
    assert isinstance(classifier, NullAsnClassifier)


def test_build_asn_classifier_falls_back_to_null_when_file_missing(monkeypatch):
    monkeypatch.setenv("GEOIP_ASN_DB_PATH", "/nonexistent/GeoLite2-ASN.mmdb")
    classifier = build_asn_classifier()
    assert isinstance(classifier, NullAsnClassifier)


def test_build_asn_classifier_selects_maxmind_when_db_present(monkeypatch, tmp_path):
    db_path = tmp_path / "GeoLite2-ASN.mmdb"
    db_path.write_bytes(b"not a real mmdb, open_database is mocked")
    monkeypatch.setenv("GEOIP_ASN_DB_PATH", str(db_path))

    with patch("maxminddb.open_database", return_value=MagicMock()) as mock_open:
        classifier = build_asn_classifier()

    assert isinstance(classifier, MaxMindAsnClassifier)
    mock_open.assert_called_once_with(str(db_path))


@pytest.mark.asyncio
async def test_maxmind_classifier_maps_datacenter_org():
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.return_value = {
        "autonomous_system_number": 16509,
        "autonomous_system_organization": "AMAZON-02",
    }
    assert await classifier.classify("3.3.3.3") == "datacenter"


@pytest.mark.asyncio
async def test_maxmind_classifier_maps_mobile_org():
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.return_value = {
        "autonomous_system_organization": "T-Mobile USA, Inc.",
    }
    assert await classifier.classify("4.4.4.4") == "mobile"


@pytest.mark.asyncio
async def test_maxmind_classifier_defaults_to_residential():
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.return_value = {
        "autonomous_system_organization": "Comcast Cable Communications, LLC",
    }
    assert await classifier.classify("5.5.5.5") == "residential"


@pytest.mark.asyncio
async def test_maxmind_classifier_returns_unknown_on_no_record():
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.return_value = None
    assert await classifier.classify("6.6.6.6") == "unknown"


@pytest.mark.asyncio
async def test_maxmind_classifier_returns_unknown_on_lookup_value_error():
    """mmdb lookups raise ValueError for malformed IPs — must not crash the caller."""
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.side_effect = ValueError("invalid IP address")
    assert await classifier.classify("not-an-ip") == "unknown"


@pytest.mark.asyncio
async def test_maxmind_classifier_returns_unknown_on_lookup_os_error():
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.side_effect = OSError("db closed")
    assert await classifier.classify("7.7.7.7") == "unknown"


@pytest.mark.asyncio
async def test_maxmind_classifier_returns_unknown_when_org_field_missing():
    with patch("maxminddb.open_database", return_value=MagicMock()):
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier._reader.get.return_value = {"autonomous_system_number": 12345}
    assert await classifier.classify("8.8.8.8") == "unknown"


def test_maxmind_classifier_close_closes_reader():
    with patch("maxminddb.open_database", return_value=MagicMock()) as mock_open:
        classifier = MaxMindAsnClassifier("/fake/path.mmdb")
    classifier.close()
    mock_open.return_value.close.assert_called_once()


def test_build_asn_classifier_falls_back_to_null_on_open_failure(monkeypatch, tmp_path):
    """A present-but-corrupt .mmdb must not crash the harvester — degrade to null."""
    db_path = tmp_path / "GeoLite2-ASN.mmdb"
    db_path.write_bytes(b"corrupt, open_database is mocked to fail")
    monkeypatch.setenv("GEOIP_ASN_DB_PATH", str(db_path))

    with patch("maxminddb.open_database", side_effect=OSError("corrupt database")):
        classifier = build_asn_classifier()

    assert isinstance(classifier, NullAsnClassifier)

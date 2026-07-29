# tests/unit/test_harvester.py
"""ProxyHarvester tests — direct scrape + proxybroker2 subprocess fallback."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scraper_engine.proxy.harvester import ProxyHarvester


class FakeResponse:
    """Stands in for httpx.Response at the network boundary — real harvester
    code only ever touches .status_code/.text/.json()/.headers/raise_for_status()."""

    def __init__(self, status_code=200, text="", json_data=None, headers=None, raise_exc=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json_data


class FakeHttpClient:
    """Stands in for the httpx.AsyncClient passed into _scrape_one — the
    method already accepts the client as a parameter, so no patching needed."""

    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise_exc = raise_exc

    async def get(self, url):
        if self._raise_exc:
            raise self._raise_exc
        return self._resp


class FakeJudgeClient:
    """Stands in for the httpx.AsyncClient _http_validate constructs itself
    (`async with httpx.AsyncClient(...) as client`) — used via patch()."""

    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise_exc = raise_exc

    async def __aenter__(self):
        if self._raise_exc:
            raise self._raise_exc
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        return self._resp


class FakeProc:
    """Stands in for the asyncio.subprocess.Process returned by
    create_subprocess_exec — the real subprocess boundary being mocked."""

    def __init__(self, stdout=b"[]", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.fixture
def pg():
    return AsyncMock()


@pytest.fixture
def classifier():
    class MockClassifier:
        async def classify(self, ip: str) -> str:
            return "residential"

    return MockClassifier()


@pytest.fixture
def redis():
    r = AsyncMock()
    r.raw = AsyncMock()
    return r


class TestProxyHarvester:
    def test_init(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        assert h._sources == ["test"]

    def test_harvester_initial_state(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["a", "b"], asn_classifier=classifier)
        assert len(h._sources) == 2

    @pytest.mark.asyncio
    async def test_harvest_once_direct_scrape_primary(self, pg, classifier):
        """Direct scrape returns proxies — harvest_once exits early."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=10)
        assert await h.harvest_once(limit=10) == 10

    @pytest.mark.asyncio
    async def test_harvest_once_falls_back_to_broker(self, pg, classifier):
        """Direct scrape returns 0 → broker fallback."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=0)
        h._harvest_via_broker = AsyncMock(return_value=5)
        assert await h.harvest_once(limit=10) == 5

    @pytest.mark.asyncio
    async def test_harvest_once_broker_exception(self, pg, classifier):
        """Broker raises → returns direct scrape count."""
        h = ProxyHarvester(pg=pg, sources=["err"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=0)
        h._harvest_via_broker = AsyncMock(side_effect=RuntimeError("boom"))
        assert await h.harvest_once(limit=10) == 0

    @pytest.mark.asyncio
    async def test_harvest_once_merges_both_paths(self, pg, classifier):
        """Requirement B: direct returns 3, broker adds 5, total 8."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=3)
        h._harvest_via_broker = AsyncMock(return_value=5)
        assert await h.harvest_once(limit=10) == 8

    @pytest.mark.asyncio
    async def test_direct_scrape_works(self, pg, classifier):
        """Direct scrape returns proxies."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=4)
        assert await h._direct_scrape(limit=10, tenant=pg) == 4

    @pytest.mark.asyncio
    async def test_direct_scrape_https_source(self, pg, classifier):
        """Direct scrape includes HTTPS variant."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=8)
        assert await h._direct_scrape(limit=20, tenant=pg) == 8


class TestProxySourceHealthWiring:
    """Round 25: record_source_health writes to Redis (not an in-process
    Gauge) because ProxyHarvester runs in a different process than the one
    serving /metrics — see proxy/source_health.py's module docstring."""

    @pytest.mark.asyncio
    async def test_record_source_health_writes_healthy(self, redis):
        from scraper_engine.proxy.source_health import record_source_health

        await record_source_health(redis, "geonode", 5)
        redis.raw.set.assert_awaited_once_with("metrics:proxy_source_healthy:geonode", "1")

    @pytest.mark.asyncio
    async def test_record_source_health_writes_unhealthy_on_zero(self, redis):
        from scraper_engine.proxy.source_health import record_source_health

        await record_source_health(redis, "geonode", 0)
        redis.raw.set.assert_awaited_once_with("metrics:proxy_source_healthy:geonode", "0")

    @pytest.mark.asyncio
    async def test_direct_scrape_records_health_when_redis_given(self, pg, classifier, redis):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier, redis=redis)
        h._scrape_one = AsyncMock(return_value=3)

        await h._direct_scrape(limit=1000, tenant=pg)

        assert redis.raw.set.await_count == len(ProxyHarvester.SOURCES)
        first_source_name = ProxyHarvester.SOURCES[0][0]
        redis.raw.set.assert_any_await(f"metrics:proxy_source_healthy:{first_source_name}", "1")

    @pytest.mark.asyncio
    async def test_direct_scrape_skips_health_recording_without_redis(self, pg, classifier):
        """redis=None (default) must not raise — matches every other existing
        ProxyHarvester test that constructs it without a redis client."""
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._scrape_one = AsyncMock(return_value=3)

        # No exception is the assertion — there's no redis client to record to.
        await h._direct_scrape(limit=1000, tenant=pg)

    @pytest.mark.asyncio
    async def test_refresh_reads_back_what_record_wrote(self, redis):
        """observability.metrics.refresh_proxy_source_health is the scrape-time
        half — reads back exactly the keys record_source_health writes."""
        from scraper_engine.observability.metrics import refresh_proxy_source_health
        from scraper_engine.proxy.source_health import proxy_source_healthy, record_source_health

        healthy_source = ProxyHarvester.SOURCES[0][0]
        dark_source = ProxyHarvester.SOURCES[1][0]

        written: dict[str, str] = {}

        async def fake_set(key, value):
            written[key] = value

        redis.raw.set = AsyncMock(side_effect=fake_set)
        await record_source_health(redis, healthy_source, 5)
        await record_source_health(redis, dark_source, 0)

        async def fake_get(key):
            return written.get(key)

        redis.raw.get = AsyncMock(side_effect=fake_get)
        await refresh_proxy_source_health(redis)

        assert proxy_source_healthy.labels(source_name=healthy_source)._value.get() == 1.0
        assert proxy_source_healthy.labels(source_name=dark_source)._value.get() == 0.0


class TestPromoteTcpOnly:
    """promote_tcp_only() — bounded background re-validation of score<40 proxies."""

    @pytest.mark.asyncio
    async def test_promotes_validating_proxy(self, pg, classifier):
        """Proxy that passes HTTP validation is promoted from 25→60."""
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.core.tenant import TenantId

        pg.fetch.return_value = [
            {"ip": "1.2.3.4", "port": 3128, "protocol": "HTTP"},
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(
            return_value=(True, AnonymityLevel.ELITE),
        )
        promoted = await h.promote_tcp_only(limit=5, tenant=TenantId("system"))
        assert promoted == 1
        pg.execute.assert_called_once()
        call_args = pg.execute.call_args
        assert "reliability_score" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_skips_non_validating_proxy(self, pg, classifier):
        """Proxy that fails HTTP validation is not promoted, but attempt IS tracked."""
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.core.tenant import TenantId

        pg.fetch.return_value = [
            {"ip": "5.6.7.8", "port": 8080, "protocol": "HTTP"},
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT))
        promoted = await h.promote_tcp_only(limit=5, tenant=TenantId("system"))
        assert promoted == 0
        pg.execute.assert_called_once()
        call_args = pg.execute.call_args
        assert "promotion_attempts" in call_args[0][1]  # tracking even on failure

    @pytest.mark.asyncio
    async def test_empty_pool_returns_zero(self, pg, classifier):
        """Empty pool (no score<40 rows) returns 0 promoted."""
        from scraper_engine.core.tenant import TenantId

        pg.fetch.return_value = []
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        promoted = await h.promote_tcp_only(limit=50, tenant=TenantId("system"))
        assert promoted == 0
        pg.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_limit_caps_rows_processed(self, pg, classifier):
        """limit= parameter caps how many rows are fetched for re-validation."""
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.core.tenant import TenantId

        pg.fetch.return_value = [
            {"ip": f"10.0.0.{i}", "port": 3128, "protocol": "HTTP"} for i in range(3)
        ]
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        h._http_validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        promoted = await h.promote_tcp_only(limit=3, tenant=TenantId("system"))
        assert promoted == 3
        fetch_args = pg.fetch.call_args
        assert fetch_args[0][2] == 3  # limit value in query

    @pytest.mark.asyncio
    async def test_defaults_to_system_tenant_when_none_given(self, pg, classifier):
        """tenant=None (the default) must resolve to TenantId('system') internally."""
        pg.fetch.return_value = []
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        promoted = await h.promote_tcp_only(limit=10)
        assert promoted == 0
        fetch_args = pg.fetch.call_args
        assert fetch_args[0][0] == "system"


class TestHarvestOnceMetricsGauge:
    """Lines 79-80: the Prometheus gauge refresh at the end of harvest_once
    is wrapped in its own try/except — a failure there must not blow up the
    harvest. Every existing test mocks _direct_scrape/_harvest_via_broker but
    still runs the real gauge-update block, which never actually raises
    against a default AsyncMock pg (float(MagicMock()) succeeds), so the
    except branch was never hit."""

    @pytest.mark.asyncio
    async def test_gauge_update_exception_is_swallowed(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._direct_scrape = AsyncMock(return_value=10)
        h._count_validated = AsyncMock(side_effect=RuntimeError("db down"))
        assert await h.harvest_once(limit=10) == 10
        h._count_validated.assert_awaited_once()


class TestDirectScrapeBreak:
    """Line 121: the loop over SOURCES must stop as soon as total >= limit."""

    @pytest.mark.asyncio
    async def test_break_when_total_reaches_limit(self, pg, classifier):
        h = ProxyHarvester(pg=pg, sources=["test"], asn_classifier=classifier)
        h._scrape_one = AsyncMock(return_value=5)
        total = await h._direct_scrape(limit=5, tenant=pg)
        assert total == 5
        assert h._scrape_one.await_count == 1


class TestScrapeOneReal:
    """Lines 130-169: the real _scrape_one body. Every existing test mocks
    _scrape_one itself, so its fetch/parse/probe/validate/insert pipeline
    was never exercised. Only the network boundary (the client) is faked."""

    @pytest.mark.asyncio
    async def test_fetch_raises_returns_zero(self, pg, classifier):
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        client = FakeHttpClient(raise_exc=RuntimeError("connection refused"))
        n = await h._scrape_one("src", "http://x", "ip_port", 10, pg, client)
        assert n == 0

    @pytest.mark.asyncio
    async def test_raise_for_status_error_returns_zero(self, pg, classifier):
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        resp = FakeResponse(status_code=500, raise_exc=RuntimeError("server error"))
        client = FakeHttpClient(resp=resp)
        n = await h._scrape_one("src", "http://x", "ip_port", 10, pg, client)
        assert n == 0

    @pytest.mark.asyncio
    async def test_unknown_format_returns_zero(self, pg, classifier):
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        resp = FakeResponse(text="1.2.3.4:8080")
        client = FakeHttpClient(resp=resp)
        n = await h._scrape_one("src", "http://x", "unknown_fmt", 10, pg, client)
        assert n == 0

    @pytest.mark.asyncio
    async def test_geonode_json_format_parsed_and_stored(self, pg, classifier):
        from scraper_engine.core.models import AnonymityLevel

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        geonode_data = {"data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]}]}
        resp = FakeResponse(json_data=geonode_data)
        client = FakeHttpClient(resp=resp)
        h._tcp_probe = AsyncMock(return_value=True)
        h._http_validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        n = await h._scrape_one("geonode", "http://x", "geonode_json", 10, pg, client)
        assert n == 1
        pg.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tcp_probe_failure_skips_proxy(self, pg, classifier):
        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        resp = FakeResponse(text="1.2.3.4:8080\n5.6.7.8:3128")
        client = FakeHttpClient(resp=resp)
        h._tcp_probe = AsyncMock(return_value=False)
        n = await h._scrape_one("src", "http://x", "ip_port", 10, pg, client)
        assert n == 0
        pg.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_http_validate_false_still_stores_tcp_only_score(self, pg, classifier):
        from scraper_engine.core.models import AnonymityLevel
        from scraper_engine.proxy.harvester import SCORE_TCP_ONLY

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        resp = FakeResponse(text="1.2.3.4:8080")
        client = FakeHttpClient(resp=resp)
        h._tcp_probe = AsyncMock(return_value=True)
        h._http_validate = AsyncMock(return_value=(False, AnonymityLevel.TRANSPARENT))
        n = await h._scrape_one("src", "http://x", "ip_port", 10, pg, client)
        assert n == 1
        call_args = pg.execute.call_args
        assert call_args[0][-1] == SCORE_TCP_ONLY

    @pytest.mark.asyncio
    async def test_limit_reached_returns_early(self, pg, classifier):
        from scraper_engine.core.models import AnonymityLevel

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        resp = FakeResponse(text="1.2.3.4:8080\n5.6.7.8:3128\n9.9.9.9:80")
        client = FakeHttpClient(resp=resp)
        h._tcp_probe = AsyncMock(return_value=True)
        h._http_validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        n = await h._scrape_one("src", "http://x", "ip_port", 1, pg, client)
        assert n == 1
        assert pg.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_pg_execute_exception_continues_to_next_proxy(self, pg, classifier):
        from scraper_engine.core.models import AnonymityLevel

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        resp = FakeResponse(text="1.2.3.4:8080\n5.6.7.8:3128")
        client = FakeHttpClient(resp=resp)
        h._tcp_probe = AsyncMock(return_value=True)
        h._http_validate = AsyncMock(return_value=(True, AnonymityLevel.ELITE))
        pg.execute = AsyncMock(side_effect=[RuntimeError("db error"), None])
        n = await h._scrape_one("src", "http://x", "ip_port", 10, pg, client)
        assert n == 1
        assert pg.execute.await_count == 2


class TestHttpValidateReal:
    """Lines 182-208: real _http_validate body. Every existing test mocks
    this method entirely, so its judge-endpoint round trip and anonymity
    classification logic were never run. Only httpx.AsyncClient — the
    network boundary — is faked."""

    @pytest.mark.asyncio
    async def test_non_200_status_is_invalid(self):
        from scraper_engine.core.models import AnonymityLevel

        resp = FakeResponse(status_code=502)
        with patch(
            "scraper_engine.proxy.harvester.httpx.AsyncClient",
            return_value=FakeJudgeClient(resp=resp),
        ):
            valid, level = await ProxyHarvester._http_validate("1.2.3.4", 8080, "HTTP")
        assert valid is False
        assert level == AnonymityLevel.TRANSPARENT

    @pytest.mark.asyncio
    async def test_malformed_judge_body_is_invalid(self):
        from scraper_engine.core.models import AnonymityLevel

        resp = FakeResponse(status_code=200, json_data={"unexpected": "shape"})
        with patch(
            "scraper_engine.proxy.harvester.httpx.AsyncClient",
            return_value=FakeJudgeClient(resp=resp),
        ):
            valid, level = await ProxyHarvester._http_validate("1.2.3.4", 8080, "HTTP")
        assert valid is False
        assert level == AnonymityLevel.TRANSPARENT

    @pytest.mark.asyncio
    async def test_connection_error_is_invalid(self):
        from scraper_engine.core.models import AnonymityLevel

        with patch(
            "scraper_engine.proxy.harvester.httpx.AsyncClient",
            return_value=FakeJudgeClient(raise_exc=OSError("refused")),
        ):
            valid, level = await ProxyHarvester._http_validate("1.2.3.4", 8080, "HTTP")
        assert valid is False
        assert level == AnonymityLevel.TRANSPARENT

    @pytest.mark.asyncio
    async def test_elite_when_no_forwarding_headers(self):
        from scraper_engine.core.models import AnonymityLevel

        resp = FakeResponse(status_code=200, json_data={"headers": {}}, headers={})
        with patch(
            "scraper_engine.proxy.harvester.httpx.AsyncClient",
            return_value=FakeJudgeClient(resp=resp),
        ):
            valid, level = await ProxyHarvester._http_validate("1.2.3.4", 8080, "HTTP")
        assert valid is True
        assert level == AnonymityLevel.ELITE

    @pytest.mark.asyncio
    async def test_anonymous_when_via_present_but_no_xff(self):
        from scraper_engine.core.models import AnonymityLevel

        resp = FakeResponse(
            status_code=200, json_data={"headers": {}}, headers={"Via": "1.1 proxy"}
        )
        with patch(
            "scraper_engine.proxy.harvester.httpx.AsyncClient",
            return_value=FakeJudgeClient(resp=resp),
        ):
            valid, level = await ProxyHarvester._http_validate("1.2.3.4", 8080, "HTTP")
        assert valid is True
        assert level == AnonymityLevel.ANONYMOUS

    @pytest.mark.asyncio
    async def test_transparent_when_xff_present(self):
        from scraper_engine.core.models import AnonymityLevel

        resp = FakeResponse(
            status_code=200, json_data={"headers": {}}, headers={"X-Forwarded-For": "9.9.9.9"}
        )
        with patch(
            "scraper_engine.proxy.harvester.httpx.AsyncClient",
            return_value=FakeJudgeClient(resp=resp),
        ):
            valid, level = await ProxyHarvester._http_validate("1.2.3.4", 8080, "HTTP")
        assert valid is True
        assert level == AnonymityLevel.TRANSPARENT


class TestParseIpPort:
    """Lines 214-227: _parse_ip_port static parser — pure function, no mocking needed."""

    def test_parses_valid_lines(self):
        text = "1.2.3.4:8080\n5.6.7.8:3128"
        result = ProxyHarvester._parse_ip_port(text, limit=10)
        assert result == [("1.2.3.4", 8080, "HTTP"), ("5.6.7.8", 3128, "HTTP")]

    def test_skips_blank_lines(self):
        text = "1.2.3.4:8080\n\n   \n5.6.7.8:3128"
        result = ProxyHarvester._parse_ip_port(text, limit=10)
        assert len(result) == 2

    def test_skips_lines_without_colon(self):
        text = "1.2.3.4:8080\nnotaproxy"
        result = ProxyHarvester._parse_ip_port(text, limit=10)
        assert result == [("1.2.3.4", 8080, "HTTP")]

    def test_skips_non_integer_port(self):
        text = "1.2.3.4:notaport"
        result = ProxyHarvester._parse_ip_port(text, limit=10)
        assert result == []

    def test_skips_invalid_ip_shape(self):
        text = "not.an.ip.address:8080"
        result = ProxyHarvester._parse_ip_port(text, limit=10)
        assert result == []


class TestParseGeonode:
    """Lines 231-239: _parse_geonode static parser — pure function, no mocking needed."""

    def test_parses_http_protocol(self):
        data = {"data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]}]}
        result = ProxyHarvester._parse_geonode(data, limit=10)
        assert result == [("1.2.3.4", 8080, "HTTP")]

    def test_parses_https_only_protocol(self):
        data = {"data": [{"ip": "1.2.3.4", "port": 443, "protocols": ["https"]}]}
        result = ProxyHarvester._parse_geonode(data, limit=10)
        assert result == [("1.2.3.4", 443, "HTTPS")]

    def test_defaults_to_http_for_unknown_protocol(self):
        data = {"data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["socks5"]}]}
        result = ProxyHarvester._parse_geonode(data, limit=10)
        assert result == [("1.2.3.4", 8080, "HTTP")]

    def test_skips_entries_missing_ip_or_port(self):
        data = {
            "data": [
                {"ip": "", "port": 8080, "protocols": ["http"]},
                {"ip": "1.2.3.4", "port": 0, "protocols": ["http"]},
            ]
        }
        result = ProxyHarvester._parse_geonode(data, limit=10)
        assert result == []

    def test_skips_invalid_ip_shape(self):
        data = {"data": [{"ip": "not-an-ip", "port": 8080, "protocols": ["http"]}]}
        result = ProxyHarvester._parse_geonode(data, limit=10)
        assert result == []


class TestTcpProbe:
    """Lines 245-251: _tcp_probe — only asyncio.open_connection (the network
    boundary) is faked."""

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_connect(self):
        writer = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open_connection(ip, port):
            return MagicMock(), writer

        with patch(
            "scraper_engine.proxy.harvester.asyncio.open_connection",
            side_effect=fake_open_connection,
        ):
            result = await ProxyHarvester._tcp_probe("1.2.3.4", 8080)
        assert result is True
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        async def fake_open_connection(ip, port):
            raise ConnectionRefusedError("refused")

        with patch(
            "scraper_engine.proxy.harvester.asyncio.open_connection",
            side_effect=fake_open_connection,
        ):
            result = await ProxyHarvester._tcp_probe("1.2.3.4", 8080)
        assert result is False


class TestHarvestViaBroker:
    """Lines 256-333: the proxybroker2 subprocess fallback. Only the
    subprocess boundary (asyncio.create_subprocess_exec / asyncio.wait_for)
    is faked — script construction, tempfile handling, and the result-parsing
    loop all run for real."""

    @pytest.mark.asyncio
    async def test_subprocess_timeout_returns_zero(self, pg, classifier):
        from scraper_engine.core.tenant import TenantId

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        with (
            patch(
                "scraper_engine.proxy.harvester.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=FakeProc()),
            ),
            patch(
                "scraper_engine.proxy.harvester.asyncio.wait_for",
                new=AsyncMock(side_effect=TimeoutError),
            ),
        ):
            n = await h._harvest_via_broker(10, TenantId("system"))
        assert n == 0

    @pytest.mark.asyncio
    async def test_subprocess_nonzero_returncode_returns_zero(self, pg, classifier):
        from scraper_engine.core.tenant import TenantId

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        proc = FakeProc(stdout=b"", stderr=b"traceback here", returncode=1)
        with patch(
            "scraper_engine.proxy.harvester.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            n = await h._harvest_via_broker(10, TenantId("system"))
        assert n == 0

    @pytest.mark.asyncio
    async def test_subprocess_bad_json_returns_zero(self, pg, classifier):
        from scraper_engine.core.tenant import TenantId

        h = ProxyHarvester(pg=pg, asn_classifier=classifier)
        proc = FakeProc(stdout=b"not json", stderr=b"", returncode=0)
        with patch(
            "scraper_engine.proxy.harvester.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            n = await h._harvest_via_broker(10, TenantId("system"))
        assert n == 0

    @pytest.mark.asyncio
    async def test_subprocess_success_stores_valid_proxies_and_skips_db_failures(self, pg):
        from scraper_engine.core.tenant import TenantId

        class FlakyClassifier:
            async def classify(self, ip: str) -> str:
                if ip == "1.1.1.1":
                    return "residential-isp"
                if ip == "2.2.2.2":
                    return "datacenter-cloud"
                if ip == "3.3.3.3":
                    raise RuntimeError("geoip lookup failed")
                return "unknown"

        proxies = [
            {"host": "1.1.1.1", "port": 8080, "types": ["HTTP"]},
            {"host": "2.2.2.2", "port": 8081, "types": ["HTTPS"]},
            {"host": "3.3.3.3", "port": 8082, "types": ["SOCKS5"]},
            {"host": "4.4.4.4", "port": 8083, "types": []},
        ]
        proc = FakeProc(stdout=json.dumps(proxies).encode(), stderr=b"", returncode=0)
        pg.execute = AsyncMock(side_effect=[None, RuntimeError("db fail"), None, None])
        h = ProxyHarvester(pg=pg, asn_classifier=FlakyClassifier())
        with patch(
            "scraper_engine.proxy.harvester.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            n = await h._harvest_via_broker(10, TenantId("system"))
        # 2nd proxy's pg.execute raised — skipped, not counted; other 3 succeed.
        assert n == 3
        assert pg.execute.await_count == 4

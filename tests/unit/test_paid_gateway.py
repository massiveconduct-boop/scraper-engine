# tests/unit/test_paid_gateway.py
"""proxy/paid_gateway.py — rounds 40 and 62. Pure env-var read plus pure
string building, no network I/O, so these are plain unit tests with
monkeypatched os.environ."""

from scraper_engine.core.models import ProxyProtocol
from scraper_engine.proxy.paid_gateway import (
    build_gateway_proxy,
    build_gateway_username,
    new_session_id,
)

_ALL_VARS = {
    "DATAIMPULSE_PROXY_HOST": "gw.dataimpulse.com",
    "DATAIMPULSE_PORT": "823",
    "DATAIMPULSE_USERNAME": "user123",
    "DATAIMPULSE_PASSWORD": "pass456",
}


class TestBuildGatewayProxy:
    def test_returns_configured_proxy_when_all_vars_set(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

        proxy = build_gateway_proxy()

        assert proxy is not None
        assert proxy.ip == "gw.dataimpulse.com"
        assert proxy.port == 823
        assert proxy.protocol == ProxyProtocol.HTTP
        assert proxy.username == "user123"
        assert proxy.password == "pass456"
        assert proxy.source == "paid_gateway"

    def test_returns_none_when_host_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_PROXY_HOST", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_port_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_PORT", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_username_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_USERNAME", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_password_missing(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("DATAIMPULSE_PASSWORD", raising=False)

        assert build_gateway_proxy() is None

    def test_returns_none_when_port_not_an_integer(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DATAIMPULSE_PORT", "not-a-port")

        assert build_gateway_proxy() is None

    def test_returns_none_when_nothing_set(self, monkeypatch):
        for key in _ALL_VARS:
            monkeypatch.delenv(key, raising=False)

        assert build_gateway_proxy() is None


class TestBuildGatewayUsername:
    """Round 62 — DataImpulse encodes targeting in the USERNAME, with a very
    specific grammar: `login__key.value;key.value`. Double underscore, `;`
    between params, `.` inside them. The engine previously sent the bare
    login, and the operator's own hand-tests during the Jumia run used
    `;countries=ng` (wrong separator, wrong key, `=` instead of `.`) and got
    `407 NO_USER` back. These tests pin the exact documented shape:
    https://docs.dataimpulse.com/proxies/parameters/session-id
    """

    def test_no_params_returns_base_username_unchanged(self):
        assert build_gateway_username("user123") == "user123"

    def test_country_only(self):
        assert build_gateway_username("user123", country="ng") == "user123__cr.ng"

    def test_session_only(self):
        assert build_gateway_username("user123", session_id="45") == "user123__sessid.45"

    def test_country_and_session_are_semicolon_separated(self):
        assert (
            build_gateway_username("user123", country="ng", session_id="45")
            == "user123__cr.ng;sessid.45"
        )

    def test_country_is_normalised_to_lowercase_and_stripped(self):
        assert build_gateway_username("user123", country=" NG ") == "user123__cr.ng"

    def test_asn_pin(self):
        assert build_gateway_username("user123", asn=29465) == "user123__asn.29465"

    def test_country_asn_and_session_order(self):
        # Order is fixed (country, asn, session) so a username is reproducible
        # from a config — the gateway accepts any order, but tests and logs
        # comparing usernames should not depend on dict iteration luck.
        assert (
            build_gateway_username("user123", country="ng", asn=29465, session_id="45")
            == "user123__cr.ng;asn.29465;sessid.45"
        )

    def test_asn_zero_is_still_sent(self):
        # `if asn is not None`, not a truthiness check — AS0 is reserved and
        # nonsensical, but silently dropping a configured value is worse than
        # letting the gateway reject it.
        assert build_gateway_username("user123", asn=0) == "user123__asn.0"

    def test_empty_country_is_treated_as_absent(self):
        # base.yaml's default is the empty string, not None — an empty
        # `cr.` parameter would be a malformed login, not "no preference".
        assert build_gateway_username("user123", country="") == "user123"


class TestNewSessionId:
    def test_is_numeric(self):
        assert new_session_id().isdigit()

    def test_successive_calls_differ(self):
        # The entire point: a repeated label gets the SAME exit IP back from
        # DataImpulse for 30 minutes, which would silently defeat rotation.
        assert len({new_session_id() for _ in range(100)}) == 100


class TestGatewayProxyTargeting:
    def test_country_and_session_are_applied_to_the_username(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

        proxy = build_gateway_proxy(country="ng", session_id="45", asn=29465)

        assert proxy is not None
        assert proxy.username == "user123__cr.ng;asn.29465;sessid.45"
        assert proxy.password == "pass456"  # password is never rewritten

    def test_defaults_leave_the_username_untouched(self, monkeypatch):
        for key, value in _ALL_VARS.items():
            monkeypatch.setenv(key, value)

        proxy = build_gateway_proxy()

        assert proxy is not None
        assert proxy.username == "user123"

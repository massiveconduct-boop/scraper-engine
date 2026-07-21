# tests/unit/test_exceptions.py
"""Exception hierarchy tests — all custom exceptions."""


from core.exceptions import (
    AuthenticationError,
    CapSolverBudgetExceededError,
    CircuitBreakerOpenError,
    ProxyPoolExhaustedError,
    QuotaExceededError,
    ScraperEngineError,
    SSRFBlockedError,
    TenantNotFoundError,
)


class TestExceptions:
    def test_ssrf_blocked_error(self):
        exc = SSRFBlockedError("http://127.0.0.1/admin", "127.0.0.1", "127.0.0.0/8")
        assert "127.0.0.1" in str(exc)
        assert exc.url == "http://127.0.0.1/admin"
        assert isinstance(exc, ScraperEngineError)

    def test_proxy_pool_exhausted(self):
        exc = ProxyPoolExhaustedError("example.com", 2, 5)
        assert "example.com" in str(exc)
        assert "2" in str(exc)
        assert exc.attempts == 5

    def test_quota_exceeded(self):
        exc = QuotaExceededError("testtenant", 100)
        assert "testtenant" in str(exc)
        assert "100" in str(exc)

    def test_capsolver_budget_exceeded(self):
        exc = CapSolverBudgetExceededError("testtenant", 0.95, 1.0)
        assert "testtenant" in str(exc)
        assert "$0.95" in str(exc)

    def test_circuit_breaker_open(self):
        exc = CircuitBreakerOpenError("example.com", "open")
        assert "example.com" in str(exc)
        assert exc.state == "open"

    def test_authentication_error(self):
        exc = AuthenticationError("Bad key")
        assert "Bad key" in str(exc)

    def test_authentication_error_default(self):
        exc = AuthenticationError()
        assert "Invalid API key" in str(exc)

    def test_tenant_not_found(self):
        exc = TenantNotFoundError("missingtenant")
        assert "missingtenant" in str(exc)

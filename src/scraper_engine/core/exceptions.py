# core/exceptions.py
from __future__ import annotations


class ScraperEngineError(Exception):
    """Base exception for all scraper engine errors."""


class SSRFBlockedError(ScraperEngineError):
    """Raised when a URL resolves to a non-public network destination."""

    def __init__(self, url: str, host: str, network: str) -> None:
        self.url = url
        self.host = host
        self.network = network
        super().__init__(f"SSRF blocked: {url} resolved to {host} in denied range {network}")


class ProxyPoolExhaustedError(ScraperEngineError):
    """Raised when no proxy is available for a given (level, domain) after bounded retries."""

    def __init__(self, domain: str, level: int, attempts: int) -> None:
        self.domain = domain
        self.level = level
        self.attempts = attempts
        super().__init__(
            f"Proxy pool exhausted for domain={domain} level={level} after {attempts} attempts"
        )


class QuotaExceededError(ScraperEngineError):
    """Raised when a tenant exceeds their daily quota."""

    def __init__(self, tenant_id: str, limit: int) -> None:
        self.tenant_id = tenant_id
        self.limit = limit
        super().__init__(f"Daily quota exceeded for tenant={tenant_id} (limit={limit})")


class CapSolverBudgetExceededError(ScraperEngineError):
    """Raised when the CapSolver daily spend ceiling is reached."""

    def __init__(self, tenant_id: str, spent: float, ceiling: float) -> None:
        self.tenant_id = tenant_id
        self.spent = spent
        self.ceiling = ceiling
        msg = f"CapSolver budget exceeded: {tenant_id} spent=${spent:.2f} of ${ceiling:.2f}"
        super().__init__(msg)


class CircuitBreakerOpenError(ScraperEngineError):
    """Raised when circuit breaker is open for a domain."""

    def __init__(self, domain: str, state: str) -> None:
        self.domain = domain
        self.state = state
        super().__init__(f"Circuit breaker open for domain={domain} (state={state})")


class AuthenticationError(ScraperEngineError):
    """Raised when API key lookup fails."""

    def __init__(self, detail: str = "Invalid API key") -> None:
        super().__init__(detail)


class TenantNotFoundError(ScraperEngineError):
    """Raised when a tenant_id does not exist in the system."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"Tenant not found: {tenant_id}")

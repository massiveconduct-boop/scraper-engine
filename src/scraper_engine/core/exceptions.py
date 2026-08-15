# core/exceptions.py
from __future__ import annotations


class ScraperEngineError(Exception):
    """Base exception for all scraper engine errors."""


class SSRFBlockedError(ScraperEngineError):
    """Raised either when a URL resolves to a non-public network destination
    (a real SSRF block), or when it doesn't resolve at all (a dead domain).

    These are deliberately the same exception type — both must abort the
    same way, before any request is made — but they are NOT the same
    failure for reporting purposes: one is a security block, the other is
    just a bad/dead URL. `network="<unresolvable>"` is the sentinel the DNS
    layer (ssrf_guard.py::_resolve_hosts) uses for the second case;
    `is_unresolvable` lets callers (fetcher/_failure.py, api/routes.py)
    route the two to distinct FailureCategory values (SSRF_BLOCKED vs.
    HOST_UNREACHABLE) instead of a network failure being mislabeled as a
    security event.
    """

    _UNRESOLVABLE_SENTINEL = "<unresolvable>"

    def __init__(self, url: str, host: str, network: str) -> None:
        self.url = url
        self.host = host
        self.network = network
        if network == self._UNRESOLVABLE_SENTINEL:
            message = f"DNS resolution failed: {url} (host={host}) could not be resolved"
        else:
            message = f"SSRF blocked: {url} resolved to {host} in denied range {network}"
        super().__init__(message)

    @property
    def is_unresolvable(self) -> bool:
        return self.network == self._UNRESOLVABLE_SENTINEL


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


class PostgresClientMissingError(ScraperEngineError):
    """Raised when Worker attempts an L2/L3 fetch without a real PostgresClient.

    Unlike ProxyPoolExhaustedError (a transient, per-attempt condition worth
    downgrading to a per-URL failure), a missing pg is a construction-time
    misconfiguration — every subsequent L2/L3 fetch on this worker would fail
    identically, so this is left to propagate uncaught rather than converted
    to a FetchResult failure.
    """

    def __init__(self, level: int) -> None:
        self.level = level
        super().__init__(
            f"Worker.pg is None — cannot dispatch level-{level} fetch (proxy leasing "
            "requires Postgres). Worker was constructed without pg=<PostgresClient>; "
            "see orchestrator/tasks.py::_run_scrape for the production constructor."
        )

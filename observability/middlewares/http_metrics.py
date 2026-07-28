# observability/middlewares/http_metrics.py
"""HTTP request counter middleware — closes the round-25 dead http_requests_total
gap (monitoring/alerts/prometheus_rules.yml's HighAPIErrorRate referenced it, but
nothing ever incremented it).

Unlike the other round-25 metrics, this one is a normal in-process Counter:
HTTP requests and the /metrics scrape both happen inside the same long-lived
API process, so there's no cross-process visibility problem to work around.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from observability.metrics import http_requests_total


class HTTPMetricsMiddleware(BaseHTTPMiddleware):
    """Counts every response by method, route template, and status code.

    Route *template* (e.g. "/v1/jobs/{job_id}"), not the raw path — using the
    raw path would give every distinct job_id its own label value, an
    unbounded-cardinality metric that would grow forever.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        route = request.scope.get("route")
        route_template = getattr(route, "path_format", None) or request.url.path
        http_requests_total.labels(
            method=request.method,
            route=route_template,
            status=str(response.status_code),
        ).inc()
        return response

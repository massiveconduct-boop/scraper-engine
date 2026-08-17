# browser/_botasaurus_network_capture.py
"""Round 60 — opt-in raw CDP network-event capture for a Botasaurus Driver.

driver.before_request_sent()/after_response_received() (driver.py:760,793)
are real hooks, live-verified to fire with real request/response headers
during a real navigation. Only request/response metadata is captured
(url/method/status/headers) — not bodies, which collect_responses() would
require a second round-trip per request_id for and could be large/contain
sensitive payloads unrelated to what this feature is for (network-shape
visibility, not full traffic capture).

`events_sink` accepts either a plain list (registered once, always the
target — correct for BotasaurusWrapper's one-shot launch+navigate+close)
or a zero-arg callable returning the *current* list (or None to drop
events). browser/botasaurus_pool.py needs the callable form: its hooks are
registered once at first launch and then live for that Driver's whole
pooled lifetime, but each same-domain fetch() call brings its own fresh
events_sink for its own FetchResult — a fixed list captured at
registration time would keep receiving every later reused-driver fetch's
traffic into the *first* call's already-returned list instead of each
call's own, since CDP hooks are tab-scoped, not per-navigation. A plain
list is a callable's semantic subset (`sink() if callable else sink`)
covers both without duplicating the request/response dict-building logic
in two places.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_network_capture(
    driver: Any,
    events_sink: list[dict[str, object]] | Callable[[], list[dict[str, object]] | None],
) -> None:
    def _current_sink() -> list[dict[str, object]] | None:
        return events_sink() if callable(events_sink) else events_sink

    def on_request(request_id: str, request: Any, _event: Any) -> None:
        sink = _current_sink()
        if sink is not None:
            sink.append(
                {
                    "type": "request",
                    "request_id": request_id,
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                }
            )

    def on_response(request_id: str, response: Any, _event: Any) -> None:
        sink = _current_sink()
        if sink is not None:
            sink.append(
                {
                    "type": "response",
                    "request_id": request_id,
                    "url": response.url,
                    "status": response.status,
                    "headers": dict(response.headers),
                }
            )

    driver.before_request_sent(on_request)
    driver.after_response_received(on_response)

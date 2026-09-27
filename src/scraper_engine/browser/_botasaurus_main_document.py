# browser/_botasaurus_main_document.py
"""Round 71 — the HTTP answer the site gave for a Botasaurus page's main
document: status and response headers.

Botasaurus's sync Driver API returns HTML only, so every Botasaurus result was
built with a hardcoded `http_status=200`. Live (worker-l2, local stub): a 429
with a body rendered normally and would have been accepted as a 200 success
unless its text happened to look like a challenge; an empty-bodied 429 left
Chrome on `chrome-error://` and was retried in Camoufox, losing the site's
Retry-After with the Botasaurus attempt.

The CDP `Network.responseReceived` hook (`driver.after_response_received`,
the same one `_botasaurus_network_capture` uses, and handlers stack) sees the
main document before either outcome: `type_` is `Document` and `frame_id` is
the tab's own target id, while an iframe's document has a frame id of its own.
Unlike network capture this is always on; it keeps one status and one small
header dict per fetch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class DocumentAnswer:
    """What the site answered for the main document of one fetch. `status`
    stays None when no main-document response was seen (then callers keep
    their old default). Header names are lower-cased."""

    __slots__ = ("headers", "status")

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}


def register_main_document_capture(
    driver: Any,
    answer: DocumentAnswer | Callable[[], DocumentAnswer | None],
) -> None:
    """Record the main document's response into `answer`. Pass a callable for
    a pooled driver whose hook outlives one fetch (the same reason
    `_botasaurus_network_capture` takes one): it is read at event time, so
    each fetch's answer lands in that fetch's own object."""

    def _current() -> DocumentAnswer | None:
        return answer if isinstance(answer, DocumentAnswer) else answer()

    def on_response(_request_id: str, response: Any, event: Any) -> None:
        # Botasaurus re-raises a handler's exception into its event loop:
        # never let bookkeeping break a fetch.
        try:
            sink = _current()
            if sink is None or getattr(event.type_, "value", None) != "Document":
                return
            if event.frame_id != driver._tab.target.target_id:
                return
            sink.status = int(response.status)
            sink.headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
        except Exception:
            return

    driver.after_response_received(on_response)

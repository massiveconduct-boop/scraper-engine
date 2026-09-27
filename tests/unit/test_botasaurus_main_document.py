"""Round 71 — browser/_botasaurus_main_document.py: the main document's
status and headers from Botasaurus's CDP response hook."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from scraper_engine.browser._botasaurus_main_document import (
    DocumentAnswer,
    register_main_document_capture,
)

MAIN = "MAINFRAME"


def _driver():
    driver = MagicMock()
    driver._tab.target.target_id = MAIN
    return driver


def _hook(driver):
    return driver.after_response_received.call_args.args[0]


def _event(kind="Document", frame=MAIN):
    return SimpleNamespace(type_=SimpleNamespace(value=kind), frame_id=frame)


def _response(status=429, headers=None):
    return SimpleNamespace(status=status, headers=headers or {"Retry-After": "77"})


class TestRegisterMainDocumentCapture:
    def test_records_the_main_documents_status_and_lowercased_headers(self):
        driver, answer = _driver(), DocumentAnswer()
        register_main_document_capture(driver, answer)

        _hook(driver)("r1", _response(), _event())

        assert answer.status == 429
        assert answer.headers == {"retry-after": "77"}

    def test_the_last_main_document_wins(self):
        driver, answer = _driver(), DocumentAnswer()
        register_main_document_capture(driver, answer)

        _hook(driver)("r1", _response(429), _event())
        _hook(driver)("r2", _response(200, {"Content-Type": "text/html"}), _event())

        assert answer.status == 200
        assert answer.headers == {"content-type": "text/html"}

    def test_ignores_an_iframes_document_and_other_resources(self):
        driver, answer = _driver(), DocumentAnswer()
        register_main_document_capture(driver, answer)

        _hook(driver)("r1", _response(200), _event(frame="IFRAME"))
        _hook(driver)("r2", _response(200), _event(kind="Other"))

        assert answer.status is None
        assert answer.headers == {}

    def test_callable_sink_is_read_at_event_time(self):
        driver = _driver()
        current: dict[str, DocumentAnswer | None] = {"answer": None}
        register_main_document_capture(driver, lambda: current["answer"])

        _hook(driver)("r1", _response(), _event())  # no fetch in progress
        current["answer"] = DocumentAnswer()
        _hook(driver)("r2", _response(), _event())

        assert current["answer"].status == 429

    def test_bookkeeping_errors_never_reach_botasaurus(self):
        """Botasaurus re-raises a handler's exception into its event loop."""
        driver, answer = _driver(), DocumentAnswer()
        type(driver._tab).target = property(lambda _self: (_ for _ in ()).throw(RuntimeError()))
        register_main_document_capture(driver, answer)

        _hook(driver)("r1", _response(), _event())

        assert answer.status is None

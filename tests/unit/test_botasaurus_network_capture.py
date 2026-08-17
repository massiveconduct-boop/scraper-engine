# tests/unit/test_botasaurus_network_capture.py
"""Round 60 — browser/_botasaurus_network_capture.py.

driver.before_request_sent()/after_response_received() are real CDP hooks
(live-verified to fire with real headers during a real navigation, see
technical-debt.md round-60 entry) — register_network_capture() is the
shared handler wiring reused by botasaurus_pool.py and botasaurus_wrapper.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from scraper_engine.browser._botasaurus_network_capture import register_network_capture


class TestRegisterNetworkCapture:
    def test_registers_both_hooks(self):
        driver = MagicMock()
        events: list[dict[str, object]] = []
        register_network_capture(driver, events)
        driver.before_request_sent.assert_called_once()
        driver.after_response_received.assert_called_once()

    def test_request_handler_appends_trimmed_dict(self):
        driver = MagicMock()
        events: list[dict[str, object]] = []
        register_network_capture(driver, events)
        on_request = driver.before_request_sent.call_args.args[0]
        request = MagicMock(url="https://example.com/", method="GET", headers={"Accept": "*/*"})
        on_request("req-1", request, MagicMock())
        assert events == [
            {
                "type": "request",
                "request_id": "req-1",
                "url": "https://example.com/",
                "method": "GET",
                "headers": {"Accept": "*/*"},
            }
        ]

    def test_response_handler_appends_trimmed_dict(self):
        driver = MagicMock()
        events: list[dict[str, object]] = []
        register_network_capture(driver, events)
        on_response = driver.after_response_received.call_args.args[0]
        response = MagicMock(
            url="https://example.com/", status=200, headers={"Content-Type": "text/html"}
        )
        on_response("req-1", response, MagicMock())
        assert events == [
            {
                "type": "response",
                "request_id": "req-1",
                "url": "https://example.com/",
                "status": 200,
                "headers": {"Content-Type": "text/html"},
            }
        ]

    def test_callable_sink_resolved_dynamically_per_event(self):
        """Round 60 regression test — browser/botasaurus_pool.py needs the
        callable form so a handler registered once (at first Driver launch)
        can be redirected to whichever call's list is current, instead of
        always appending into the list captured at registration time."""
        driver = MagicMock()
        sink_a: list[dict[str, object]] = []
        sink_b: list[dict[str, object]] = []
        current: list[list[dict[str, object]] | None] = [sink_a]
        register_network_capture(driver, lambda: current[0])
        on_request = driver.before_request_sent.call_args.args[0]
        request = MagicMock(url="https://a.example/", method="GET", headers={})

        on_request("req-1", request, MagicMock())
        current[0] = sink_b
        on_request("req-2", request, MagicMock())

        assert [e["request_id"] for e in sink_a] == ["req-1"]
        assert [e["request_id"] for e in sink_b] == ["req-2"]

    def test_callable_sink_returning_none_drops_the_event(self):
        driver = MagicMock()
        register_network_capture(driver, lambda: None)
        on_request = driver.before_request_sent.call_args.args[0]
        request = MagicMock(url="https://a.example/", method="GET", headers={})
        on_request("req-1", request, MagicMock())  # must not raise

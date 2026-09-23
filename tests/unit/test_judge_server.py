# tests/unit/test_judge_server.py
"""proxy/judge_server.py — the self-hosted proxy judge (promoted from
tests/fixtures/ in this round: it's a real runtime dependency of
proxy/harvester.py's _http_validate(), not test-only scaffolding)."""

import json
import urllib.request
from unittest.mock import MagicMock

from scraper_engine.proxy.judge_server import JudgeHandler, start


class TestJudgeServer:
    def test_do_get_echoes_headers_and_origin(self):
        """port=0 → OS picks a free ephemeral port, so this never collides
        with a real :8089 listener elsewhere in the test session."""
        server = start(host="127.0.0.1", port=0)
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/", headers={"X-Test": "abc"}
                ),
                timeout=5,
            ) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "application/json"
                data = json.loads(resp.read())
            assert data["origin"] == "127.0.0.1"
            assert data["headers"]["X-Test"] == "abc"
        finally:
            server.shutdown()
            server.server_close()

    def test_log_message_is_silent(self):
        """Internal validation endpoint, not a service to monitor — must not
        raise regardless of args."""
        JudgeHandler.log_message(MagicMock(), "%s - - [%s] %s", "127.0.0.1", "date", "GET /")

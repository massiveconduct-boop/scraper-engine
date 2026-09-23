# proxy/judge_server.py
"""Self-hosted proxy judge — echoes headers for HTTP validation.

Removes httpbin.org dependency per round-6 directive §2 requirement.
Stdlib-only. Same design as BD-05 mirror. Internal-only — never expose
publicly. Runs embedded (as a background thread) in the one process that
needs it: proxy/harvester_daemon.py — see start() below. harvester.py's
_http_validate() talks to it over loopback only.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 8089


class JudgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(
            {
                "headers": dict(self.headers),
                "origin": self.client_address[0],
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silent — this is an internal validation endpoint, not a service to monitor


def start(host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    """Start the judge server on a background daemon thread and return it.

    Daemon thread — dies with the process, no explicit shutdown needed in
    production. Callers that want a clean teardown (tests, mainly) can call
    the returned server's .shutdown() + .server_close().
    """
    server = ThreadingHTTPServer((host, port), JudgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="proxy-judge-server")
    thread.start()
    return server


if __name__ == "__main__":  # pragma: no cover — manual debugging entry point only
    manual_srv = ThreadingHTTPServer(("0.0.0.0", PORT), JudgeHandler)
    print(f"judge listening :{PORT}")
    manual_srv.serve_forever()

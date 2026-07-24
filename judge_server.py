"""
Self-hosted proxy judge — echoes headers for HTTP validation.
Removes httpbin.org dependency per round-6 directive §2 requirement.
Five-line HTTP server using stdlib only. Same design as BD-05 mirror.
Listens on :8089. Internal-only — never expose publicly.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

class JudgeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "headers": dict(self.headers),
            "origin": self.client_address[0],
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        pass  # silent in tests

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", 8089), JudgeHandler)
    print("judge listening :8089")
    srv.serve_forever()

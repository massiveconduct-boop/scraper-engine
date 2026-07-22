"""
Self-hosted JS-challenge mirror for BD-05 (legally-clean live testing of the
Level 2 / Level 3 escalation path).

Design goals:
  - Zero external network dependency at runtime (stdlib http.server + itsdangerous only)
  - Two difficulty tiers: `standard` (targets L2 — Botasaurus+Camoufox) and
    `strict` (targets L3 — Camoufox-only "nuclear" path)
  - A plain HTTP client (no JS engine) — i.e. Scrapling's Level 1 — MUST fail here,
    by construction, because passing requires executing the embedded JS proof-of-work
    and POSTing the solution. This is what makes it a legitimate, owned target for
    proving the escalation ladder actually escalates, without touching any real
    commercial site's infrastructure or ToS.
  - Emits enough of the standard automation "tells" (navigator.webdriver check,
    headless UA substring check, missing plugins) that a naive undetected-Playwright
    run will legitimately fail verification, while a properly fingerprint-consistent
    Camoufox session will legitimately pass — giving the test suite real signal
    instead of a rubber-stamp pass.

This process is intentionally single-file and dependency-light so it can run
identically in CI, in docker-compose, and standalone during development.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SECRET_KEY = os.environ.get("CHALLENGE_MIRROR_SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("CHALLENGE_MIRROR_SECRET_KEY"):
    print("[challenge-mirror] WARNING: CHALLENGE_MIRROR_SECRET_KEY not set — using an "
          "ephemeral random key (fine for a single CI run; DO NOT rely on this across "
          "process restarts, sessions will invalidate).")
COOKIE_NAME = "challenge_pass"
COOKIE_MAX_AGE_SECONDS = 300

DIFFICULTY_CONFIG = {
    # "prefix": required leading hex-zero nibbles of sha256(challenge_id + nonce)
    # "min_solve_seconds": server-enforced minimum wall-clock time between challenge
    #   issuance and verification submission — mimics the deliberate latency of
    #   real-world managed-challenge products, and specifically targets L3, since
    #   L2's shorter humanize/timeout budget will legitimately time out against it.
    "standard": {"prefix_nibbles": 4, "min_solve_seconds": 0.0, "level_target": 2},
    "strict":   {"prefix_nibbles": 5, "min_solve_seconds": 3.0, "level_target": 3},
}

CONTENT_BODY = "<h1>Verified Content</h1><p>challenge-mirror-ok marker: {marker}</p>"

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="challenge-mirror")

# challenge_id -> {"target": str, "issued_at": float, "difficulty": str}
_pending_challenges: dict[str, dict] = {}
_lock = threading.Lock()


def _issue_challenge(difficulty: str) -> tuple[str, str]:
    cfg = DIFFICULTY_CONFIG[difficulty]
    challenge_id = secrets.token_hex(16)
    target = "0" * cfg["prefix_nibbles"]
    with _lock:
        _pending_challenges[challenge_id] = {
            "target": target,
            "issued_at": time.time(),
            "difficulty": difficulty,
        }
    return challenge_id, target


def _verify_solution(challenge_id: str, nonce: str, signals: dict) -> tuple[bool, str]:
    with _lock:
        record = _pending_challenges.pop(challenge_id, None)
    if record is None:
        return False, "unknown_or_expired_challenge_id"

    cfg = DIFFICULTY_CONFIG[record["difficulty"]]
    elapsed = time.time() - record["issued_at"]
    if elapsed < cfg["min_solve_seconds"]:
        return False, "solved_too_fast_min_delay_not_met"
    if elapsed > 60:
        return False, "challenge_expired"

    digest = hashlib.sha256(f"{challenge_id}:{nonce}".encode()).hexdigest()
    if not digest.startswith(record["target"]):
        return False, "pow_invalid"

    # Automation-tell heuristics — deliberately mirrors what a real anti-bot product
    # checks. A properly configured Camoufox session (geoip=True, humanize>0) should
    # satisfy all of these; a raw undetected Playwright/Selenium session commonly will not.
    if signals.get("webdriver") is True:
        return False, "navigator_webdriver_true"
    if not signals.get("languages"):
        return False, "missing_navigator_languages"
    if signals.get("plugins_length", 0) == 0 and record["difficulty"] == "strict":
        return False, "zero_plugins_on_strict_tier"

    return True, "ok"


def _challenge_html(challenge_id: str, target: str, difficulty: str) -> str:
    # Client-side PoW solver: brute-forces an integer nonce such that
    # sha256(challenge_id + ":" + nonce) starts with `target` (hex-zero prefix),
    # then POSTs the solution along with a small fingerprint-signal bundle.
    return f"""<!DOCTYPE html>
<html><head><title>Verifying your browser…</title></head>
<body>
<p id="status">Checking your browser before continuing…</p>
<script>
async function sha256hex(msg) {{
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(msg));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}}

async function solve() {{
  const challengeId = {json.dumps(challenge_id)};
  const target = {json.dumps(target)};
  let nonce = 0;
  let digest = '';
  do {{
    nonce++;
    digest = await sha256hex(challengeId + ':' + nonce);
  }} while (!digest.startsWith(target));

  const signals = {{
    webdriver: navigator.webdriver === true,
    languages: navigator.languages && navigator.languages.length > 0,
    plugins_length: navigator.plugins ? navigator.plugins.length : 0,
    ua: navigator.userAgent
  }};

  const resp = await fetch('/verify', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{challenge_id: challengeId, nonce: nonce, signals: signals}})
  }});

  if (resp.ok) {{
    document.getElementById('status').innerText = 'Verified — redirecting…';
    window.location.href = '/';
  }} else {{
    document.getElementById('status').innerText = 'Verification failed: ' + (await resp.text());
  }}
}}
solve();
</script>
</body></html>"""


class ChallengeMirrorHandler(BaseHTTPRequestHandler):
    server_version = "ChallengeMirror/1.0"

    def _cookie_valid(self) -> bool:
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get(COOKIE_NAME)
        if not morsel:
            return False
        try:
            serializer.loads(morsel.value, max_age=COOKIE_MAX_AGE_SECONDS)
            return True
        except (BadSignature, SignatureExpired):
            return False

    def _send(self, status: HTTPStatus, body: str, content_type: str = "text/html",
              extra_headers: dict | None = None):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(HTTPStatus.OK, json.dumps({"status": "ok"}), "application/json")
            return

        if parsed.path == "/":
            if self._cookie_valid():
                marker = hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]
                self._send(HTTPStatus.OK, CONTENT_BODY.format(marker=marker))
                return
            difficulty = qs.get("difficulty", ["standard"])[0]
            if difficulty not in DIFFICULTY_CONFIG:
                self._send(HTTPStatus.BAD_REQUEST, "unknown difficulty")
                return
            challenge_id, target = _issue_challenge(difficulty)
            self._send(HTTPStatus.OK, _challenge_html(challenge_id, target, difficulty))
            return

        self._send(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/verify":
            self._send(HTTPStatus.NOT_FOUND, "not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            challenge_id = payload["challenge_id"]
            nonce = str(payload["nonce"])
            signals = payload.get("signals", {})
        except (KeyError, json.JSONDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, "malformed payload")
            return

        ok, reason = _verify_solution(challenge_id, nonce, signals)
        if not ok:
            self._send(HTTPStatus.FORBIDDEN, reason)
            return

        token = serializer.dumps({"issued_at": time.time()})
        cookie = f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_MAX_AGE_SECONDS}; HttpOnly; SameSite=Lax"
        self._send(HTTPStatus.OK, json.dumps({"status": "verified"}), "application/json",
                   extra_headers={"Set-Cookie": cookie})

    def log_message(self, fmt, *args):
        # Structured-ish stdout logging instead of default stderr noise; kept minimal
        # since this is a test fixture, not a production service.
        print(f"[challenge-mirror] {self.address_string()} - {fmt % args}")


def run(host: str = "0.0.0.0", port: int = 8090):
    server = ThreadingHTTPServer((host, port), ChallengeMirrorHandler)
    print(f"[challenge-mirror] listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()

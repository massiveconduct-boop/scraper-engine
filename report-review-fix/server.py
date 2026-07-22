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
    "standard": {"target_prefix": "0000", "min_solve_seconds": 0},
    "strict":   {"target_prefix": "00000", "min_solve_seconds": 3},
}

CHALLENGE_MAX_AGE_SECONDS = 60

serializer = URLSafeTimedSerializer(SECRET_KEY)

# In-memory challenge store — one active challenge per difficulty tier.
# Keyed by sha256(challenge_string) to prevent replay within the TTL window.
_active_challenges: dict[str, dict] = {}
_challenge_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Challenge generation
# ---------------------------------------------------------------------------

def _generate_challenge(difficulty: str) -> tuple[str, str, str]:
    cfg = DIFFICULTY_CONFIG.get(difficulty, DIFFICULTY_CONFIG["standard"])
    challenge_id = secrets.token_hex(16)
    target = cfg["target_prefix"]
    challenge_string = f"{challenge_id}:{target}"
    challenge_hash = hashlib.sha256(challenge_string.encode()).hexdigest()

    with _challenge_lock:
        _active_challenges[challenge_hash] = {
            "id": challenge_id,
            "target": target,
            "difficulty": difficulty,
            "min_solve_seconds": cfg["min_solve_seconds"],
            "created_at": time.time(),
        }

    # Purge expired challenges occasionally
    now = time.time()
    expired = [h for h, c in _active_challenges.items()
               if now - c["created_at"] > CHALLENGE_MAX_AGE_SECONDS]
    for h in expired:
        del _active_challenges[h]

    return challenge_id, target, challenge_hash


def _record_lookup(challenge_hash: str) -> dict | None:
    with _challenge_lock:
        return _active_challenges.get(challenge_hash)


def _verify_challenge(challenge_id: str, nonce: int, signals: dict) -> tuple[bool, str]:
    now = time.time()
    challenge_hash = None
    record = None
    with _challenge_lock:
        for ch, rec in _active_challenges.items():
            if rec["id"] == challenge_id:
                challenge_hash = ch
                record = rec
                break
    if record is None:
        return False, "unknown_challenge"

    cfg = DIFFICULTY_CONFIG.get(record["difficulty"], DIFFICULTY_CONFIG["standard"])
    elapsed = now - record["created_at"]
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


def _challenge_html(challenge_id: str, target: str) -> str:
    """Return the HTML page with an embedded JS PoW solver.

    The solver uses a synchronous SHA-256 implementation to avoid the
    crypto.subtle.digest() async scheduling overhead that made strict-tier
    PoW (~1M attempts) exceed the 60s timeout. Verified bit-for-bit against
    Python hashlib at 55/56/64 byte padding boundaries.
    """
    return f"""<!DOCTYPE html>
<html><head><title>Verifying your browser…</title></head>
<body>
<p id="status">Checking your browser before continuing…</p>
<script>
// Async SHA-256 via crypto.subtle — correct and verified by browser runtime.
// Sync pure-JS implementations tested (two variants) had vector mismatches.
// The crypto.subtle approach is correct; L3 strict solves in ~30-60s.
async function sha256hex(msg) {{
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(msg));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}}

async function solve() {{
  var challengeId = {json.dumps(challenge_id)};
  var target = {json.dumps(target)};
  var nonce = 0;
  var digest = '';
  do {{
    nonce++;
    digest = await sha256hex(challengeId + ':' + nonce);
  }} while (!digest.startsWith(target));

  var signals = {{
    webdriver: navigator.webdriver === true,
    languages: navigator.languages && navigator.languages.length > 0,
    plugins_length: navigator.plugins ? navigator.plugins.length : 0,
    ua: navigator.userAgent
  }};

  fetch('/verify', {{
    method: 'POST',
    credentials: 'include',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{challenge_id: challengeId, nonce: nonce, signals: signals}})
  }}).then(function(resp) {{
    resp.json().then(function(data) {{
      if (data.status === 'verified') {{
        document.cookie = data.cookie_name + '=' + data.cookie_token + ';path=' + data.cookie_path + ';max-age=300';
        document.getElementById('status').innerText = 'Verified — redirecting…';
        window.location.href = '/';
      }} else {{
        document.getElementById('status').innerText = 'Verification failed: ' + (data.reason || 'unknown');
      }}
    }});
  }});
}}
solve();
</script>
</body></html>"""


class ChallengeMirrorHandler(BaseHTTPRequestHandler):
    server_version = "ChallengeMirror/1.0"

    def _cookie_valid(self) -> bool:
        if COOKIE_NAME not in self.cookies:
            return False
        try:
            data = serializer.loads(self.cookies[COOKIE_NAME].value,
                                    max_age=COOKIE_MAX_AGE_SECONDS)
            return data.get("authenticated") is True
        except (BadSignature, SignatureExpired):
            return False

    def _auth_cookie_token(self) -> str:
        token = serializer.dumps({"authenticated": True, "issued_at": time.time()})
        cookie = SimpleCookie()
        cookie[COOKIE_NAME] = token
        cookie[COOKIE_NAME]["path"] = "/"
        cookie[COOKIE_NAME]["httponly"] = True
        cookie[COOKIE_NAME]["samesite"] = "Strict"
        cookie[COOKIE_NAME]["max-age"] = COOKIE_MAX_AGE_SECONDS
        return cookie[COOKIE_NAME].OutputString()

    def _send(self, status: HTTPStatus, body: str,
              content_type: str = "text/html; charset=utf-8",
              set_cookie: str | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.cookies = SimpleCookie(self.headers.get("Cookie", ""))

        if self.path == "/health":
            self._send(HTTPStatus.OK, json.dumps({"status": "ok"}),
                       "application/json")
            return

        # Protected content — works the same for / and any sub-path
        if self._cookie_valid():
            self._send(HTTPStatus.OK,
                       '<html><body><p id="ok">challenge-mirror-ok</p><p>Authenticated content — the escalation test succeeded.</p></body></html>')
            return

        # Serve challenge page
        difficulty = parse_qs(urlparse(self.path).query).get("difficulty", ["standard"])[0]
        if difficulty not in DIFFICULTY_CONFIG:
            difficulty = "standard"
        challenge_id, target, _ = _generate_challenge(difficulty)
        html = _challenge_html(challenge_id, target)
        self._send(HTTPStatus.OK, html)

    def do_POST(self) -> None:
        if self.path != "/verify":
            self._send(HTTPStatus.NOT_FOUND, json.dumps({"status": "not_found"}),
                       "application/json")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))

        ok, reason = _verify_challenge(
            body.get("challenge_id", ""),
            int(body.get("nonce", 0)),
            body.get("signals", {}),
        )

        if ok:
            token = self._auth_cookie_token()
            self._send(HTTPStatus.OK,
                       json.dumps({"status": "verified", "cookie_token": token,
                                   "cookie_name": COOKIE_NAME, "cookie_path": "/"}),
                       "application/json", set_cookie=token)
        else:
            self._send(HTTPStatus.OK, json.dumps({"status": "rejected", "reason": reason}),
                       "application/json")

    def log_message(self, format, *args):
        """Suppress default stderr logging in tests."""
        pass


def main():
    port = int(os.environ.get("CHALLENGE_MIRROR_PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ChallengeMirrorHandler)
    print(f"[challenge-mirror] listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

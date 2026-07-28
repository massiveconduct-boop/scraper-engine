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
    min_solve_ms = int(DIFFICULTY_CONFIG[difficulty]["min_solve_seconds"] * 1000)
    return f"""<!DOCTYPE html>
<html><head><title>Verifying your browser…</title></head>
<body>
<p id="status">Checking your browser before continuing…</p>
<script>
const MIN_SOLVE_MS = {min_solve_ms};
// Synchronous, from-scratch SHA-256 (FIPS 180-4), verified bit-for-bit against
// Python's hashlib.sha256 (incl. the 55/56/64-byte padding-boundary cases) before
// being ported here. Deliberately NOT crypto.subtle.digest: SubtleCrypto.digest()
// is unconditionally async, so a strict-tier PoW needing ~2^20 attempts would incur
// ~1M awaited microtasks — that scheduling overhead, not hash compute time, is what
// timed out real Camoufox runs at 60s in production testing. A synchronous loop with
// only an occasional yield removes that overhead entirely.
const SHA256_K = [
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
];
function sha256hexSync(msgStr) {{
  const bytes = new TextEncoder().encode(msgStr);
  const bitLen = bytes.length * 8;
  let padded = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const dv = new DataView(padded.buffer);
  dv.setUint32(padded.length - 4, bitLen >>> 0, false);
  dv.setUint32(padded.length - 8, Math.floor(bitLen / 4294967296), false);

  let h = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));

  for (let off = 0; off < padded.length; off += 64) {{
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {{
      const s0 = rotr(w[i-15], 7) ^ rotr(w[i-15], 18) ^ (w[i-15] >>> 3);
      const s1 = rotr(w[i-2], 17) ^ rotr(w[i-2], 19) ^ (w[i-2] >>> 10);
      w[i] = (w[i-16] + s0 + w[i-7] + s1) | 0;
    }}
    let [a,b,c,d,e,f,g,hh] = h;
    for (let i = 0; i < 64; i++) {{
      const S1 = rotr(e,6) ^ rotr(e,11) ^ rotr(e,25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + SHA256_K[i] + w[i]) | 0;
      const S0 = rotr(a,2) ^ rotr(a,13) ^ rotr(a,22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = b; b = a; a = (t1 + t2) | 0;
    }}
    h[0]=(h[0]+a)|0; h[1]=(h[1]+b)|0; h[2]=(h[2]+c)|0; h[3]=(h[3]+d)|0;
    h[4]=(h[4]+e)|0; h[5]=(h[5]+f)|0; h[6]=(h[6]+g)|0; h[7]=(h[7]+hh)|0;
  }}
  return Array.from(h).map(x => (x >>> 0).toString(16).padStart(8, '0')).join('');
}}

function yieldToUI() {{
  return new Promise(resolve => setTimeout(resolve, 0));
}}

async function solve() {{
  const startTime = Date.now();
  const challengeId = {json.dumps(challenge_id)};
  const target = {json.dumps(target)};
  let nonce = 0;
  let digest = '';
  const YIELD_EVERY = 200000;  // sparse yield: keeps the tab responsive without reintroducing per-attempt async overhead
  do {{
    nonce++;
    digest = sha256hexSync(challengeId + ':' + nonce);
    if (nonce % YIELD_EVERY === 0) {{
      await yieldToUI();
    }}
  }} while (!digest.startsWith(target));

  // Respect server-enforced minimum solve time — if the PoW completed
  // faster than MIN_SOLVE_MS, wait for the remainder before submitting.
  const elapsed = Date.now() - startTime;
  if (elapsed < MIN_SOLVE_MS) {{
    await new Promise(r => setTimeout(r, MIN_SOLVE_MS - elapsed));
  }}

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

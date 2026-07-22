"""
Manual end-to-end verification of the challenge mirror, run directly (not via pytest)
to prove the server logic works before wiring the pytest suite. Mimics exactly what
the embedded challenge.js does in a real browser: parse challenge_id/target out of the
HTML, brute-force the PoW, POST /verify, then confirm the session cookie unlocks /.
"""
import hashlib
import re
import time

import requests

BASE = "http://127.0.0.1:8090"


def solve_pow(challenge_id: str, target: str) -> int:
    nonce = 0
    while True:
        nonce += 1
        digest = hashlib.sha256(f"{challenge_id}:{nonce}".encode()).hexdigest()
        if digest.startswith(target):
            return nonce


def extract(pattern, html_text):
    m = re.search(pattern, html_text)
    assert m, f"pattern not found: {pattern}"
    return m.group(1)


def run_flow(difficulty: str, expect_min_delay: float, bad_signals: bool = False):
    print(f"\n=== difficulty={difficulty} bad_signals={bad_signals} ===")
    session = requests.Session()

    # Step 1: dumb HTTP client behavior (Level 1 equivalent) — no JS, just fetch /
    r0 = session.get(f"{BASE}/?difficulty={difficulty}")
    assert r0.status_code == 200
    assert "Verified Content" not in r0.text, "L1-equivalent must NOT see real content without solving JS"
    assert "challenge-mirror-ok" not in r0.text
    print("  [ok] plain HTTP client correctly blocked (challenge page served, not content)")

    challenge_id = extract(r'challengeId = "([a-f0-9]+)"', r0.text)
    target = extract(r'target = "([0]+)"', r0.text)
    print(f"  challenge_id={challenge_id} target_prefix={target}")

    t0 = time.time()
    nonce = solve_pow(challenge_id, target)
    solve_time = time.time() - t0
    print(f"  solved nonce={nonce} in {solve_time:.3f}s")

    signals = {
        "webdriver": bad_signals,          # True simulates a naive undetected-automation tell
        "languages": True,
        "plugins_length": 0 if bad_signals else 5,
        "ua": "Mozilla/5.0 (X11; Linux x86_64) simulated",
    }

    if difficulty == "strict" and solve_time < expect_min_delay:
        time.sleep(expect_min_delay - solve_time + 0.1)  # simulate humanize delay budget

    r1 = session.post(f"{BASE}/verify", json={
        "challenge_id": challenge_id, "nonce": nonce, "signals": signals
    })

    if bad_signals:
        assert r1.json().get("status") == "rejected", f"expected rejected, got: {r1.text}"
        print(f"  [ok] verification correctly REJECTED for bot-like signals: {r1.text}")
        return

    assert r1.status_code == 200, f"verify failed: {r1.status_code} {r1.text}"
    print(f"  [ok] verification accepted: {r1.json()}")

    r2 = session.get(f"{BASE}/")
    assert r2.status_code == 200
    assert "challenge-mirror-ok" in r2.text
    print("  [ok] authenticated session now sees real content")


if __name__ == "__main__":
    run_flow("standard", expect_min_delay=0.0, bad_signals=False)
    run_flow("strict", expect_min_delay=3.0, bad_signals=False)
    run_flow("standard", expect_min_delay=0.0, bad_signals=True)
    print("\nALL MANUAL VERIFICATION FLOWS PASSED")

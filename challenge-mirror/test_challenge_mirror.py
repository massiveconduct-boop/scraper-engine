"""
CI-ready pytest suite for the challenge mirror itself (a test *fixture*, but it still
needs its own tests — an untested test fixture just moves the unverified-code problem
one layer down instead of solving it).

Run with: pytest tests/test_challenge_mirror.py -v
Requires the server running locally (see conftest fixture below) or against
CHALLENGE_MIRROR_URL env var pointing at a docker-compose instance.
"""
import hashlib
import os
import re
import subprocess
import sys
import time

import pytest
import requests

BASE = os.environ.get("CHALLENGE_MIRROR_URL", "http://127.0.0.1:8090")


@pytest.fixture(scope="session", autouse=True)
def ensure_server_running():
    """If nothing is listening at BASE, spin up a local instance for the test session."""
    try:
        requests.get(f"{BASE}/health", timeout=1)
        yield
        return
    except requests.exceptions.ConnectionError:
        time.sleep(1)  # wait for server to start, then retry

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        try:
            requests.get(f"{BASE}/health", timeout=0.5)
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError("challenge mirror did not start in time")
    yield
    proc.terminate()


def _solve(challenge_id: str, target: str) -> int:
    nonce = 0
    while True:
        nonce += 1
        if hashlib.sha256(f"{challenge_id}:{nonce}".encode()).hexdigest().startswith(target):
            return nonce


def test_health():
    r = requests.get(f"{BASE}/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_root_without_cookie_serves_challenge_not_content():
    r = requests.get(f"{BASE}/")
    assert r.status_code == 200
    assert "Verified Content" not in r.text
    assert "solve()" in r.text  # the PoW JS is present — proves this requires JS execution


def test_plain_http_client_cannot_reach_content_this_is_the_l1_negative_control():
    """This is the specific behavior blueprint tests/live/test_escalation_ladder.py
    asserts on for Level 1: a non-JS client must fail here, by construction."""
    session = requests.Session()
    for _ in range(3):  # even repeated plain fetches must never see content
        r = session.get(f"{BASE}/")
        assert "Verified Content" not in r.text


def test_full_pow_solve_standard_tier_grants_access():
    session = requests.Session()
    r0 = session.get(f"{BASE}/?difficulty=standard")
    challenge_id = re.search(r'challengeId = "([a-f0-9]+)"', r0.text).group(1)
    target = re.search(r'target = "([0]+)"', r0.text).group(1)
    nonce = _solve(challenge_id, target)

    r1 = session.post(f"{BASE}/verify", json={
        "challenge_id": challenge_id, "nonce": nonce,
        "signals": {"webdriver": False, "languages": True, "plugins_length": 5, "ua": "test"},
    })
    assert r1.status_code == 200

    r2 = session.get(f"{BASE}/")
    assert "Verified Content" in r2.text


def test_strict_tier_enforces_minimum_delay():
    """Solving faster than min_solve_seconds must be rejected — this is what makes
    the strict tier a meaningful target for Level 3 specifically (Level 2's shorter
    per-request timeout budget should legitimately fail here)."""
    session = requests.Session()
    r0 = session.get(f"{BASE}/?difficulty=strict")
    challenge_id = re.search(r'challengeId = "([a-f0-9]+)"', r0.text).group(1)
    target = re.search(r'target = "([0]+)"', r0.text).group(1)
    nonce = _solve(challenge_id, target)

    # Submit immediately — before the 3s min_solve_seconds has elapsed
    r1 = session.post(f"{BASE}/verify", json={
        "challenge_id": challenge_id, "nonce": nonce,
        "signals": {"webdriver": False, "languages": True, "plugins_length": 5, "ua": "test"},
    })
    assert r1.status_code == 403
    assert "too_fast" in r1.text


def test_webdriver_true_signal_is_rejected():
    """This is the negative control proving the mirror actually discriminates on the
    same signal Camoufox's fingerprint work exists to neutralize — without this test,
    a passing L2/L3 run would be meaningless (the mirror might just accept everything)."""
    session = requests.Session()
    r0 = session.get(f"{BASE}/?difficulty=standard")
    challenge_id = re.search(r'challengeId = "([a-f0-9]+)"', r0.text).group(1)
    target = re.search(r'target = "([0]+)"', r0.text).group(1)
    nonce = _solve(challenge_id, target)

    r1 = session.post(f"{BASE}/verify", json={
        "challenge_id": challenge_id, "nonce": nonce,
        "signals": {"webdriver": True, "languages": True, "plugins_length": 5, "ua": "test"},
    })
    assert r1.status_code == 403
    assert r1.text == "navigator_webdriver_true"


def test_expired_or_replayed_challenge_id_rejected():
    session = requests.Session()
    r0 = session.get(f"{BASE}/?difficulty=standard")
    challenge_id = re.search(r'challengeId = "([a-f0-9]+)"', r0.text).group(1)
    target = re.search(r'target = "([0]+)"', r0.text).group(1)
    nonce = _solve(challenge_id, target)

    payload = {"challenge_id": challenge_id, "nonce": nonce,
               "signals": {"webdriver": False, "languages": True, "plugins_length": 5, "ua": "t"}}
    r1 = session.post(f"{BASE}/verify", json=payload)
    assert r1.status_code == 200

    # Replay the exact same solved challenge — must fail (single-use)
    r2 = session.post(f"{BASE}/verify", json=payload)
    assert r2.status_code == 403
    assert r2.text == "unknown_or_expired_challenge_id"

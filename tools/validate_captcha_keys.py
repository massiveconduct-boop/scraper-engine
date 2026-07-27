#!/usr/bin/env python
"""Preflight: is each CAPTCHA provider key actually accepted by its provider?

The worker only knows whether a key is *present* (it can't do a network call at
startup). A key can be present but rejected — NoCaptchaAI with an inactive
reCAPTCHA capability, or a CapSolver key returning 401. That failure is otherwise
invisible until a real solve silently returns no token mid-scrape.

This tool calls each provider's balance endpoint and reports, per provider:
  configured (key present?) · ok (provider accepted it?) · detail (balance / error)

Keys are read from the environment (loaded from .env by the caller) and never
printed. Exit code: 0 if at least one provider is working, 1 if none are.

Usage:
  set -a && . ./.env && set +a && .venv/bin/python tools/validate_captcha_keys.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from services.captcha_solver import validate_captcha_keys


def _mask(env_var: str) -> str:
    v = os.environ.get(env_var)
    if not v:
        return "<absent>"
    return f"{v[:4]}…{v[-2:]} (len={len(v)})"


async def main() -> int:
    print("CAPTCHA provider key preflight")
    print(f"  NOCAPTCHA_AI_API_KEY: {_mask('NOCAPTCHA_AI_API_KEY')}")
    print(f"  CAPSOLVER_API_KEY:    {_mask('CAPSOLVER_API_KEY')}")
    print()

    results = await validate_captcha_keys()
    any_solve_capable = False
    for provider, r in results.items():
        balance = r.get("balance")
        if not r["configured"]:
            status = "— (no key set)"
        elif not r["ok"]:
            status = "REJECTED"  # key not accepted at all (bad/expired/401)
        elif isinstance(balance, (int, float)) and balance <= 0:
            status = "NO FUNDS"  # authenticates, but $0 → solves will fail
        else:
            status = "WORKING"
            any_solve_capable = True
        print(f"  {provider:<12} {status:<12} {r['detail']}")

    print()
    print(
        "note: WORKING means the key authenticates AND has a balance. It does not "
        "prove a specific captcha capability is active — confirm with "
        "tools/verify_captcha_live.py."
    )
    if any_solve_capable:
        print("PASS: at least one provider authenticates and has funds.")
        return 0
    print(
        "FAIL: no solve-capable provider. Fix the REJECTED / NO-FUNDS provider "
        "(replace the key or top up its balance), then re-run."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

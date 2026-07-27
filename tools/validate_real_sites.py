#!/usr/bin/env python
"""Round 15 — real-target validation against public, purpose-built scraping /
anti-bot TEST sandboxes (all sites exist explicitly for this).

Exploratory, not pass/fail against a pre-written assertion: the job is to OBSERVE
where the scraper's real behaviour matches or diverges from design. For each
target we record level, success, HTTP status, ChallengeDetector verdict, duration
and whether an expected content marker was actually extracted.

Run a single group:  python tools/validate_real_sites.py l1|l2|l3
"""

import asyncio
import sys

from config.loader import load_config
from core.tenant import TenantId
from fetcher.challenge_detector import ChallengeDetector
from fetcher.factory import (
    build_level1_fetcher,
    build_level2_fetcher,
    build_level3_fetcher,
)

TENANT = TenantId("realsites")
DETECTOR = ChallengeDetector()

# (label, url, marker that proves real content was extracted)
STATIC = [
    ("books.toscrape", "http://books.toscrape.com/", "A Light in the Attic"),
    ("quotes.toscrape", "http://quotes.toscrape.com/", "Albert Einstein"),
    ("scrapethissite", "https://www.scrapethissite.com/pages/simple/", "Andorra"),
    ("webscraper.io-ecom", "https://webscraper.io/test-sites/e-commerce/allinone", "Laptops"),
]
JS_AND_ANTIBOT = [
    ("webscraper.io-scroll", "https://webscraper.io/test-sites/e-commerce/scroll", "Laptops"),
    ("nowsecure.nl", "https://nowsecure.nl/", "You are through"),
    ("sannysoft-fp", "https://bot.sannysoft.com/", "WebDriver"),
    ("scrapecups", "https://harvester.scrapecups.me/", None),
]


def classify(html: str | None, status: int) -> str:
    if not html:
        return "no-html"
    return "CHALLENGE" if DETECTOR.is_challenge_page(html, status) else "clean"


def line(level: str, label: str, r, marker: str | None) -> str:
    html = r.html or ""
    verdict = classify(r.html, r.http_status or 0)
    mk = ("n/a" if marker is None
          else ("FOUND" if marker in html else "MISSING"))
    return (
        f"[{level}] {label:22} success={str(r.success):5} "
        f"http={r.http_status} detector={verdict:9} marker={mk:7} "
        f"dur={r.duration_ms}ms len={len(html)} cat={r.failure_category}"
    )


async def run_l1() -> None:
    f = build_level1_fetcher(load_config())
    print("=== L1 (HTTP/Scrapling, no JS) vs static sites ===")
    for label, url, marker in STATIC:
        try:
            r = await f.fetch(url, TENANT)
            print(line("L1", label, r, marker))
        except Exception as exc:
            print(f"[L1] {label:22} EXCEPTION: {exc!r}")


async def run_browser(level: str) -> None:
    cfg = load_config()
    f = build_level2_fetcher(cfg) if level == "l2" else build_level3_fetcher(cfg)
    tag = level.upper()
    print(f"=== {tag} (Camoufox) vs JS + anti-bot sites ===")
    # L1-static too, to see if the browser path also extracts them cleanly,
    # plus the JS/anti-bot set which L1 cannot handle.
    targets = STATIC + JS_AND_ANTIBOT if level == "l2" else JS_AND_ANTIBOT
    for label, url, marker in targets:
        try:
            r = await f.fetch(url, TENANT, proxy=None)
            print(line(tag, label, r, marker))
        except Exception as exc:
            print(f"[{tag}] {label:22} EXCEPTION: {exc!r}")


async def main() -> None:
    group = sys.argv[1] if len(sys.argv) > 1 else "l1"
    if group == "l1":
        await run_l1()
    elif group in ("l2", "l3"):
        await run_browser(group)
    else:
        print(f"unknown group {group!r}; use l1|l2|l3")


if __name__ == "__main__":
    asyncio.run(main())

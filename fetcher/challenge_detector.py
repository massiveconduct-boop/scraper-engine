# fetcher/challenge_detector.py
"""Heuristic classifier: is this HTML a block/challenge/interstitial page?

Design invariant §1.1.5: nothing is cached as successful content unless
FetchResult.success is True and the response is not a classified challenge page.
"""

from __future__ import annotations

import re


class ChallengeDetector:
    """Heuristic classifier for challenge/block/interstitial pages.

    Checks HTML content against known patterns (Cloudflare, DataDome, Akamai,
    reCAPTCHA, hCaptcha, custom WAF challenge pages).
    """

    # Known challenge indicators — matched case-insensitively in HTML body
    CHALLENGE_SIGNATURES: list[str] = [
        "cf-browser-verification",
        "cf-challenge-running",
        "g-recaptcha",
        "h-captcha",
        "datadome",
        "akamai-bot-manager",
        "_challenge",
        "interstitial",
        "captcha-delivery",
        "attention required",
        "please verify you are a human",
        "access denied",
        "request blocked",
    ]

    # HTTP status codes that strongly indicate blocks/challenges
    CHALLENGE_STATUS_CODES: set[int] = {403, 429, 503}

    # Patterns for classifying challenge vendor
    VENDOR_PATTERNS: dict[str, str] = {
        "cloudflare": r"cf-(?:browser-verification|challenge|ray-id)",
        "datadome": r"datadome",
        "akamai": r"akamai",
        "recaptcha": r"g-recaptcha",
        "hcaptcha": r"h-captcha",
        "custom_waf": r"(?:blocked|denied|challenge|verify).*?(?:human|bot|automated)",
    }

    def __init__(self) -> None:
        self._signatures_compiled = [
            re.compile(re.escape(sig), re.IGNORECASE)
            for sig in self.CHALLENGE_SIGNATURES
        ]

    def is_challenge_page(self, html: str, status_code: int) -> bool:
        """Returns True if the HTML looks like a challenge/block page."""
        # Quick check: HTTP status codes that signal blocking
        if status_code in self.CHALLENGE_STATUS_CODES:
            return True

        # Content-based check: scan HTML against known challenge signatures
        html_lower = html.lower()
        for sig_re in self._signatures_compiled:
            if sig_re.search(html_lower):
                return True

        # Short pages with no meaningful content are suspect
        text_content = self._strip_html(html)
        return len(text_content) < 50 and status_code == 200

    def classify_challenge_type(self, html: str) -> str:
        """Return the likely challenge vendor name (e.g., 'cloudflare', 'datadome')."""
        html_lower = html.lower()
        for vendor, pattern in self.VENDOR_PATTERNS.items():
            if re.search(pattern, html_lower, re.IGNORECASE):
                return vendor
        return "unknown"

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove HTML tags to get visible text content."""
        import re as _re
        text = _re.sub(r"<[^>]+>", "", html)
        text = _re.sub(r"\s+", " ", text)
        return text.strip()

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
        # Challenge mirror + CDN interstitial page indicators
        "verifying your browser",
        "checking your browser",
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
            re.compile(re.escape(sig), re.IGNORECASE) for sig in self.CHALLENGE_SIGNATURES
        ]

    def is_challenge_page(
        self, html: str, status_code: int, *, short_page_is_suspect: bool = True
    ) -> bool:
        """Returns True if the HTML looks like a challenge/block page.

        When short_page_is_suspect is False, the short-page heuristic is
        skipped — useful for polling loops (Level3Fetcher retry loop) where
        the page is already loaded and the only question is "challenge solved
        yet?", and a short solved-marker page would otherwise be misclassified.
        """
        # Quick check: HTTP status codes that signal blocking
        if status_code in self.CHALLENGE_STATUS_CODES:
            return True

        # Content-based check: scan HTML against known challenge signatures
        html_lower = html.lower()
        for sig_re in self._signatures_compiled:
            if sig_re.search(html_lower):
                return True

        # Short pages with no meaningful content are suspect
        if short_page_is_suspect:
            text_content = self._strip_html(html)
            return len(text_content) < 50 and status_code == 200

        return False

    # Markers that a page's real content is rendered client-side (JS-gated).
    _JS_REQUIRED_MARKERS: tuple[str, ...] = (
        "you need to enable javascript",
        "please enable javascript",
        "javascript is required",
        "javascript is disabled",
        "enable javascript to run this app",
        "this app requires javascript",
    )
    # Empty single-page-app mount points — the shell an HTTP-only fetch sees
    # before the framework renders anything into them.
    _EMPTY_SPA_ROOTS: tuple[str, ...] = (
        '<div id="root"></div>',
        '<div id="app"></div>',
        "<app-root></app-root>",
        '<div id="__next"></div>',
        '<div id="app" class=""></div>',
    )

    def looks_javascript_gated(self, html: str) -> bool:
        """True if this looks like a JS-gated shell whose real content did not
        render (an HTTP-only fetch of a SPA / JS-required page).

        Deliberately conservative to avoid escalating fully-rendered static
        pages that merely carry a `<noscript>` analytics tag: requires BOTH a
        JS-required marker (or an empty SPA mount point) AND thin visible text.
        A complete static page has plenty of visible text even with a noscript
        block, so it will not trip this.
        """
        if not html:
            return False
        html_lower = html.lower()
        has_js_required = any(m in html_lower for m in self._JS_REQUIRED_MARKERS)
        has_empty_root = any(r in html_lower for r in self._EMPTY_SPA_ROOTS)
        if not (has_js_required or has_empty_root):
            return False
        # Thin rendered content is the corroborating signal.
        return len(self._strip_html(html)) < 500

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

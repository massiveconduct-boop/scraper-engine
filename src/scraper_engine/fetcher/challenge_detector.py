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
        # Round 45 — Cloudflare's own minimal bot-management rejection body
        # ("error code: 1010" — JA3/browser-fingerprint banned, a handful of
        # bytes with no other markup). Confirmed live against real target
        # domains in this deployment's own batch (nairametrics.com,
        # techcabal.com) — an L1 (non-JS, easily-fingerprinted) request gets
        # this exact page, while a real browser does not.
        "error code: 1010",
    ]

    # HTTP status codes that strongly indicate blocks/challenges. 500/502/504
    # added round 33 — a free proxy's own upstream dying produces exactly
    # these, and previously wasn't in this set at all (only 503 was). 404
    # added round 45 — live-caught: a definitive-looking 404 turned out, for
    # at least 2 of 5 domains investigated, to be a WAF/anti-bot block
    # disguised as "not found" (Cloudflare returning 403 with a
    # "banned browser signature" body to a naive L1 request, but other
    # target sites showed inconsistent block-vs-real-404 presentation
    # depending on exact request fingerprint) rather than a genuinely dead
    # page — real users confirmed the same URLs load fine in an actual
    # browser. A 404 alone is no longer trustworthy enough to skip giving a
    # real browser (L2/L3) a chance, same reasoning as 403/429/5xx above.
    # See orchestrator/worker.py's final-level confirmation check for how a
    # 404 that's STILL present after a real browser render gets treated as
    # a genuine NOT_FOUND instead of silently accepted as content.
    CHALLENGE_STATUS_CODES: set[int] = {403, 404, 429, 500, 502, 503, 504}

    # A gateway/proxy failure page (the proxy's own upstream connection
    # died — not the target blocking us) is not real content, same problem
    # class as an unsolved anti-bot challenge. It also can't always be
    # caught via CHALLENGE_STATUS_CODES above: the browser-level fetchers
    # (Level2Fetcher's Botasaurus path, and any path that can't expose the
    # real navigation status) report a fixed 200 regardless of what the
    # page's actual content is. Real examples captured live (round 33) from
    # 3 unrelated free proxies share no vendor string in common — nginx/
    # openresty's stock error_page ("500 Internal Server Error ...
    # openresty"), Squid's ("500 Internal Server Error
    # (ERR_SOCKET_FAILURE)"), and a small proxy's own shell ("proxylite
    # Error - 500 ... Name resolution failed.") — but do share a shape: a
    # very short body whose only real content is a 5xx number next to an
    # error-flavored word. A structural check generalizes to vendor
    # software never seen before, unlike literal strings added to
    # CHALLENGE_SIGNATURES one at a time.
    _GATEWAY_ERROR_MAX_LEN = 300
    _GATEWAY_ERROR_NUMBER_RE = re.compile(r"\b5\d{2}\b")
    _GATEWAY_ERROR_WORD_RE = re.compile(
        r"\b(error|gateway|unavailable|time-?out|refused|failure)\b", re.IGNORECASE
    )

    # Firefox/Gecko's OWN internal viewer wrapper for a non-HTML response
    # body (plain text, unformatted JSON, etc.) — `<meta name="color-scheme"
    # content="light dark">` + `<pre style="word-wrap: break-word;
    # white-space: pre-wrap;">`. Camoufox is Firefox-based, so any page
    # rendered through this wrapper means the real HTTP response was never
    # HTML in the first place — caught live (round 33, same investigation
    # as the gateway-error work above): a free proxy returned a bare
    # `text/plain` body reading "DNS cache overflow" with a real HTTP 200,
    # which the gateway-error check above didn't catch (no 5xx number, no
    # error-flavored word — a different vocabulary than any of the 3
    # captures that motivated that check). Matching this wrapper instead of
    # the diagnostic text itself generalizes to whatever a misbehaving
    # proxy's plain-text body happens to say, not just this one string.
    _FIREFOX_PLAINTEXT_WRAPPER_RE = re.compile(
        r'<pre\s+style="word-wrap:\s*break-word;\s*white-space:\s*pre-wrap;?"', re.IGNORECASE
    )

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

        # Runs unconditionally (not gated on status_code or
        # short_page_is_suspect) — a gateway error page is short regardless
        # of which caller is asking, including the Botasaurus path and
        # poll_until_solved's mid-retry checks, both of which always pass
        # status_code=200 whether or not that's the real status.
        if self._looks_like_gateway_error(html):
            return True

        # Also unconditional, same rationale — see _FIREFOX_PLAINTEXT_WRAPPER_RE.
        if self._FIREFOX_PLAINTEXT_WRAPPER_RE.search(html):
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

    def _looks_like_gateway_error(self, html: str) -> bool:
        """Structural check: a short body carrying a 5xx number next to an
        error-flavored word, regardless of exact vendor wording."""
        if not html:
            return False
        text = self._strip_html(html)
        if not text or len(text) > self._GATEWAY_ERROR_MAX_LEN:
            return False
        return bool(
            self._GATEWAY_ERROR_NUMBER_RE.search(text) and self._GATEWAY_ERROR_WORD_RE.search(text)
        )

    def classify_challenge_type(self, html: str) -> str:
        """Return the likely challenge vendor name (e.g., 'cloudflare', 'datadome')."""
        html_lower = html.lower()
        for vendor, pattern in self.VENDOR_PATTERNS.items():
            if re.search(pattern, html_lower, re.IGNORECASE):
                return vendor
        return "unknown"

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove HTML tags to get visible text content.

        Tags are replaced with a space, not removed outright — adjacent
        inline elements with no whitespace between them in the source
        (e.g. `500</title></head><body><h2>Name`) would otherwise glue
        into one word ("500Name"), breaking any \\b-boundary regex over the
        result (round 33 — found via a real captured gateway-error page
        whose title and body tags directly abutted)."""
        import re as _re

        text = _re.sub(r"<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text)
        return text.strip()

# tests/unit/test_challenge_detector.py
"""ChallengeDetector — challenge-page classification and JS-gated shell detection.

The detector gates escalation decisions (challenge pages, and round-15's
JS-gated-shell escalation), so it warrants direct coverage.
"""

from scraper_engine.fetcher.challenge_detector import ChallengeDetector

CD = ChallengeDetector()


class TestIsChallengePage:
    def test_cloudflare_interstitial_flagged(self):
        html = (
            "<html><head><title>Just a moment...</title></head>"
            "<body>cf-challenge-running</body></html>"
        )
        assert CD.is_challenge_page(html, 200) is True

    def test_blocking_status_code_flagged(self):
        assert CD.is_challenge_page("<html>ok</html>", 403) is True

    def test_real_content_not_flagged(self):
        html = "<html><body>" + "<p>Real article text. </p>" * 30 + "</body></html>"
        assert CD.is_challenge_page(html, 200) is False

    def test_short_page_suspect_toggle(self):
        tiny = "<html><body>hi</body></html>"
        assert CD.is_challenge_page(tiny, 200, short_page_is_suspect=True) is True
        # polling loops disable the short-page heuristic to avoid misclassifying
        # a short solved-marker page as still-a-challenge
        assert CD.is_challenge_page(tiny, 200, short_page_is_suspect=False) is False

    def test_google_ad_manager_interstitial_ad_slot_not_flagged(self):
        """Round 46 — live-caught: bare "interstitial" false-positived on a
        real, live nairametrics.com/businessday.ng article page's Google Ad
        Manager boilerplate — a completely ordinary ad-slot type, not an
        anti-bot interstitial. Exact real-world snippet."""
        html = (
            "<html><body><article>"
            + ("Real published article text discussing the news event. " * 40)
            + "</article><script>"
            "if (anchorSlot) { anchorSlot.addService(googletag.pubads()); }"
            "/* Interstitial */"
            "const interstitialSlot = googletag.defineOutOfPageSlot("
            "'/1234/site', googletag.enums.OutOfPageFormat.INTERSTITIAL);"
            "</script></body></html>"
        )
        assert CD.is_challenge_page(html, 200, short_page_is_suspect=False) is False

    def test_embedded_recaptcha_widget_for_unrelated_form_not_flagged(self):
        """Round 46 — live-caught: bare "g-recaptcha" false-positived on a
        real, live premiumtimesng.com page whose comment-form widget uses
        reCAPTCHA — unrelated to whether the article content itself was
        blocked. Exact real-world snippet (a CSS rule referencing the
        widget's class name)."""
        html = (
            "<html><head><style>"
            ".dark_mode_switch{position:relative}.g-recaptcha{margin-bottom:15px}"
            "</style></head><body><article>"
            + ("Real published article text discussing the news event. " * 40)
            + "</article></body></html>"
        )
        assert CD.is_challenge_page(html, 200, short_page_is_suspect=False) is False


class TestGatewayErrorPages:
    """Round 33: a free proxy's own upstream dying used to slip through as
    success — level_2.py/level_3.py hardcode http_status=200 on every
    browser-level fetch regardless of what the page actually navigated to
    (fixed separately), and the Botasaurus path and poll_until_solved's
    mid-retry checks always pass status_code=200 too, by design (they don't
    have a real status to pass). So this must be caught by content alone.
    All 3 HTML bodies below are real responses captured live from 3
    unrelated free proxies (not fabricated) — see .wolf/STATUS.md round 33
    for how they were gathered. They share no vendor string in common,
    which is exactly why a structural heuristic is used instead of a
    growing literal-string list."""

    # nginx/openresty's stock error_page.
    OPENRESTY_500 = (
        "<html>\n<head><title>500 Internal Server Error</title></head>\n"
        "<body>\n<center><h1>500 Internal Server Error</h1></center>\n"
        "<hr><center>openresty</center>\n</body>\n</html>\n"
    )
    # Squid's default error page — note title/body tags directly abut with
    # no whitespace, the exact case that broke _strip_html's word boundary.
    SQUID_500 = (
        "<HTML><HEAD><TITLE>500 Internal Server Error (ERR_SOCKET_FAILURE)"
        "</TITLE></HEAD><BODY><H1>500 Internal Server Error</H1><BR>"
        "ERR_SOCKET_FAILURE<HR><B>Webserver</B> Sun, 09 Aug 2026 12:48:11 "
        "GMT</BODY></HTML>"
    )
    # A small proxy's own custom error shell — also directly-abutting tags.
    PROXYLITE_500 = (
        '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN"><html><head>'
        "<title>proxylite Error - 500</title></head><body><h2>Name "
        "resolution failed.</h2></body></html>"
    )

    def test_openresty_gateway_error_flagged_even_with_fake_200_status(self):
        assert CD.is_challenge_page(self.OPENRESTY_500, 200, short_page_is_suspect=False) is True

    def test_squid_gateway_error_flagged_even_with_fake_200_status(self):
        assert CD.is_challenge_page(self.SQUID_500, 200, short_page_is_suspect=False) is True

    def test_proxylite_gateway_error_flagged_even_with_fake_200_status(self):
        assert CD.is_challenge_page(self.PROXYLITE_500, 200, short_page_is_suspect=False) is True

    def test_real_success_page_from_same_capture_session_not_flagged(self):
        """The control case — a genuine 200 response captured in the same
        session (proxied fetch of example.com through a working proxy)
        must not be caught by the new heuristic."""
        real_success = (
            '<!doctype html><html lang="en"><head><title>Example Domain</title>'
            '<link rel="icon" href="data:,"><meta name="viewport" '
            'content="width=device-width, initial-scale=1">'
            "<style>body{background:#eee;width:60vw;margin:15vh auto;"
            "font-family:system-ui,sans-serif}h1{font-size:1.5em}"
            "div{opacity:0.8}a:link,a:visited{color:#348}</style></head>"
            "<body><div><h1>Example Domain</h1><p>This domain is for use in "
            "documentation examples without needing permission. Avoid use "
            'in operations.</p><p><a href="https://iana.org/domains/example">'
            "Learn more</a></p></div></body></html>"
        )
        assert (
            CD.is_challenge_page(real_success, 200, short_page_is_suspect=False) is False
        )

    def test_literal_504_gateway_timeout_from_original_bug_report_flagged(self):
        """The exact wording from the live bug that started this investigation
        (`<title>504 Gateway Time-out</title>` — see .wolf/STATUS.md round 32)."""
        html = "<html><head><title>504 Gateway Time-out</title></head><body></body></html>"
        assert CD.is_challenge_page(html, 200, short_page_is_suspect=False) is True

    def test_real_status_code_alone_is_sufficient_502_504(self):
        """When the real navigation status IS available (level_2.py/level_3.py
        now thread it through instead of hardcoding 200), 502/504/500 alone
        trip CHALLENGE_STATUS_CODES without needing to inspect content."""
        assert CD.is_challenge_page("<html>anything</html>", 502) is True
        assert CD.is_challenge_page("<html>anything</html>", 504) is True
        assert CD.is_challenge_page("<html>anything</html>", 500) is True

    def test_long_page_mentioning_error_and_a_500ish_number_not_flagged(self):
        """The heuristic requires a SHORT body — a real, substantial page that
        happens to mention an error code in passing (e.g. a blog post about
        outages) must not be misclassified."""
        html = (
            "<html><body>"
            + ("<p>This is a real article about handling 500 errors gracefully. </p>" * 10)
            + "</body></html>"
        )
        assert CD.is_challenge_page(html, 200, short_page_is_suspect=False) is False


class TestFirefoxPlaintextWrapper:
    """Round 33 follow-up — found by actually re-running the live
    escalation-ladder check after the gateway-error fix above, not by
    guessing: a real job against a real free proxy came back
    success=True, http_status=200, is_challenge_page=False with the ACTUAL
    captured HTML snapshot (pulled straight from this session's own MinIO,
    not fabricated) being exactly the string below — a free proxy's raw
    text/plain diagnostic body, wrapped in Firefox/Gecko's own internal
    plain-text-viewer template (Camoufox is Firefox-based). This slipped
    past the gateway-error check above because it has no 5xx number and no
    word from that check's vocabulary ("overflow" isn't "error" or
    "gateway" etc.) — proof that a literal-word-list approach doesn't
    generalize, which is why this check matches the *wrapper markup*
    instead of anything the diagnostic text says."""

    # Exact raw bytes pulled from MinIO (scraper-snapshots bucket) after a
    # real job against this session's own free-proxy pool + challenge-mirror
    # over this box's Tailscale IP.
    DNS_CACHE_OVERFLOW = (
        '<html><head><meta name="color-scheme" content="light dark"></head>'
        '<body><pre style="word-wrap: break-word; white-space: pre-wrap;">'
        "DNS cache overflow</pre></body></html>"
    )

    def test_real_captured_dns_cache_overflow_page_flagged(self):
        assert (
            CD.is_challenge_page(self.DNS_CACHE_OVERFLOW, 200, short_page_is_suspect=False)
            is True
        )

    def test_arbitrary_plaintext_wrapper_body_flagged_regardless_of_wording(self):
        """The point of matching the wrapper, not the text: an entirely
        different diagnostic string in the same Firefox wrapper must also
        be caught, proving this generalizes beyond the one string observed
        live."""
        html = (
            '<html><head><meta name="color-scheme" content="light dark"></head>'
            '<body><pre style="word-wrap: break-word; white-space: pre-wrap;">'
            "connection refused by upstream</pre></body></html>"
        )
        assert CD.is_challenge_page(html, 200, short_page_is_suspect=False) is True

    def test_real_html_page_not_using_the_wrapper_not_flagged(self):
        """Control — a normal rendered HTML page (no <pre> plaintext
        wrapper) must not be caught by this check."""
        html = "<html><body>" + "<p>Real article text. </p>" * 30 + "</body></html>"
        assert CD.is_challenge_page(html, 200, short_page_is_suspect=False) is False


class TestLooksJavascriptGated:
    def test_empty_spa_root(self):
        html = '<html><body><div id="root"></div><script src="/a.js"></script></body></html>'
        assert CD.looks_javascript_gated(html) is True

    def test_noscript_enable_js_with_empty_shell(self):
        html = (
            "<html><body><noscript>You need to enable JavaScript to run this app."
            '</noscript><div id="root"></div></body></html>'
        )
        assert CD.looks_javascript_gated(html) is True

    def test_full_static_page_with_analytics_noscript_not_gated(self):
        # A complete page that merely carries a noscript tag must NOT be treated
        # as JS-gated — lots of visible text is the corroborating signal.
        html = (
            "<html><body>"
            + "<p>Real content paragraph. </p>" * 40
            + "<noscript>Please enable JavaScript for analytics</noscript></body></html>"
        )
        assert CD.looks_javascript_gated(html) is False

    def test_normal_content_not_gated(self):
        html = "<html><body>" + "x" * 800 + "</body></html>"
        assert CD.looks_javascript_gated(html) is False

    def test_empty_html_not_gated(self):
        assert CD.looks_javascript_gated("") is False


class TestClassifyChallengeType:
    def test_cloudflare_vendor_matched(self):
        html = "<html><body>cf-browser-verification ray-id abc</body></html>"
        assert CD.classify_challenge_type(html) == "cloudflare"

    def test_datadome_vendor_matched(self):
        html = "<html><body>protected by datadome</body></html>"
        assert CD.classify_challenge_type(html) == "datadome"

    def test_unmatched_html_returns_unknown(self):
        html = "<html><body>" + "<p>Ordinary article text.</p>" * 10 + "</body></html>"
        assert CD.classify_challenge_type(html) == "unknown"

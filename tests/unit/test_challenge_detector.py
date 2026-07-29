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

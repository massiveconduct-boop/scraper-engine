# tests/live/test_botasaurus_extension_loading.py
"""Live test — requires a real Botasaurus/Chromium launch.

Round 60: `Driver(extensions=[...])` needs each item to expose
`.load(with_command_line_option=False) -> str` (LocalExtension,
browser/_botasaurus_extension.py) — a mocked-Driver unit test can only
prove the kwarg is forwarded, not that Chromium actually loads the
extension. This is the only place that can catch a real regression (a
wrong manifest field, a Chromium `--load-extension` behavior change,
LocalExtension.load() returning a bad path) — round-60's own live
verification used a throwaway script; this commits that check instead of
letting `tests/fixtures/botasaurus_test_extension/` sit unreferenced.

content.js writes a DOM attribute (not a `window.*` JS variable) precisely
because content-script isolated-world variables aren't visible to page-
context `driver.run_js()` reads — only real DOM mutations are shared
across the isolated/main-world split. See the fixture's own content.js
comment.
"""

from __future__ import annotations

import os

import pytest

FIXTURE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "botasaurus_test_extension"
)


@pytest.mark.live
def test_local_extension_actually_loads_in_chromium():
    from botasaurus.browser import Driver

    from scraper_engine.browser._botasaurus_extension import LocalExtension

    driver = Driver(
        headless=False,
        enable_xvfb_virtual_display=True,
        extensions=[LocalExtension(FIXTURE_DIR)],
    )
    try:
        driver.get("https://example.com")
        marker = driver.run_js(
            "return document.documentElement."
            "getAttribute('data-botasaurus-test-extension-loaded') === 'true';"
        )
        print(f"extension-loaded marker: {marker!r}")
        assert marker is True, "extension did not load — marker attribute not observed"
    finally:
        driver.close()

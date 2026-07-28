# tests/unit/test_captcha_inpage.py
"""In-page CAPTCHA solve wiring: fetcher/_captcha.solve_captcha_on_page and the
Level2/Level3 _maybe_solve_captcha integration.

The DOM detect/inject JS runs in a real browser in production; here a FakePage
stands in for Playwright's page — evaluate() returns a scripted detect result
and records inject calls, so the orchestration (detect → solve → inject → gate)
is verified without a browser. End-to-end against a live CAPTCHA is out of scope
(needs a solver entitlement + real target — not yet available at build time)."""

from unittest.mock import AsyncMock

import pytest

from core.tenant import TenantId
from fetcher._captcha import solve_captcha_on_page
from fetcher.level_2 import Level2Fetcher
from fetcher.level_3 import Level3Fetcher

TENANT = TenantId("captchawire")
URL = "https://target.example/protected"


class FakePage:
    """Minimal Playwright-page stand-in. `detect` is what the first evaluate()
    (the detect JS) returns; subsequent evaluate() calls (injection) are recorded
    and return True. `inject_raises` forces the injection eval to blow up."""

    def __init__(self, detect, *, inject_raises=False):
        self._detect = detect
        self._inject_raises = inject_raises
        self.evaluate_calls: list[tuple] = []
        self.waited_ms = 0

    async def evaluate(self, script, *args):
        self.evaluate_calls.append((script, args))
        if not self.evaluate_calls[:-1]:  # first call == detect
            return self._detect
        if self._inject_raises:
            raise RuntimeError("eval boom")
        return True

    async def wait_for_timeout(self, ms):
        self.waited_ms += ms

    async def content(self):
        return "<html>solved real content</html>"


def _solver(**kw):
    s = AsyncMock()
    s.solve_recaptcha_v2.return_value = kw.get("recaptcha_v2")
    s.solve_hcaptcha.return_value = kw.get("hcaptcha")
    s.solve_turnstile.return_value = kw.get("turnstile")
    return s


class TestSolveCaptchaOnPage:
    @pytest.mark.asyncio
    async def test_recaptcha_detect_solve_inject(self):
        page = FakePage({"kind": "recaptcha_v2", "sitekey": "6Lc_ABC"})
        solver = _solver(recaptcha_v2="tok-123")
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is True
        solver.solve_recaptcha_v2.assert_awaited_once_with(TENANT, "6Lc_ABC", URL)
        # detect + inject == 2 evaluate calls; inject received the token
        assert len(page.evaluate_calls) == 2
        assert page.evaluate_calls[1][1] == ("tok-123",)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,attr", [
        ("hcaptcha", "solve_hcaptcha"),
        ("turnstile", "solve_turnstile"),
    ])
    async def test_kind_routes_to_correct_method(self, kind, attr):
        page = FakePage({"kind": kind, "sitekey": "SK"})
        solver = _solver(**{kind: "tok"})
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is True
        getattr(solver, attr).assert_awaited_once_with(TENANT, "SK", URL)

    @pytest.mark.asyncio
    async def test_no_widget_returns_false_no_solve(self):
        page = FakePage(None)
        solver = _solver(recaptcha_v2="tok")
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is False
        solver.solve_recaptcha_v2.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_sitekey_returns_false(self):
        page = FakePage({"kind": "recaptcha_v2", "sitekey": None})
        solver = _solver(recaptcha_v2="tok")
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is False
        solver.solve_recaptcha_v2.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_kind_returns_false(self):
        page = FakePage({"kind": "datadome", "sitekey": "SK"})
        solver = _solver()
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is False

    @pytest.mark.asyncio
    async def test_solver_no_token_skips_injection(self):
        page = FakePage({"kind": "recaptcha_v2", "sitekey": "SK"})
        solver = _solver(recaptcha_v2=None)  # solver miss
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is False
        assert len(page.evaluate_calls) == 1  # detect only — no inject

    @pytest.mark.asyncio
    async def test_injection_failure_returns_false(self):
        page = FakePage({"kind": "turnstile", "sitekey": "SK"}, inject_raises=True)
        solver = _solver(turnstile="tok")
        assert await solve_captcha_on_page(page, solver=solver, tenant_id=TENANT, url=URL) is False

    @pytest.mark.asyncio
    async def test_detect_eval_failure_returns_false(self):
        class Boom:
            async def evaluate(self, *a):
                raise RuntimeError("page gone")
        solver = _solver(recaptcha_v2="tok")
        out = await solve_captcha_on_page(Boom(), solver=solver, tenant_id=TENANT, url=URL)
        assert out is False


class TestFetcherMaybeSolve:
    """_maybe_solve_captcha gating on the browser fetchers."""

    @pytest.mark.asyncio
    async def test_l2_no_solver_is_noop(self):
        f = Level2Fetcher(captcha_solver=None)
        page = FakePage({"kind": "recaptcha_v2", "sitekey": "SK"})
        html = "<div class='g-recaptcha'>challenge</div>"
        assert await f._maybe_solve_captcha(page, URL, TENANT, html) == html
        assert page.evaluate_calls == []  # never touched the page

    @pytest.mark.asyncio
    async def test_l2_non_challenge_html_is_noop(self):
        f = Level2Fetcher(captcha_solver=_solver(recaptcha_v2="tok"))
        page = FakePage({"kind": "recaptcha_v2", "sitekey": "SK"})
        html = "<html><body>Totally normal fully rendered content here.</body></html>"
        assert await f._maybe_solve_captcha(page, URL, TENANT, html) == html
        assert page.evaluate_calls == []

    @pytest.mark.asyncio
    async def test_l2_none_tenant_is_noop(self):
        f = Level2Fetcher(captcha_solver=_solver(recaptcha_v2="tok"))
        page = FakePage({"kind": "recaptcha_v2", "sitekey": "SK"})
        html = "<div class='g-recaptcha'>challenge</div>"
        out = await f._maybe_solve_captcha(page, URL, None, html)
        assert out == html

    @pytest.mark.asyncio
    async def test_l2_challenge_solves_and_repolls(self):
        f = Level2Fetcher(captcha_solver=_solver(recaptcha_v2="tok"))
        page = FakePage({"kind": "recaptcha_v2", "sitekey": "SK"})
        html = "<div class='g-recaptcha'>please verify you are a human</div>"
        out = await f._maybe_solve_captcha(page, URL, TENANT, html)
        assert out == "<html>solved real content</html>"  # re-read after solve
        assert page.waited_ms > 0  # waited before re-poll

    @pytest.mark.asyncio
    async def test_l3_challenge_solves_and_repolls(self):
        f = Level3Fetcher(captcha_solver=_solver(turnstile="tok"))
        page = FakePage({"kind": "turnstile", "sitekey": "SK"})
        html = "<div class='cf-turnstile'>checking your browser</div>"
        out = await f._maybe_solve_captcha(page, URL, TENANT, html)
        assert out == "<html>solved real content</html>"

    @pytest.mark.asyncio
    async def test_l3_no_solver_is_noop(self):
        f = Level3Fetcher(captcha_solver=None)
        page = FakePage({"kind": "turnstile", "sitekey": "SK"})
        html = "<div class='cf-turnstile'>checking your browser</div>"
        assert await f._maybe_solve_captcha(page, URL, TENANT, html) == html
        assert page.evaluate_calls == []

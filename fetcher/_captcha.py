# fetcher/_captcha.py
"""In-page CAPTCHA solving for the browser levels (L2/L3).

After `poll_until_solved` a page may still be a challenge interstitial holding a
token-grant widget (reCAPTCHA v2 / hCaptcha / Cloudflare Turnstile). Those are
not defeated by waiting — they need a *token*: read the widget's sitekey from
the DOM, hand it to a CAPTCHA-solving provider (services/captcha_solver.py),
then inject the returned token back into the page so the site's own callback /
form submission proceeds.

Why here (not in the solver): the solver only knows the anti-captcha protocol
(sitekey → token). The DOM read and token injection are browser-side and belong
next to the fetchers that own the live `page`. Keeping this separate from
level_2/level_3 keeps the single-source-of-truth pattern the fetchers already
use for `_content_utils`.

Best-effort by design: every step returns False rather than raising, so a solve
attempt can never turn a recoverable fetch into a crash. The DOM detect/inject
JS is unit-tested with a fake page; a real end-to-end solve depends on a live
solver entitlement + a real target (see docs/round-19-evidence.md — the
NoCaptchaAI account's reCAPTCHA capability was not active at build time).

The injection scripts are module constants (not inlined) so unit tests can
assert on them and so the reCAPTCHA callback-walk stays reviewable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.tenant import TenantId
    from services.captcha_solver import CaptchaSolver

logger = logging.getLogger(__name__)

# Read the first solvable widget's kind + sitekey from the DOM.
# Turnstile/hCaptcha are probed before the generic `[data-sitekey]` selector so
# a page carrying both a branded class and a bare data-sitekey classifies as the
# specific vendor, not reCAPTCHA. Returns {kind, sitekey} or null.
_DETECT_JS = r"""() => {
  const q = (s) => document.querySelector(s);
  let el = q('.cf-turnstile[data-sitekey], [data-sitekey].cf-turnstile');
  if (el) return {kind: 'turnstile', sitekey: el.getAttribute('data-sitekey')};
  el = q('.h-captcha[data-sitekey], [data-hcaptcha-sitekey]');
  if (el) return {kind: 'hcaptcha',
                  sitekey: el.getAttribute('data-sitekey')
                        || el.getAttribute('data-hcaptcha-sitekey')};
  el = q('.g-recaptcha[data-sitekey], [data-sitekey]');
  if (el) return {kind: 'recaptcha_v2', sitekey: el.getAttribute('data-sitekey')};
  const ri = q('iframe[src*="recaptcha"]');
  if (ri) { const m = ri.src.match(/[?&]k=([^&]+)/);
            if (m) return {kind: 'recaptcha_v2', sitekey: decodeURIComponent(m[1])}; }
  const hi = q('iframe[src*="hcaptcha"]');
  if (hi) { const m = hi.src.match(/[?&]sitekey=([^&]+)/);
            if (m) return {kind: 'hcaptcha', sitekey: decodeURIComponent(m[1])}; }
  return null;
}"""

# reCAPTCHA v2: set the response textarea, then invoke any registered client
# callback. The callback lives at a version-specific depth inside
# ___grecaptcha_cfg.clients, so walk two levels looking for a `callback` fn.
_INJECT_RECAPTCHA_JS = r"""(token) => {
  document.querySelectorAll(
    '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
  ).forEach((el) => { el.value = token; el.innerHTML = token; el.style.display = ''; });
  try {
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      for (const cid in cfg.clients) {
        const client = cfg.clients[cid];
        for (const k in client) {
          const o = client[k];
          if (o && typeof o.callback === 'function') { o.callback(token); continue; }
          if (o) for (const kk in o) {
            const oo = o[kk];
            if (oo && typeof oo.callback === 'function') oo.callback(token);
          }
        }
      }
    }
  } catch (e) { /* callback shape varies; textarea set is the fallback */ }
  return true;
}"""

# hCaptcha shares the g-recaptcha-response textarea name on many integrations,
# so set both.
_INJECT_HCAPTCHA_JS = r"""(token) => {
  document.querySelectorAll(
    'textarea[name="h-captcha-response"], #h-captcha-response, ' +
    'textarea[name="g-recaptcha-response"], #g-recaptcha-response'
  ).forEach((el) => { el.value = token; el.innerHTML = token; });
  return true;
}"""

# Turnstile writes to a hidden input named cf-turnstile-response; create it if
# the widget hasn't (it normally injects one on render).
_INJECT_TURNSTILE_JS = r"""(token) => {
  let el = document.querySelector('input[name="cf-turnstile-response"]');
  if (!el) {
    el = document.createElement('input');
    el.type = 'hidden';
    el.name = 'cf-turnstile-response';
    (document.querySelector('form') || document.body).appendChild(el);
  }
  el.value = token;
  document.querySelectorAll('textarea[name="g-recaptcha-response"]')
          .forEach((t) => { t.value = token; });
  return true;
}"""

# kind → (solver method name, injection script)
_HANDLERS: dict[str, tuple[str, str]] = {
    "recaptcha_v2": ("solve_recaptcha_v2", _INJECT_RECAPTCHA_JS),
    "hcaptcha": ("solve_hcaptcha", _INJECT_HCAPTCHA_JS),
    "turnstile": ("solve_turnstile", _INJECT_TURNSTILE_JS),
}


async def solve_captcha_on_page(
    page: Any,
    *,
    solver: CaptchaSolver,
    tenant_id: TenantId,
    url: str,
) -> bool:
    """Detect a solvable CAPTCHA widget on `page`, solve it, inject the token.

    Returns True only when a token was obtained and injected. Returns False —
    never raises — for every non-happy path: no widget, unextractable sitekey,
    unsupported kind, solver returned no token (budget/API/idle), or the
    injection eval failed. The caller re-reads the page afterwards; the
    ChallengeDetector still gates whether the result counts as success, so a
    failed or unaccepted solve degrades to "still a challenge", never a false
    positive.
    """
    try:
        found = await page.evaluate(_DETECT_JS)
    except Exception:
        return False
    if not found or not found.get("sitekey"):
        return False

    kind = found.get("kind")
    sitekey = found["sitekey"]
    handler = _HANDLERS.get(kind) if kind else None
    if handler is None:
        return False
    method_name, inject_js = handler

    from observability.metrics import captcha_solve_attempts_total, captcha_solved_total

    captcha_solve_attempts_total.labels(kind=kind).inc()
    logger.info("captcha detected kind=%s sitekey=%s… — requesting token", kind, sitekey[:8])

    token: str | None = await getattr(solver, method_name)(tenant_id, sitekey, url)
    if not token:
        logger.warning("captcha solve returned no token kind=%s", kind)
        return False

    try:
        await page.evaluate(inject_js, token)
    except Exception:
        logger.warning("captcha token injection failed kind=%s", kind, exc_info=True)
        return False

    captcha_solved_total.labels(kind=kind).inc()
    logger.info("captcha token injected kind=%s", kind)
    return True

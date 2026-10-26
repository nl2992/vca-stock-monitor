"""Browser-only, read-only inspection of the product page.

The page is never fetched over plain HTTP. A fresh Chromium is launched, the
exact product URL is opened, the inspection script below is evaluated against
the rendered DOM, and the browser is always closed in a `finally` block. There
is at most one bounded retry for a transient technical failure.

Nothing here clicks, types, signs in, adds to bag, or defeats a bot check. If
the site serves a wall instead of the product, that is reported as a fact and
the classifier turns it into INCONCLUSIVE.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .availability import check_identity_tokens, extract_product_price, looks_like_captcha
from .config import (
    CONTROLS_TIMEOUT_S,
    MAX_ATTEMPTS,
    NAV_TIMEOUT_S,
    PRODUCT_URL,
    RENDER_SETTLE_S,
)
from .models import Observation

log = logging.getLogger(__name__)

# Read-only DOM inspection, evaluated verbatim in the page. It reads
# attributes, computed styles and geometry; it mutates nothing.
INSPECTION_JS = """
(() => {
  const controls = document.querySelector('.vca-pdp-v2-controls');
  const ctaValue = controls?.getAttribute('data-show-cta') ?? null;
  const buttons = [...document.querySelectorAll('.base-checkout-addtocart-button')];
  const visibleButtons = buttons.filter(el => {
    const style = window.getComputedStyle(el);
    const visibleGeometry = el.offsetWidth > 0 ||
      el.offsetHeight > 0 || el.getClientRects().length > 0;
    return visibleGeometry && style.display !== 'none' &&
      style.visibility !== 'hidden' && style.opacity !== '0';
  });
  const visiblePrices = [...document.querySelectorAll(
    '[class*="price"], [data-testid*="price"]'
  )].filter(el => el.getClientRects().length > 0)
    .map(el => el.textContent?.trim()).filter(Boolean).slice(0, 10);
  return {
    url: window.location.href,
    pageTitle: document.title,
    timestamp: new Date().toISOString(),
    controlsFound: Boolean(controls),
    ctaValue,
    addToBagButtonCount: buttons.length,
    visibleAddToBagCount: visibleButtons.length,
    visibleButtonTexts: visibleButtons.map(el => el.innerText?.trim() || ''),
    prices: visiblePrices
  };
})()
"""

# Separate read of the rendered text, used for identity, price fallback and
# bot-wall detection.
PAGE_TEXT_JS = "() => document.body ? document.body.innerText : ''"

CONTROLS_SELECTOR = ".vca-pdp-v2-controls"


def inspect_product_page(
    url: str = PRODUCT_URL,
    *,
    headless: bool = True,
    max_attempts: int = MAX_ATTEMPTS,
) -> Observation:
    """Inspect the live product page and return one Observation.

    Never raises for a page-level problem: a failure is recorded on the
    Observation so the classifier can turn it into INCONCLUSIVE.
    """
    last_error: str | None = None
    started = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        try:
            observation = _inspect_once(url, headless=headless)
            return _with_timing(observation, started, attempt)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("inspection attempt %s/%s failed: %s", attempt, max_attempts, last_error)
            if attempt < max_attempts:
                time.sleep(3)

    return _with_timing(Observation(url=url, error=last_error), started, max_attempts)


def _with_timing(observation: Observation, started: float, attempts: int) -> Observation:
    from dataclasses import replace

    return replace(
        observation,
        duration_seconds=round(time.monotonic() - started, 2),
        attempts=attempts,
    )


def _inspect_once(url: str, *, headless: bool) -> Observation:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = None
        try:
            context = browser.new_context(
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_S * 1000)
            page.wait_for_timeout(RENDER_SETTLE_S * 1000)

            # Give the client-rendered controls a bounded extra chance.
            try:
                page.wait_for_selector(
                    f"{CONTROLS_SELECTOR}[data-show-cta]",
                    timeout=CONTROLS_TIMEOUT_S * 1000,
                )
            except Exception:
                log.info("product controls did not appear within %ss", CONTROLS_TIMEOUT_S)

            payload: dict[str, Any] = page.evaluate(INSPECTION_JS)
            page_text: str = page.evaluate(PAGE_TEXT_JS) or ""
            return build_observation(payload, page_text)
        finally:
            try:
                if page is not None:
                    page.close()
            finally:
                browser.close()


def build_observation(payload: dict[str, Any], page_text: str) -> Observation:
    """Combine the inspection payload with identity and price derivation.

    Split out from the browser plumbing so the same code path runs against
    local HTML fixtures in Chromium during tests.
    """
    reference_found, title_match = check_identity_tokens(
        page_text, payload.get("pageTitle") or ""
    )
    price, price_verified = extract_product_price(
        list(payload.get("prices") or []), page_text
    )
    captcha = looks_like_captcha(page_text) or looks_like_captcha(
        payload.get("pageTitle") or ""
    )
    return Observation.from_page_payload(
        payload,
        reference_found=reference_found,
        title_match=title_match,
        product_price=price,
        price_verified=price_verified,
        captcha_suspected=captcha,
    )

#!/usr/bin/env python3
"""Put VCARD35600 into a real, visible Chrome cart you can check out from.

Runs your installed Google Chrome (not bundled Chromium) against a dedicated,
persistent profile, so the cart survives in real cookies and you can reopen it
later and complete the purchase yourself.

The window is left OPEN on purpose: the point is to hand you a browser with the
bracelet already in the bag.

Hard limits, enforced in code:
  * It only ever acts on the verified VCARD35600 product page.
  * It clicks exactly one visible Add to Bag button and nothing else.
  * It never navigates to checkout, never touches payment or address fields,
    and never confirms an order. Buying stays entirely yours.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vca_monitor.availability import check_identity_tokens  # noqa: E402
from vca_monitor.config import PRODUCT_REFERENCE, PRODUCT_URL  # noqa: E402

PROFILE_DIR = Path.home() / "Library/Application Support/vca-monitor/chrome-profile"
SHOTS = Path.home() / "Library/Application Support/vca-monitor/shots"
ADD_BUTTON = ".base-checkout-addtocart-button"

# Anything resembling an order-completing control. Used as an assertion that we
# are not about to click the wrong thing, never as something to click.
FORBIDDEN = ("checkout", "place order", "pay now", "confirm order", "buy now")

VISIBLE_ADD_BUTTONS_JS = """
() => [...document.querySelectorAll('.base-checkout-addtocart-button')]
  .map((el, i) => {
    const s = getComputedStyle(el);
    const geom = el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0;
    return {
      index: i,
      text: (el.innerText || '').trim(),
      visible: geom && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0',
      disabled: el.disabled ?? null,
    };
  })
"""

CART_COUNT_JS = """
() => {
  const el = document.querySelector('[class*="cart"] [class*="count"], [class*="minicart"] [class*="count"], [data-cart-count]');
  const n = el ? (el.textContent || '').trim() : null;
  return { raw: n, bodyMentionsBag: /added to (your )?(bag|cart)/i.test(document.body.innerText) };
}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add VCARD35600 to a real Chrome cart")
    parser.add_argument("--keep-open", type=int, default=1800,
                        help="seconds to leave the browser open for you (default 1800)")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify everything and report, but do not click")
    parser.add_argument("--url", default=PRODUCT_URL,
                        help="override the target, used to exercise the click "
                             "path against a local fixture while the real page "
                             "has no visible button")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"clicked": False}

    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="chrome",      # your real Chrome, not bundled Chromium
                headless=False,        # a window you can see and take over
                viewport=None,
                args=["--start-maximized"],
            )
        except Exception as exc:
            print(json.dumps({"error": f"could not launch Chrome: {exc}"}))
            return 1

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            try:
                page.wait_for_selector(".vca-pdp-v2-controls[data-show-cta]", timeout=10000)
            except Exception:
                pass

            # Guard 1: we must be on the right product.
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            ref_ok, title_ok = check_identity_tokens(text, page.title())
            result["reference_found"] = ref_ok
            result["title_match"] = title_ok
            if not (ref_ok and title_ok):
                result["error"] = f"page is not the verified {PRODUCT_REFERENCE} product page"
                print(json.dumps(result, indent=2))
                return 2

            # Guard 2: exactly one visible Add to Bag button, nothing dodgy.
            buttons = page.evaluate(VISIBLE_ADD_BUTTONS_JS)
            visible = [b for b in buttons if b["visible"]]
            result["buttons"] = buttons
            if not visible:
                result["error"] = "no visible Add to Bag button; nothing to add"
                print(json.dumps(result, indent=2))
                return 3

            label = (visible[0].get("text") or "").lower()
            if any(bad in label for bad in FORBIDDEN):
                result["error"] = f"refusing to click a control labelled {label!r}"
                print(json.dumps(result, indent=2))
                return 4

            before = page.screenshot(path=str(SHOTS / "before-add.png"))
            result["screenshot_before"] = str(SHOTS / "before-add.png")

            if args.dry_run:
                result["note"] = "dry run: a visible Add to Bag button was found but not clicked"
                print(json.dumps(result, indent=2))
                return 0

            # The single permitted action.
            page.locator(ADD_BUTTON).nth(visible[0]["index"]).click(timeout=15000)
            result["clicked"] = True
            page.wait_for_timeout(6000)

            result["cart_signal"] = page.evaluate(CART_COUNT_JS)
            page.screenshot(path=str(SHOTS / "after-add.png"))
            result["screenshot_after"] = str(SHOTS / "after-add.png")
            result["final_url"] = page.url
            print(json.dumps(result, indent=2))

            # Hand the browser over. No checkout, no payment: that is yours.
            page.wait_for_timeout(max(0, args.keep_open) * 1000)
            return 0
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())

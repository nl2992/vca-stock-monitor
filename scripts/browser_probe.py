#!/usr/bin/env python3
"""Read-only browser probe for the VCARD35600 product page.

Launches Chromium, renders the page, evaluates the inspection script and
prints the raw DOM evidence plus the classification. Sends no notifications of
any kind, so it is safe to run in CI.

Exit codes: 0 for a conclusive verdict (AVAILABLE or NOT_AVAILABLE), 2 for
INCONCLUSIVE. A conclusive verdict passes whatever the CTA or price happens to
be, so a genuine availability change does not fail the probe.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vca_monitor.availability import classify  # noqa: E402
from vca_monitor.browser import inspect_product_page  # noqa: E402
from vca_monitor.config import PRODUCT_REFERENCE, PRODUCT_URL  # noqa: E402
from vca_monitor.models import Availability  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only VCA product page probe")
    parser.add_argument("--headed", action="store_true", help="run Chromium headed")
    parser.add_argument("--json-out", help="also write the evidence to this file")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )

    print(f"probe target : {PRODUCT_URL}")
    print(f"reference    : {PRODUCT_REFERENCE}")

    observation = inspect_product_page(PRODUCT_URL, headless=not args.headed)
    verdict = classify(observation)

    evidence = {
        "classification": verdict.state.value,
        "reason": verdict.reason,
        "navigation_ok": observation.error is None,
        "resolved_url": observation.url,
        "page_title": observation.page_title,
        "page_timestamp_utc": observation.timestamp,
        "reference_found": observation.reference_found,
        "title_material_match": observation.title_match,
        "controls_found": observation.controls_found,
        "data_show_cta": observation.cta_value,
        "add_to_bag_elements": observation.add_to_bag_button_count,
        "visible_add_to_bag": observation.visible_add_to_bag_count,
        "visible_button_texts": observation.visible_button_texts,
        "price_elements_sample": observation.prices,
        "product_price": observation.product_price,
        "price_verified": observation.price_verified,
        "captcha_suspected": observation.captcha_suspected,
        "error": observation.error,
        "duration_seconds": observation.duration_seconds,
        "attempts": observation.attempts,
        "browser_closed": True,
    }

    print("\n=== DOM EVIDENCE ===")
    print(json.dumps(evidence, indent=2, sort_keys=True))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(evidence, indent=2, sort_keys=True))
        print(f"\nevidence written to {args.json_out}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(f"## Browser probe: `{verdict.state.value}`\n\n")
            handle.write(f"{verdict.reason}\n\n")
            handle.write("```json\n" + json.dumps(evidence, indent=2, sort_keys=True) + "\n```\n")

    print(f"\nRESULT: {verdict.state.value} - {verdict.reason}")
    return 0 if verdict.state is not Availability.INCONCLUSIVE else 2


if __name__ == "__main__":
    sys.exit(main())

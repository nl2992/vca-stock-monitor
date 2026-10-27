#!/usr/bin/env python3
"""Turn probe evidence into a GitHub-only alert decision.

Reads the probe's evidence JSON and emits:
  * `state` on GITHUB_OUTPUT, so later workflow steps can branch,
  * a rendered issue body at the path given by --body-out when the state is
    AVAILABLE,
  * a human-readable run summary on GITHUB_STEP_SUMMARY.

Sends nothing itself. The workflow opens the issue with the automatic
GITHUB_TOKEN, so the monitor needs no credentials of any kind.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PRODUCT_URL = (
    "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
    "vcard35600---vintage-alhambra-bracelet-5-motifs.html"
)
REFERENCE = "VCARD35600"
NEW_YORK = ZoneInfo("America/New_York")
PRICE_UNVERIFIED = "Price could not be verified"
MENTION = "@nl2992"

ISSUE_TITLE = "VCA Alhambra bracelet AVAILABLE - Vintage Alhambra 5 motifs Tiger Eye"


def load_evidence(path: Path) -> dict:
    """Missing or unreadable evidence is itself an inconclusive result."""
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        return {
            "classification": "INCONCLUSIVE",
            "reason": f"probe evidence could not be read: {type(exc).__name__}: {exc}",
        }


def price_line(evidence: dict) -> str:
    if evidence.get("price_verified") and evidence.get("product_price"):
        return str(evidence["product_price"])
    return PRICE_UNVERIFIED


def render_issue_body(evidence: dict, *, simulated: bool, now: datetime | None = None) -> str:
    checked = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    cta = evidence.get("data_show_cta") or "(absent)"
    visible = evidence.get("visible_add_to_bag", 0)
    banner = (
        "> **This is a SIMULATED alert triggered manually to test the "
        "notification path. It is not a real stock signal.**\n\n"
        if simulated
        else ""
    )
    return f"""{banner}{MENTION}

**The Vintage Alhambra bracelet, 5 motifs, 18K yellow gold, Tiger Eye
({REFERENCE}) appears to be AVAILABLE for online purchase.**

- **Price:** {price_line(evidence)}
- **Checked at:** {checked.strftime('%A, %B %d, %Y at %I:%M %p %Z')}
- **Product page:** {PRODUCT_URL}

Availability evidence:

- `data-show-cta`: `{cta}`
- Visible Add to Bag buttons: `{visible}`

Stock can change quickly. Please verify availability directly on the
retailer's website. This is an automated availability notification. No
purchase has been made.

Close this issue once you have acted on it. While it stays open, later runs
will not open another one.

<details><summary>Full probe evidence</summary>

```json
{json.dumps(evidence, indent=2, sort_keys=True)}
```

</details>
"""


def render_summary(state: str, evidence: dict, *, simulated: bool) -> str:
    note = " (SIMULATED)" if simulated else ""
    return (
        f"## Stock check: `{state}`{note}\n\n"
        f"{evidence.get('reason', '')}\n\n"
        "```json\n"
        f"{json.dumps(evidence, indent=2, sort_keys=True)}\n"
        "```\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide on the probe evidence")
    parser.add_argument("--evidence", default="probe-evidence.json")
    parser.add_argument("--body-out", default="issue-body.md")
    parser.add_argument(
        "--simulate",
        default="none",
        choices=["none", "available", "inconclusive"],
        help="override the classification to exercise the alert path",
    )
    args = parser.parse_args(argv)

    evidence = load_evidence(Path(args.evidence))
    state = str(evidence.get("classification") or "INCONCLUSIVE")

    simulated = args.simulate != "none"
    if args.simulate == "available":
        state = "AVAILABLE"
    elif args.simulate == "inconclusive":
        state = "INCONCLUSIVE"

    print(f"state={state}{' (SIMULATED)' if simulated else ''}")
    print(f"reason={evidence.get('reason', '')}")

    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handle:
            handle.write(f"state={state}\n")
            handle.write(f"simulated={'true' if simulated else 'false'}\n")

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as handle:
            handle.write(render_summary(state, evidence, simulated=simulated))

    if state == "AVAILABLE":
        Path(args.body_out).write_text(
            render_issue_body(evidence, simulated=simulated)
        )
        print(f"issue body written to {args.body_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

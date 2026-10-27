# VCA stock monitor — VCARD35600

Unattended, read-only availability monitor for one product page:

**Vintage Alhambra bracelet, 5 motifs, 18K yellow gold, Tiger Eye** (`VCARD35600`)
<https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/vcard35600---vintage-alhambra-bracelet-5-motifs.html>

It renders the page in Chromium, reads the button the site chooses to show,
classifies availability deterministically, and on a confirmed change to
AVAILABLE emails both recipients once and sends a push notification. It never
signs in, adds to bag, reserves, or buys anything.

## Current status

| Capability | State |
|---|---|
| Implemented | yes |
| Locally tested | yes — 115 tests, incl. the real inspection script in Chromium against 11 HTML fixtures |
| Cloud-browser tested | yes — 3 consecutive GitHub-hosted Chromium probes of the live page, all conclusive |
| Notification tested | no — no credentials are configured, so no channel can fire |
| Production-ready | no — awaiting Phase C |
| Production-active | no — the hourly schedule is commented out |

### Cloud browser proof (Phase B)

Three read-only `workflow_dispatch` runs against the live product page, with
notifications disabled and no secrets configured:

| Run | Result | `data-show-cta` | Add to Bag elements / visible | Price | Duration | Attempts |
|---|---|---|---|---|---|---|
| [40427111819](https://github.com/nl2992/vca-stock-monitor/actions/runs/40427111819) | `NOT_AVAILABLE` | `order-by-phone` | 2 / 0 | $5,850 (verified) | 7.09 s | 1 |
| [40427272392](https://github.com/nl2992/vca-stock-monitor/actions/runs/40427272392) | `NOT_AVAILABLE` | `order-by-phone` | 2 / 0 | $5,850 (verified) | 7.24 s | 1 |
| [40427428394](https://github.com/nl2992/vca-stock-monitor/actions/runs/40427428394) | `NOT_AVAILABLE` | `order-by-phone` | 2 / 0 | $5,850 (verified) | 7.48 s | 1 |

Every run verified page identity (title, material and reference `VCARD35600`),
found the product controls, detected no bot wall, needed no retry, and closed
the browser. GitHub-hosted Chromium is **not** blocked by the site.

## How it decides

A single read-only script is evaluated against the rendered DOM. It reads
`data-show-cta` on `.vca-pdp-v2-controls`, counts `.base-checkout-addtocart-button`
elements, and filters them for genuine visibility (geometry plus computed
`display`, `visibility` and `opacity`).

| Verdict | Conditions |
|---|---|
| `AVAILABLE` | Product identity verified **and** (`data-show-cta` contains `add-to-bag`, case-insensitively, **or** at least one visible Add to Bag button). A visible button wins even when the CTA is absent or says `order-by-phone`. |
| `NOT_AVAILABLE` | Identity verified, controls present, `data-show-cta == order-by-phone`, and zero visible buttons. |
| `INCONCLUSIVE` | Navigation, render or JS failure; bot wall; controls missing with no positive signal; unverified product identity; or otherwise unusable signals. |

Access failure is never read as out of stock. Identity requires the reference
code plus product-name and material tokens in the rendered page, so a
purchasable page for a different Alhambra reference cannot trigger an alert.

**Price.** The inspection selector matches every element whose class contains
`price`, which on the live page includes a related-products carousel. A price
is reported as verified only when the first amount in DOM order agrees with
the amount anchored next to the reference code. Otherwise the alert says
"Price could not be verified". No amount is ever invented, and a missing price
never downgrades an otherwise conclusive verdict.

## Notification contract

- **`NOT_AVAILABLE`** — logs only. No email, no push, no other searching.
- **`INCONCLUSIVE`** — Pushover only, titled `VCA STOCK CHECK INCONCLUSIVE`,
  carrying the technical reason and the statements "No stock availability claim
  was made." and "No purchase was attempted." Never an email.
- **`AVAILABLE`** — the Gmail `SENT` label is checked for the exact alert
  subject within the preceding six hours. Subject equality is compared exactly
  in-process and send times come from each message's `internalDate`, not from a
  Gmail query string. A duplicate suppresses both channels. A *failed* lookup
  withholds the email and reports the verification failure by push; it is never
  treated as "no duplicate found". Otherwise one email goes to both recipients
  together, followed by a push titled `VCA BRACELET AVAILABLE` stating the
  actual Gmail outcome.
- **Workflow failure** — a separate push titled `VCA MONITOR WORKFLOW FAILURE`
  fires when dependency install, Chromium install or the test suite breaks, so
  a silent CI failure cannot be mistaken for "not in stock".

## Modes

| Mode | Browser | Messages |
|---|---|---|
| `dry-run` (default) | real page or a fixture | none at all |
| `staging` | synthetic observations | clearly-marked `[TEST - NOT A REAL STOCK ALERT]` content, only to `STAGING_RECIPIENT` |
| `production` | real page | live alerts under the rules above |

An unrecognised or empty mode resolves to `dry-run`, so a misconfigured
workflow cannot send live alerts.

## Local use

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
```

```bash
# Full test suite (unit + real Chromium against local fixtures)
.venv/bin/python -m pytest

# Read-only probe of the live page; sends nothing. Exit 0 = conclusive, 2 = inconclusive.
.venv/bin/python scripts/browser_probe.py

# Classify a local fixture end to end
PYTHONPATH=src .venv/bin/python -m vca_monitor.monitor --mode dry-run \
  --fixture tests/fixtures/available.html
```

## Manual cloud runs

Actions → **VCA stock monitor** → Run workflow. Inputs:

- `mode` — `dry-run` (default), `staging`, or `production`.
- `probe_only` — `true` (default) runs the read-only probe and sends nothing;
  `false` runs the monitor for the selected mode.

Probe evidence is uploaded as the `probe-evidence-<run id>` artifact and
summarised on the job page.

## Secrets

Repository → Settings → Secrets and variables → Actions. Nothing is read from
committed files, and no secret is ever logged or printed.

| Secret | Purpose |
|---|---|
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` | OAuth 2.0 desktop client |
| `GMAIL_REFRESH_TOKEN` | Long-lived grant, exchanged for an access token per run |
| `GMAIL_SENDER` | The sending Gmail address |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | Pushover application and user keys |
| `STAGING_RECIPIENT` | The single approved destination for staging tests |

Gmail scopes are deliberately narrow: `gmail.send` to send, and
`gmail.metadata` to read headers in `SENT`. No message bodies are readable, and
no account password is used. Note that `gmail.metadata` does not permit
server-side search, which is why duplicate detection lists `SENT` and compares
subjects and timestamps locally.

## Scheduling

The hourly schedule is **not active**. To activate it after approval,
uncomment these lines in `.github/workflows/stock-monitor.yml`:

```yaml
  # schedule:
  #   - cron: "17 * * * *"
```

To pause it again, comment them out (or disable the workflow from the Actions
tab). GitHub's cron is best-effort and can be delayed under load, so minute 17
is a target rather than a guarantee.

A `concurrency` group (`vca-stock-monitor`, `cancel-in-progress: false`) keeps
runs serialised, and an advisory lock guards the delivery section on a single
runner. Cross-run idempotency rests on the Gmail six-hour lookback.

## Known limitations

- **Zero-size buttons.** The specified visibility predicate treats a 0×0
  element as visible, because `getClientRects()` returns a rect for it. The
  live page hides its buttons with `display:none`, which the predicate does
  catch, so this has not produced a false positive. Recorded in
  `tests/fixtures/zero_size_button.html` and asserted in the test suite.
- **Delivery-timeout ambiguity.** If a Gmail send times out after the API has
  accepted the message, this run reports failure while the mail may in fact
  have been delivered. The next run's six-hour lookback resolves it only if the
  message actually landed in `SENT`; if it did not, a second alert may be sent.
  The window for this is one run.
- **Cron precision.** Scheduled runs can drift by minutes to tens of minutes.
- **Site terms.** This performs ordinary read-only page loads at a low rate and
  does not evade bot protection. If the retailer blocks automated access, the
  monitor reports `INCONCLUSIVE` rather than working around it.

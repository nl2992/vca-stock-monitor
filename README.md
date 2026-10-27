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
| Notification tested | yes — the alert path was exercised with a simulated AVAILABLE run |
| Production-ready | yes |
| Production-active | yes — every 15 minutes |

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

## How you get told

The monitor runs entirely on GitHub. There are no credentials, no secrets, no
OAuth and no third-party services. Alerting uses GitHub's own notification
emails:

| Verdict | What happens |
|---|---|
| `NOT_AVAILABLE` | The run passes quietly. Nothing is sent. |
| `AVAILABLE` | An issue is opened mentioning `@nl2992` with the price, the New York timestamp, the evidence and the product link, and then the run fails. You get an email for the mention and another for the failed run. |
| `INCONCLUSIVE` | No issue, and no availability claim. The run fails, so you still get an email telling you the monitor could not reach a conclusion. |

Failing the run on purpose is what makes this work without credentials: GitHub
emails the repository owner on a failed workflow run by default. It also means
"the monitor is broken" can never quietly pass for "not in stock", which is the
failure mode that matters most here.

**Duplicate suppression.** While an issue labelled `vca-in-stock` is open,
later runs see it and do not open another or re-notify. Close the issue once
you have acted, and the monitor is armed again.

**Testing the alert path.** Run the workflow manually with `simulate` set to
`available`. It opens a `[TEST]`-prefixed issue with a banner saying the signal
is simulated, so you can confirm the notification reaches you without waiting
for real stock.

## Scheduling

The schedule is **active**: `cron: "7,22,37,52 * * * *"`, roughly every 15 minutes, deliberately off the contended :00/:15/:30/:45 boundaries.

GitHub's scheduler is best-effort. Runs are commonly several minutes late and
an interval is occasionally skipped altogether, more so on low-traffic private
repositories, so the real-world cadence is closer to every 15 to 40 minutes.

At this interval the workflow uses roughly 2,880 Actions minutes per month,
which exceeds the 2,000 free minutes included for private repositories. Making
the repository public removes the cost entirely, since public repositories get
unlimited free Actions minutes, and this repository contains no secrets by
design.

Pause it:

```bash
gh workflow disable "VCA stock monitor" --repo nl2992/vca-stock-monitor
```

Resume it:

```bash
gh workflow enable "VCA stock monitor" --repo nl2992/vca-stock-monitor
```

A `concurrency` group keeps runs serialised and never cancels one mid-flight.

## Unused modules

`src/vca_monitor/gmail_client.py`, `notifications.py` and `monitor.py`
implement direct Gmail and Pushover delivery with a six-hour Sent-folder
duplicate check. They are fully tested but **not** on the live path, which
needs no credentials. They are kept because they are the route to real email
and push alerts if GitHub notifications ever prove too slow or too noisy; see
the `wip/email-only-fallback` branch for the health-notice work.

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

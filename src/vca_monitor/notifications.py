"""Message composition and Pushover delivery.

Composition is pure so the exact wording can be asserted in tests; delivery is
one small function around the Pushover HTTP API. Staging output is prefixed so
a test message can never be mistaken for a real stock alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import (
    AVAILABILITY_SUBJECT,
    DISPLAY_TIMEZONE,
    PRODUCT_REFERENCE,
    PRODUCT_URL,
    Mode,
)
from .models import Observation

log = logging.getLogger(__name__)

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
PUSHOVER_TIMEOUT_S = 20

TEST_PREFIX = "[TEST - NOT A REAL STOCK ALERT] "
PRICE_UNVERIFIED = "Price could not be verified"

AVAILABLE_PUSH_TITLE = "VCA BRACELET AVAILABLE"
INCONCLUSIVE_PUSH_TITLE = "VCA STOCK CHECK INCONCLUSIVE"
WORKFLOW_FAILURE_PUSH_TITLE = "VCA MONITOR WORKFLOW FAILURE"

NO_CLAIM_LINES = (
    "No stock availability claim was made.",
    "No purchase was attempted.",
)


@dataclass(frozen=True)
class PushResult:
    ok: bool
    detail: str


def format_checked_at(moment: datetime | None = None) -> str:
    """Human-readable New York timestamp for the alert body."""
    reference = moment or datetime.now(DISPLAY_TIMEZONE)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=DISPLAY_TIMEZONE)
    local = reference.astimezone(DISPLAY_TIMEZONE)
    return local.strftime("%A, %B %d, %Y at %I:%M %p %Z")


def price_for_display(observation: Observation) -> str:
    """Only a verified price is ever stated; nothing is invented."""
    if observation.price_verified and observation.product_price:
        return observation.product_price
    return PRICE_UNVERIFIED


def subject_for(mode: Mode) -> str:
    """The production subject is exact; other modes are clearly marked TEST."""
    if mode is Mode.PRODUCTION:
        return AVAILABILITY_SUBJECT
    return TEST_PREFIX + AVAILABILITY_SUBJECT


def build_availability_email(
    observation: Observation,
    *,
    mode: Mode = Mode.PRODUCTION,
    moment: datetime | None = None,
) -> str:
    """Compose the availability email body from the agreed template."""
    body = f"""VAN CLEEF & ARPELS — STOCK ALERT

The Vintage Alhambra bracelet, 5 motifs, in 18K yellow gold with Tiger Eye
({PRODUCT_REFERENCE}) appears to be AVAILABLE for online purchase.

Price: {price_for_display(observation)}
Checked at: {format_checked_at(moment)}
Product URL: {PRODUCT_URL}

Availability evidence:
- data-show-cta: {observation.cta_value if observation.cta_value is not None else "(absent)"}
- Visible Add to Bag buttons: {observation.visible_add_to_bag_count}

Stock can change quickly. Please verify availability directly on the retailer's website.
This is an automated availability notification. No purchase has been made.
"""
    if mode is not Mode.PRODUCTION:
        return (
            f"{TEST_PREFIX.strip()}\n"
            f"This is a synthetic {mode.value} message. The content below is "
            "generated from test data and makes no claim about real stock.\n\n"
            + body
        )
    return body


def build_availability_push(
    observation: Observation,
    *,
    email_status: str,
    mode: Mode = Mode.PRODUCTION,
    moment: datetime | None = None,
) -> str:
    """Push body for a confirmed availability verdict.

    `email_status` is passed in rather than guessed, so the push always states
    what actually happened to the email.
    """
    lines = [
        f"{PRODUCT_REFERENCE} - Vintage Alhambra bracelet, 5 motifs, Tiger Eye",
        f"Price: {price_for_display(observation)}",
        f"Checked at: {format_checked_at(moment)}",
        f"Evidence: data-show-cta="
        f"{observation.cta_value if observation.cta_value is not None else '(absent)'}, "
        f"visible Add to Bag buttons={observation.visible_add_to_bag_count}",
        f"Email: {email_status}",
        PRODUCT_URL,
    ]
    prefix = "" if mode is Mode.PRODUCTION else TEST_PREFIX
    return prefix + "\n".join(lines)


def build_inconclusive_push(reason: str, *, mode: Mode = Mode.PRODUCTION) -> str:
    """Push body for an inconclusive check. Never claims anything about stock."""
    prefix = "" if mode is Mode.PRODUCTION else TEST_PREFIX
    lines = [
        f"Reference: {PRODUCT_REFERENCE}",
        f"Technical reason: {reason}",
        *NO_CLAIM_LINES,
    ]
    return prefix + "\n".join(lines)


def build_workflow_failure_push(stage: str, detail: str) -> str:
    """Push body for a failure before or around the application itself."""
    return "\n".join(
        [
            f"Reference: {PRODUCT_REFERENCE}",
            f"Failed stage: {stage}",
            f"Detail: {detail}",
            "The stock check did not complete.",
            *NO_CLAIM_LINES,
        ]
    )


def send_push(
    token: str | None,
    user: str | None,
    title: str,
    message: str,
    *,
    url: str | None = PRODUCT_URL,
    priority: int = 1,
    _poster: Any = None,
) -> PushResult:
    """Deliver one Pushover notification.

    Returns a result instead of raising, so a push failure is recorded
    explicitly rather than masking the stock verdict.
    """
    if not (token and user):
        return PushResult(False, "Pushover is not configured; push skipped")

    payload = {
        "token": token,
        "user": user,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "Open product page"

    poster = _poster
    if poster is None:
        import requests  # noqa: PLC0415

        poster = requests.post

    try:
        response = poster(PUSHOVER_ENDPOINT, data=payload, timeout=PUSHOVER_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        return PushResult(False, f"Pushover request failed: {type(exc).__name__}: {exc}")

    status = getattr(response, "status_code", None)
    if status != 200:
        return PushResult(False, f"Pushover returned HTTP {status}")
    return PushResult(True, "Pushover accepted the notification")

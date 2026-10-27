"""Configuration: product facts, timeouts, and mode/credential handling.

Product facts are constants, not settings, so a typo in an environment
variable can never point the monitor at a different page. Everything that is
secret is read from the environment and never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

# --- Product under watch (constants on purpose) -----------------------------

PRODUCT_URL = (
    "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
    "vcard35600---vintage-alhambra-bracelet-5-motifs.html"
)
PRODUCT_REFERENCE = "VCARD35600"
PRODUCT_NAME = "Vintage Alhambra bracelet, 5 motifs"
PRODUCT_DESCRIPTION = (
    "Vintage Alhambra bracelet, 5 motifs, 18K yellow gold, Tiger Eye"
)
RETAILER = "Van Cleef & Arpels"

# Tokens that must all appear (case-insensitively) somewhere in the rendered
# page for product identity to count as verified.
IDENTITY_TITLE_TOKENS = ("vintage alhambra", "bracelet")
IDENTITY_MATERIAL_TOKENS = ("tiger eye", "tiger's eye", "tigers eye")

DISPLAY_TIMEZONE = ZoneInfo("America/New_York")

# Recipients are read from the environment, never committed: this repository is
# public, and publishing anyone's address (least of all a third party's) invites
# scraping. Set VCA_EMAIL_RECIPIENTS to a comma-separated list, or as a GitHub
# secret, if the direct-email path is ever enabled. The live GitHub-only
# alerting path does not use this at all.
EMAIL_RECIPIENTS: tuple[str, ...] = tuple(
    address.strip()
    for address in (os.environ.get("VCA_EMAIL_RECIPIENTS") or "").split(",")
    if address.strip()
)
AVAILABILITY_SUBJECT = (
    "VCA Alhambra bracelet AVAILABLE - Vintage Alhambra 5 motifs Tiger Eye"
)
DUPLICATE_LOOKBACK_HOURS = 6

# --- Browser timing (seconds) ----------------------------------------------

NAV_TIMEOUT_S = 30
RENDER_SETTLE_S = 5
CONTROLS_TIMEOUT_S = 10
MAX_ATTEMPTS = 2  # one bounded retry, no uncontrolled loops

# Strings that suggest a bot wall rather than the product page.
CAPTCHA_MARKERS = (
    "captcha",
    "are you a human",
    "access denied",
    "unusual traffic",
    "pardon our interruption",
    "request unsuccessful",
    "bot detection",
    "incident id",
)


class Mode(str, Enum):
    """Execution mode. Only PRODUCTION may send a real availability alert."""

    DRY_RUN = "dry-run"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True)
class Secrets:
    """Credentials pulled from the environment. Never logged or serialised."""

    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    gmail_refresh_token: str | None = None
    gmail_sender: str | None = None
    pushover_token: str | None = None
    pushover_user: str | None = None
    staging_recipient: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Secrets":
        e = os.environ if env is None else env

        def get(key: str) -> str | None:
            value = (e.get(key) or "").strip()
            return value or None

        return cls(
            gmail_client_id=get("GMAIL_CLIENT_ID"),
            gmail_client_secret=get("GMAIL_CLIENT_SECRET"),
            gmail_refresh_token=get("GMAIL_REFRESH_TOKEN"),
            gmail_sender=get("GMAIL_SENDER"),
            pushover_token=get("PUSHOVER_TOKEN"),
            pushover_user=get("PUSHOVER_USER"),
            staging_recipient=get("STAGING_RECIPIENT"),
        )

    @property
    def gmail_configured(self) -> bool:
        return all(
            (
                self.gmail_client_id,
                self.gmail_client_secret,
                self.gmail_refresh_token,
                self.gmail_sender,
            )
        )

    @property
    def pushover_configured(self) -> bool:
        return bool(self.pushover_token and self.pushover_user)

    def __repr__(self) -> str:  # pragma: no cover - defensive against leaks
        return (
            f"Secrets(gmail_configured={self.gmail_configured}, "
            f"pushover_configured={self.pushover_configured})"
        )


def resolve_mode(raw: str | None) -> Mode:
    """Parse a mode string, defaulting to the safest mode.

    Anything unrecognised (including None or an empty value) resolves to
    dry-run so a misconfigured workflow cannot send live alerts.
    """
    value = (raw or "").strip().lower()
    for mode in Mode:
        if value == mode.value:
            return mode
    return Mode.DRY_RUN

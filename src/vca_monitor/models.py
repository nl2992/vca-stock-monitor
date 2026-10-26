"""Data structures shared across the monitor.

Two value objects only: what the browser observed (`Observation`) and what the
classifier concluded from it (`Verdict`). Both are frozen so a verdict can never
be edited after classification, and neither carries credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class Observation:
    """One read-only snapshot of the rendered product page.

    Mirrors the JSON returned by the page-evaluation script, plus the identity
    and price fields derived from the page by `browser.py`. `error` is set only
    when the snapshot could not be taken at all.
    """

    url: str = ""
    page_title: str = ""
    timestamp: str = ""
    controls_found: bool = False
    cta_value: str | None = None
    add_to_bag_button_count: int = 0
    visible_add_to_bag_count: int = 0
    visible_button_texts: list[str] = field(default_factory=list)
    prices: list[str] = field(default_factory=list)
    # Derived by browser.py from the rendered DOM.
    reference_found: bool = False
    title_match: bool = False
    product_price: str | None = None
    price_verified: bool = False
    captcha_suspected: bool = False
    error: str | None = None
    duration_seconds: float | None = None
    attempts: int = 1

    @classmethod
    def from_page_payload(cls, payload: dict[str, Any], **extra: Any) -> "Observation":
        """Build an Observation from the raw page-evaluation payload."""
        return cls(
            url=payload.get("url") or "",
            page_title=payload.get("pageTitle") or "",
            timestamp=payload.get("timestamp") or "",
            controls_found=bool(payload.get("controlsFound")),
            cta_value=payload.get("ctaValue"),
            add_to_bag_button_count=int(payload.get("addToBagButtonCount") or 0),
            visible_add_to_bag_count=int(payload.get("visibleAddToBagCount") or 0),
            visible_button_texts=list(payload.get("visibleButtonTexts") or []),
            prices=list(payload.get("prices") or []),
            **extra,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def log_line(self) -> str:
        """Single-line evidence summary. Contains no credentials by construction."""
        return json.dumps(
            {
                "controls_found": self.controls_found,
                "cta_value": self.cta_value,
                "buttons_total": self.add_to_bag_button_count,
                "buttons_visible": self.visible_add_to_bag_count,
                "reference_found": self.reference_found,
                "title_match": self.title_match,
                "price": self.product_price,
                "price_verified": self.price_verified,
                "captcha_suspected": self.captcha_suspected,
                "error": self.error,
                "duration_s": self.duration_seconds,
                "attempts": self.attempts,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class Verdict:
    """The classifier's conclusion plus the reason it reached it."""

    state: Availability
    reason: str
    observation: Observation

    @property
    def is_available(self) -> bool:
        return self.state is Availability.AVAILABLE

    @property
    def is_inconclusive(self) -> bool:
        return self.state is Availability.INCONCLUSIVE

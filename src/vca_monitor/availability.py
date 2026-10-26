"""Deterministic availability classifier.

Pure functions over an `Observation`: no I/O, no clock, no network, so the
same observation always yields the same verdict. The ordering of the checks is
the contract:

1. A failed snapshot is never evidence about stock.
2. A page whose identity is unverified is never evidence about stock.
3. Positive button evidence outranks a negative CTA.
4. Only the fully-formed negative pattern counts as NOT_AVAILABLE.
5. Anything left over is INCONCLUSIVE.
"""

from __future__ import annotations

import re

from .config import (
    CAPTCHA_MARKERS,
    IDENTITY_MATERIAL_TOKENS,
    IDENTITY_TITLE_TOKENS,
    PRODUCT_REFERENCE,
)
from .models import Availability, Observation, Verdict

ADD_TO_BAG_CTA = "add-to-bag"
ORDER_BY_PHONE_CTA = "order-by-phone"

# A USD amount with at least three digits, e.g. "$5,850" or "$5,850.00".
_PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\$\s?\d{3,}(?:\.\d{2})?")


def normalise_cta(cta: str | None) -> str:
    return (cta or "").strip().lower()


def has_add_to_bag_cta(observation: Observation) -> bool:
    """True when the CTA attribute itself advertises add-to-bag (any casing)."""
    return ADD_TO_BAG_CTA in normalise_cta(observation.cta_value)


def has_visible_button(observation: Observation) -> bool:
    return observation.visible_add_to_bag_count > 0


def is_order_by_phone_cta(observation: Observation) -> bool:
    return normalise_cta(observation.cta_value) == ORDER_BY_PHONE_CTA


def looks_like_captcha(page_text: str) -> bool:
    """True when the rendered text matches a known bot-wall marker."""
    lowered = (page_text or "").lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def identity_verified(observation: Observation) -> bool:
    """The page must prove it is this product before stock signals mean anything."""
    return observation.reference_found and observation.title_match


def check_identity_tokens(page_text: str, title: str = "") -> tuple[bool, bool]:
    """Return (reference_found, title_match) for rendered page text.

    `title_match` requires both a product-name token set and a material token,
    so a different Alhambra bracelet does not pass as this one.
    """
    haystack = f"{title}\n{page_text}".lower()
    reference_found = PRODUCT_REFERENCE.lower() in haystack
    title_ok = all(token in haystack for token in IDENTITY_TITLE_TOKENS)
    material_ok = any(token in haystack for token in IDENTITY_MATERIAL_TOKENS)
    return reference_found, (title_ok and material_ok)


def extract_product_price(
    prices: list[str], page_text: str = "", reference: str = PRODUCT_REFERENCE
) -> tuple[str | None, bool]:
    """Isolate this product's price from the page's price elements.

    The selector in the inspection script matches every element whose class
    contains "price", which on the live page includes a related-products
    carousel. Two independent signals are therefore required before a price is
    reported as verified:

    1. the first USD amount in DOM order among the price elements, which is the
       product's own price block (the carousel renders after it); and
    2. the first USD amount appearing just after the reference code in the
       rendered text.

    A price is verified when those agree, or when every distinct amount among
    the price elements is the same. Otherwise the best candidate is returned
    with `verified=False` and the caller states that the price could not be
    verified. No amount is ever invented.
    """
    candidates: list[str] = []
    for raw in prices or []:
        candidates.extend(
            match.group(0).replace(" ", "") for match in _PRICE_RE.finditer(raw or "")
        )

    distinct = set(candidates)
    primary = candidates[0] if candidates else None
    corroboration = _price_near_reference(page_text, reference)

    # A contradicting reference-anchored amount always defeats verification,
    # including when the price elements agree among themselves: agreement
    # between carousel entries is not evidence about this product.
    if primary and corroboration:
        if primary == corroboration:
            return primary, True
        return corroboration, False

    if len(distinct) == 1:
        return distinct.pop(), True

    if primary or corroboration:
        return (primary or corroboration), False

    return None, False


def _price_near_reference(page_text: str, reference: str) -> str | None:
    if not page_text:
        return None
    index = page_text.lower().find(reference.lower())
    if index < 0:
        return None
    window = page_text[index : index + 400]
    match = _PRICE_RE.search(window)
    return match.group(0).replace(" ", "") if match else None


def classify(observation: Observation) -> Verdict:
    """Map an observation onto exactly one availability state."""

    if observation.error:
        return Verdict(
            Availability.INCONCLUSIVE,
            f"browser inspection failed: {observation.error}",
            observation,
        )

    if observation.captcha_suspected:
        return Verdict(
            Availability.INCONCLUSIVE,
            "bot check or access wall served instead of the product page",
            observation,
        )

    if not identity_verified(observation):
        missing = []
        if not observation.reference_found:
            missing.append(f"reference {PRODUCT_REFERENCE} not present in rendered page")
        if not observation.title_match:
            missing.append("product name/material tokens not matched")
        return Verdict(
            Availability.INCONCLUSIVE,
            "product identity unverified: " + "; ".join(missing),
            observation,
        )

    # Positive evidence wins, even against an order-by-phone or absent CTA.
    if has_visible_button(observation):
        return Verdict(
            Availability.AVAILABLE,
            (
                f"{observation.visible_add_to_bag_count} visible Add to Bag "
                f"button(s) on the verified product page "
                f"(data-show-cta={observation.cta_value!r})"
            ),
            observation,
        )

    if has_add_to_bag_cta(observation):
        return Verdict(
            Availability.AVAILABLE,
            (
                f"data-show-cta={observation.cta_value!r} advertises add-to-bag "
                "on the verified product page"
            ),
            observation,
        )

    if not observation.controls_found:
        return Verdict(
            Availability.INCONCLUSIVE,
            "product controls (.vca-pdp-v2-controls) absent and no positive "
            "availability signal; cannot distinguish sold out from a render failure",
            observation,
        )

    if is_order_by_phone_cta(observation) and observation.visible_add_to_bag_count == 0:
        return Verdict(
            Availability.NOT_AVAILABLE,
            (
                "verified product page with controls present, "
                f"data-show-cta={ORDER_BY_PHONE_CTA!r} and no visible Add to Bag button"
            ),
            observation,
        )

    return Verdict(
        Availability.INCONCLUSIVE,
        (
            f"unusable availability signals: data-show-cta={observation.cta_value!r}, "
            f"visible Add to Bag buttons={observation.visible_add_to_bag_count}"
        ),
        observation,
    )

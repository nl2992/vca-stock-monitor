"""Classifier unit tests: one test per rule and per boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vca_monitor.availability import (
    classify,
    extract_product_price,
    has_add_to_bag_cta,
    identity_verified,
    looks_like_captcha,
    check_identity_tokens,
)
from vca_monitor.models import Availability, Observation


# --- AVAILABLE --------------------------------------------------------------

@pytest.mark.parametrize("cta", ["add-to-bag", "ADD-TO-BAG", "Add-To-Bag", "cta add-to-bag now"])
def test_add_to_bag_cta_is_available_regardless_of_case(verified_observation, cta):
    verdict = classify(replace(verified_observation, cta_value=cta))
    assert verdict.state is Availability.AVAILABLE


def test_visible_button_with_phone_cta_is_available(verified_observation):
    """Positive button evidence outranks an order-by-phone CTA."""
    verdict = classify(replace(verified_observation, visible_add_to_bag_count=1))
    assert verdict.state is Availability.AVAILABLE
    assert "visible Add to Bag" in verdict.reason


def test_visible_button_with_missing_cta_is_available(verified_observation):
    verdict = classify(
        replace(verified_observation, cta_value=None, visible_add_to_bag_count=2)
    )
    assert verdict.state is Availability.AVAILABLE


def test_available_survives_unverified_price(verified_observation):
    """A missing price never invalidates an otherwise conclusive verdict."""
    verdict = classify(
        replace(
            verified_observation,
            cta_value="add-to-bag",
            product_price=None,
            price_verified=False,
        )
    )
    assert verdict.state is Availability.AVAILABLE


# --- NOT_AVAILABLE ----------------------------------------------------------

def test_phone_cta_with_hidden_buttons_is_not_available(verified_observation):
    verdict = classify(verified_observation)
    assert verdict.state is Availability.NOT_AVAILABLE
    assert "order-by-phone" in verdict.reason


def test_not_available_requires_controls(verified_observation):
    verdict = classify(replace(verified_observation, controls_found=False))
    assert verdict.state is Availability.INCONCLUSIVE


# --- INCONCLUSIVE -----------------------------------------------------------

def test_browser_error_is_inconclusive():
    verdict = classify(Observation(error="TimeoutError: navigation timed out"))
    assert verdict.state is Availability.INCONCLUSIVE
    assert "browser inspection failed" in verdict.reason


def test_captcha_is_inconclusive(verified_observation):
    verdict = classify(replace(verified_observation, captcha_suspected=True))
    assert verdict.state is Availability.INCONCLUSIVE
    assert "bot check" in verdict.reason


def test_wrong_product_is_inconclusive_even_with_add_to_bag(verified_observation):
    """A purchasable page for another reference must never read as AVAILABLE."""
    verdict = classify(
        replace(
            verified_observation,
            reference_found=False,
            cta_value="add-to-bag",
            visible_add_to_bag_count=1,
        )
    )
    assert verdict.state is Availability.INCONCLUSIVE
    assert "identity unverified" in verdict.reason


def test_title_mismatch_is_inconclusive(verified_observation):
    verdict = classify(replace(verified_observation, title_match=False))
    assert verdict.state is Availability.INCONCLUSIVE


def test_missing_controls_without_positive_signal_is_inconclusive(verified_observation):
    verdict = classify(
        replace(verified_observation, controls_found=False, cta_value=None,
                add_to_bag_button_count=0)
    )
    assert verdict.state is Availability.INCONCLUSIVE
    assert "cannot distinguish sold out" in verdict.reason


def test_unknown_cta_with_no_buttons_is_inconclusive(verified_observation):
    verdict = classify(replace(verified_observation, cta_value="coming-soon"))
    assert verdict.state is Availability.INCONCLUSIVE
    assert "unusable availability signals" in verdict.reason


def test_empty_cta_with_no_buttons_is_inconclusive(verified_observation):
    verdict = classify(replace(verified_observation, cta_value=""))
    assert verdict.state is Availability.INCONCLUSIVE


def test_access_failure_never_implies_out_of_stock():
    """An unreachable page is INCONCLUSIVE, never NOT_AVAILABLE."""
    verdict = classify(Observation(error="net::ERR_CONNECTION_REFUSED"))
    assert verdict.state is not Availability.NOT_AVAILABLE


# --- helpers ----------------------------------------------------------------

def test_identity_requires_both_signals():
    assert not identity_verified(Observation(reference_found=True, title_match=False))
    assert not identity_verified(Observation(reference_found=False, title_match=True))
    assert identity_verified(Observation(reference_found=True, title_match=True))


def test_check_identity_tokens_accepts_the_real_product():
    found, matched = check_identity_tokens(
        "Vintage Alhambra bracelet, 5 motifs, 18K yellow gold, Tiger Eye VCARD35600"
    )
    assert found and matched


def test_check_identity_tokens_rejects_a_different_stone():
    found, matched = check_identity_tokens(
        "Vintage Alhambra bracelet, 5 motifs, Carnelian VCARB12345"
    )
    assert not found and not matched


def test_has_add_to_bag_cta_handles_none():
    assert not has_add_to_bag_cta(Observation(cta_value=None))


@pytest.mark.parametrize(
    "text", ["Please complete the CAPTCHA", "Access Denied", "Pardon Our Interruption"]
)
def test_captcha_markers(text):
    assert looks_like_captcha(text)


def test_captcha_detector_ignores_normal_product_copy():
    assert not looks_like_captcha("Vintage Alhambra bracelet, 5 motifs, Tiger Eye")


# --- price isolation --------------------------------------------------------

def test_single_agreeing_price_is_verified():
    price, verified = extract_product_price(["$5,850", "$5,850 excluding taxes"])
    assert (price, verified) == ("$5,850", True)


def test_disagreeing_prices_are_not_verified():
    price, verified = extract_product_price(["$5,850", "$12,300", "$3,750"])
    assert verified is False


def test_related_product_prices_do_not_override_the_product_price():
    """Two independent signals agree, so the carousel does not spoil the price."""
    price, verified = extract_product_price(
        ["$5,850", "$12,300"], page_text="Reference: VCARD35600 Price $5,850 excluding taxes"
    )
    assert price == "$5,850"
    assert verified is True


def test_price_is_unverified_when_the_two_signals_disagree():
    price, verified = extract_product_price(
        ["$9,999", "$12,300"], page_text="Reference: VCARD35600 Price $5,850"
    )
    assert verified is False
    assert price == "$5,850"  # the reference-anchored amount is preferred


def test_agreeing_price_elements_cannot_outvote_the_reference_anchor():
    """A single carousel price must not be verified against a different
    reference-anchored amount."""
    price, verified = extract_product_price(
        ["$12,300"], page_text="Reference: VCARD35600 bracelet $5,850 excluding taxes"
    )
    assert verified is False
    assert price == "$5,850"


def test_live_page_price_shape_is_verified():
    """Mirrors the real page: product price first, then a related carousel."""
    price, verified = extract_product_price(
        ["$ 5,850\n Including taxes", "$ 5,850", "$ 3,450", "$ 21,700", "$ 5,200"],
        page_text="Reference VCARD35600 Vintage Alhambra bracelet $ 5,850 Excluding taxes",
    )
    assert (price, verified) == ("$5,850", True)


def test_missing_price_yields_none():
    assert extract_product_price([]) == (None, False)


def test_unrelated_text_is_not_a_price():
    assert extract_product_price(["5 motifs", "18K"]) == (None, False)

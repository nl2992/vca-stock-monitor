"""Runs the real inspection JavaScript in Chromium against local HTML files.

These tests exercise the actual DOM evaluation and visibility logic rather
than a mocked return value, so a change to the selectors or the visibility
rules is caught here.
"""

from __future__ import annotations

import pytest

from vca_monitor.availability import classify
from vca_monitor.browser import INSPECTION_JS, PAGE_TEXT_JS, build_observation
from vca_monitor.models import Availability

pytestmark = pytest.mark.chromium


@pytest.fixture(scope="module")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as instance:
        browser = instance.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def observe_fixture(chromium, fixture_dir, name):
    page = chromium.new_page()
    try:
        page.goto((fixture_dir / name).resolve().as_uri(), wait_until="domcontentloaded")
        payload = page.evaluate(INSPECTION_JS)
        page_text = page.evaluate(PAGE_TEXT_JS) or ""
        return build_observation(payload, page_text)
    finally:
        page.close()


@pytest.mark.parametrize(
    "fixture_name,expected",
    [
        ("available.html", Availability.AVAILABLE),
        ("unavailable.html", Availability.NOT_AVAILABLE),
        ("hidden_button.html", Availability.NOT_AVAILABLE),
        ("missing_controls.html", Availability.INCONCLUSIVE),
        ("contradictory.html", Availability.AVAILABLE),
        ("wrong_reference.html", Availability.INCONCLUSIVE),
        ("captcha.html", Availability.INCONCLUSIVE),
        ("ambiguous_price.html", Availability.AVAILABLE),
        ("no_price.html", Availability.AVAILABLE),
        # Known predicate limitation, documented below.
        ("zero_size_button.html", Availability.AVAILABLE),
        ("conflicting_price.html", Availability.AVAILABLE),
    ],
)
def test_fixture_classification(chromium, fixture_dir, fixture_name, expected):
    observation = observe_fixture(chromium, fixture_dir, fixture_name)
    assert classify(observation).state is expected


def test_available_fixture_evidence(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "available.html")
    assert observation.cta_value == "add-to-bag"
    assert observation.add_to_bag_button_count == 1
    assert observation.visible_add_to_bag_count == 1
    assert observation.visible_button_texts == ["Add to Bag"]
    assert observation.product_price == "$5,850"
    assert observation.price_verified is True


def test_display_none_buttons_count_as_invisible(chromium, fixture_dir):
    """Two button elements exist in the DOM but none is visible."""
    observation = observe_fixture(chromium, fixture_dir, "unavailable.html")
    assert observation.add_to_bag_button_count == 2
    assert observation.visible_add_to_bag_count == 0
    assert observation.cta_value == "order-by-phone"


def test_visibility_hidden_and_zero_opacity_count_as_invisible(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "hidden_button.html")
    assert observation.add_to_bag_button_count == 2
    assert observation.visible_add_to_bag_count == 0


def test_zero_size_button_is_a_known_false_positive(chromium, fixture_dir):
    """Documents a limitation of the specified visibility predicate.

    `getClientRects()` returns a rect even for a 0x0 absolutely-positioned
    element, so such a button passes the `visibleGeometry` test and is counted
    as visible. The live page hides its buttons with `display:none`, which the
    predicate does catch (see unavailable.html), so this has not produced a
    false positive in practice. Asserted here so the behaviour is recorded
    rather than discovered during an alert.
    """
    observation = observe_fixture(chromium, fixture_dir, "zero_size_button.html")
    assert observation.add_to_bag_button_count == 1
    assert observation.visible_add_to_bag_count == 1
    assert classify(observation).state is Availability.AVAILABLE


def test_missing_controls_fixture_reports_no_controls(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "missing_controls.html")
    assert observation.controls_found is False
    assert observation.cta_value is None


def test_wrong_reference_fixture_fails_identity(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "wrong_reference.html")
    assert observation.reference_found is False
    assert observation.visible_add_to_bag_count == 1  # purchasable, but not our product


def test_captcha_fixture_is_flagged(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "captcha.html")
    assert observation.captcha_suspected is True


def test_related_products_carousel_does_not_spoil_the_price(chromium, fixture_dir):
    """The carousel adds three unrelated amounts; the product price still wins."""
    observation = observe_fixture(chromium, fixture_dir, "ambiguous_price.html")
    assert len(observation.prices) >= 3
    assert observation.product_price == "$5,850"
    assert observation.price_verified is True


def test_conflicting_price_signals_are_not_verified(chromium, fixture_dir):
    """With no product price block, the two signals disagree and nothing is claimed."""
    observation = observe_fixture(chromium, fixture_dir, "conflicting_price.html")
    assert observation.price_verified is False
    assert classify(observation).state is Availability.AVAILABLE


def test_uppercase_cta_still_reads_as_available(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "ambiguous_price.html")
    assert observation.cta_value == "ADD-TO-BAG"
    assert classify(observation).state is Availability.AVAILABLE


def test_no_price_fixture_has_no_price(chromium, fixture_dir):
    observation = observe_fixture(chromium, fixture_dir, "no_price.html")
    assert observation.product_price is None
    assert observation.price_verified is False

"""Message composition and Pushover delivery tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from vca_monitor.config import AVAILABILITY_SUBJECT, PRODUCT_URL, Mode
from vca_monitor.models import Observation
from vca_monitor.notifications import (
    AVAILABLE_PUSH_TITLE,
    INCONCLUSIVE_PUSH_TITLE,
    PRICE_UNVERIFIED,
    PUSHOVER_ENDPOINT,
    TEST_PREFIX,
    WORKFLOW_FAILURE_PUSH_TITLE,
    build_availability_email,
    build_availability_push,
    build_inconclusive_push,
    build_workflow_failure_push,
    format_checked_at,
    send_push,
    subject_for,
)
from tests.conftest import RecordingPoster

AT = datetime(2026, 10, 26, 21, 30, tzinfo=timezone.utc)  # 17:30 New York


@pytest.fixture
def available_observation() -> Observation:
    return Observation(
        url=PRODUCT_URL,
        controls_found=True,
        cta_value="add-to-bag",
        add_to_bag_button_count=1,
        visible_add_to_bag_count=1,
        reference_found=True,
        title_match=True,
        product_price="$5,850",
        price_verified=True,
    )


# --- email body -------------------------------------------------------------

def test_email_body_matches_the_agreed_template(available_observation):
    body = build_availability_email(available_observation, moment=AT)
    assert body.startswith("VAN CLEEF & ARPELS")
    assert "(VCARD35600) appears to be AVAILABLE for online purchase." in body
    assert "Price: $5,850" in body
    assert "Checked at: Monday, October 26, 2026 at 05:30 PM EDT" in body
    assert f"Product URL: {PRODUCT_URL}" in body
    assert "- data-show-cta: add-to-bag" in body
    assert "- Visible Add to Bag buttons: 1" in body
    assert "No purchase has been made." in body


def test_unverified_price_is_stated_as_unverifiable_not_invented(available_observation):
    observation = replace(available_observation, product_price="$5,850", price_verified=False)
    body = build_availability_email(observation, moment=AT)
    assert f"Price: {PRICE_UNVERIFIED}" in body
    assert "$5,850" not in body


def test_missing_price_is_stated_as_unverifiable(available_observation):
    observation = replace(available_observation, product_price=None, price_verified=False)
    assert f"Price: {PRICE_UNVERIFIED}" in build_availability_email(observation, moment=AT)


def test_absent_cta_is_rendered_explicitly(available_observation):
    observation = replace(available_observation, cta_value=None, visible_add_to_bag_count=2)
    body = build_availability_email(observation, moment=AT)
    assert "- data-show-cta: (absent)" in body
    assert "- Visible Add to Bag buttons: 2" in body


def test_checked_at_uses_new_york_time():
    assert "EDT" in format_checked_at(AT) or "EST" in format_checked_at(AT)
    assert "05:30 PM" in format_checked_at(AT)


# --- mode safety ------------------------------------------------------------

def test_production_subject_is_exact():
    assert subject_for(Mode.PRODUCTION) == AVAILABILITY_SUBJECT


@pytest.mark.parametrize("mode", [Mode.STAGING, Mode.DRY_RUN])
def test_non_production_subject_is_marked_test(mode):
    subject = subject_for(mode)
    assert subject.startswith(TEST_PREFIX)
    assert "TEST" in subject


def test_staging_email_body_is_unmistakably_a_test(available_observation):
    body = build_availability_email(available_observation, mode=Mode.STAGING, moment=AT)
    assert body.startswith(TEST_PREFIX.strip())
    assert "makes no claim about real stock" in body


# --- push bodies ------------------------------------------------------------

def test_availability_push_reports_the_real_email_status(available_observation):
    message = build_availability_push(
        available_observation, email_status="NOT SENT - Gmail send failed", moment=AT
    )
    assert "Email: NOT SENT - Gmail send failed" in message
    assert "VCARD35600" in message
    assert PRODUCT_URL in message
    assert "visible Add to Bag buttons=1" in message


def test_availability_push_states_unverified_price(available_observation):
    observation = replace(available_observation, price_verified=False)
    message = build_availability_push(observation, email_status="sent", moment=AT)
    assert PRICE_UNVERIFIED in message


def test_inconclusive_push_makes_no_stock_claim():
    message = build_inconclusive_push("navigation timed out after 30s")
    assert "VCARD35600" in message
    assert "navigation timed out after 30s" in message
    assert "No stock availability claim was made." in message
    assert "No purchase was attempted." in message
    assert "AVAILABLE" not in message


def test_workflow_failure_push_is_distinct_from_a_stock_alert():
    message = build_workflow_failure_push("playwright install", "exit code 1")
    assert "Failed stage: playwright install" in message
    assert "The stock check did not complete." in message
    assert "No stock availability claim was made." in message


def test_the_three_push_titles_are_distinct():
    titles = {AVAILABLE_PUSH_TITLE, INCONCLUSIVE_PUSH_TITLE, WORKFLOW_FAILURE_PUSH_TITLE}
    assert len(titles) == 3


# --- delivery ---------------------------------------------------------------

def test_push_posts_to_pushover_with_credentials(poster):
    result = send_push("tok", "usr", "title", "message", _poster=poster)
    assert result.ok
    assert poster.calls[0]["url"] == PUSHOVER_ENDPOINT
    assert poster.calls[0]["data"]["token"] == "tok"
    assert poster.calls[0]["data"]["user"] == "usr"
    assert poster.calls[0]["data"]["url"] == PRODUCT_URL


def test_push_skipped_when_unconfigured(poster):
    result = send_push(None, None, "t", "m", _poster=poster)
    assert result.ok is False
    assert "not configured" in result.detail
    assert poster.calls == []


def test_push_non_200_is_a_failure():
    poster = RecordingPoster(status_code=429)
    result = send_push("tok", "usr", "t", "m", _poster=poster)
    assert result.ok is False
    assert "429" in result.detail


def test_push_network_error_is_reported_not_raised():
    poster = RecordingPoster(raises=TimeoutError("connection timed out"))
    result = send_push("tok", "usr", "t", "m", _poster=poster)
    assert result.ok is False
    assert "connection timed out" in result.detail

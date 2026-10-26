from __future__ import annotations

from pathlib import Path

import pytest

from vca_monitor.config import Secrets
from vca_monitor.models import Observation

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def verified_observation() -> Observation:
    """A verified product page with controls present and nothing available."""
    return Observation(
        url="https://example.test/product",
        page_title="Vintage Alhambra bracelet, 5 motifs, Tiger Eye",
        controls_found=True,
        cta_value="order-by-phone",
        add_to_bag_button_count=2,
        visible_add_to_bag_count=0,
        reference_found=True,
        title_match=True,
        product_price="$5,850",
        price_verified=True,
    )


@pytest.fixture
def full_secrets() -> Secrets:
    return Secrets(
        gmail_client_id="cid",
        gmail_client_secret="csecret",
        gmail_refresh_token="rtoken",
        gmail_sender="sender@example.test",
        pushover_token="ptoken",
        pushover_user="puser",
        staging_recipient="staging@example.test",
    )


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class RecordingPoster:
    """Stands in for requests.post and records every Pushover call."""

    def __init__(self, status_code: int = 200, raises: Exception | None = None):
        self.status_code = status_code
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self.raises:
            raise self.raises
        return FakeResponse(self.status_code)


@pytest.fixture
def poster() -> RecordingPoster:
    return RecordingPoster()

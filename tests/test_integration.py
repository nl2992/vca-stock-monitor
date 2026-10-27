"""End-to-end orchestration tests with the notification boundaries mocked.

Every test here drives `monitor.run`/`handle_*` with a fake Gmail service and
a recording Pushover poster, so the decision rules are checked by what would
actually have been delivered.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from vca_monitor import monitor
from vca_monitor.availability import classify
from vca_monitor.config import AVAILABILITY_SUBJECT, EMAIL_RECIPIENTS, Mode, Secrets, resolve_mode
from vca_monitor.models import Availability, Observation
from vca_monitor.monitor import AlertLock, EXIT_ERROR, EXIT_INCONCLUSIVE, EXIT_OK
from vca_monitor.notifications import AVAILABLE_PUSH_TITLE, INCONCLUSIVE_PUSH_TITLE, TEST_PREFIX
from tests.test_gmail import FakeGmail


def recent_message(id_, minutes_ago, subject=AVAILABILITY_SUBJECT):
    """A SENT entry positioned relative to the real clock.

    `monitor.run` lets `find_recent_duplicate` default to `datetime.now`, so a
    fixture built from a fixed constant would drift in and out of the six-hour
    window depending on when the suite happens to run.
    """
    return {
        "id": id_,
        "sent_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "subject": subject,
    }

AVAILABLE = Observation(
    url="https://example.test",
    controls_found=True,
    cta_value="add-to-bag",
    add_to_bag_button_count=1,
    visible_add_to_bag_count=1,
    reference_found=True,
    title_match=True,
    product_price="$5,850",
    price_verified=True,
)


@pytest.fixture(autouse=True)
def isolated_lock(tmp_path, monkeypatch):
    """Keep each test's alert lock out of the real state directory."""
    monkeypatch.setenv("VCA_STATE_DIR", str(tmp_path))
    yield


# --- NOT_AVAILABLE: silence ------------------------------------------------

def test_not_available_classifies_without_touching_any_channel(poster):
    """The negative pattern is a pure classification; nothing is contacted."""
    observation = replace(AVAILABLE, cta_value="order-by-phone", visible_add_to_bag_count=0)
    verdict = classify(observation)
    assert verdict.state is Availability.NOT_AVAILABLE
    assert poster.calls == []


def test_not_available_run_report_records_silence(monkeypatch, full_secrets, poster):
    observation = replace(AVAILABLE, cta_value="order-by-phone", visible_add_to_bag_count=0)
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: observation)
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets,
                         service=FakeGmail(), poster=poster)
    assert report.state == "NOT_AVAILABLE"
    assert report.exit_code == EXIT_OK
    assert report.email_status == "not attempted"
    assert report.push_status == "not attempted"
    assert poster.calls == []


# --- INCONCLUSIVE: push only ------------------------------------------------

def test_inconclusive_pushes_and_never_emails(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: Observation(error="Timeout"))
    service = FakeGmail()
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)

    assert report.state == "INCONCLUSIVE"
    assert report.exit_code == EXIT_INCONCLUSIVE
    assert len(poster.calls) == 1
    assert poster.calls[0]["data"]["title"] == INCONCLUSIVE_PUSH_TITLE
    assert "No stock availability claim was made." in poster.calls[0]["data"]["message"]
    assert service.sent == []


def test_inconclusive_push_carries_the_technical_reason(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(
        monitor, "observe", lambda *a, **k: Observation(error="net::ERR_TIMED_OUT")
    )
    monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=FakeGmail(), poster=poster)
    assert "net::ERR_TIMED_OUT" in poster.calls[0]["data"]["message"]


def test_captcha_is_inconclusive_and_pushes(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(
        monitor, "observe", lambda *a, **k: replace(AVAILABLE, captcha_suspected=True)
    )
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets,
                         service=FakeGmail(), poster=poster)
    assert report.state == "INCONCLUSIVE"
    assert poster.calls[0]["data"]["title"] == INCONCLUSIVE_PUSH_TITLE


def test_inconclusive_with_no_pushover_configured_is_recorded(monkeypatch, poster):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: Observation(error="Timeout"))
    secrets = Secrets(gmail_client_id="a", gmail_client_secret="b",
                      gmail_refresh_token="c", gmail_sender="d@e.test")
    report = monitor.run(Mode.PRODUCTION, secrets=secrets, service=FakeGmail(), poster=poster)
    assert "not configured" in report.push_status
    assert poster.calls == []


# --- AVAILABLE: dedupe, email, push ----------------------------------------

def test_available_emails_both_recipients_once_and_pushes(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail([])
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)

    assert report.state == "AVAILABLE"
    assert len(service.sent) == 1
    import base64
    raw = base64.urlsafe_b64decode(service.sent[0]["body"]["raw"]).decode()
    assert f"To: {', '.join(EMAIL_RECIPIENTS)}" in raw
    assert f"Subject: {AVAILABILITY_SUBJECT}" in raw
    assert len(poster.calls) == 1
    assert poster.calls[0]["data"]["title"] == AVAILABLE_PUSH_TITLE
    assert "sent to" in poster.calls[0]["data"]["message"]


def test_duplicate_within_six_hours_suppresses_both_channels(
    monkeypatch, full_secrets, poster
):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail([recent_message("m1", minutes_ago=30)])
    report = monitor.run(
        Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster
    )
    assert service.sent == []
    assert poster.calls == []
    assert "suppressed" in report.email_status
    assert "suppressed (duplicate)" == report.push_status


def test_alert_older_than_six_hours_does_not_suppress(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail([recent_message("m1", minutes_ago=400)])
    monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)
    assert len(service.sent) == 1
    assert len(poster.calls) == 1


def test_duplicate_lookup_failure_withholds_the_email_and_pushes_the_failure(
    monkeypatch, full_secrets, poster
):
    """A broken lookup must never be treated as 'no duplicate found'."""
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail(list_error=RuntimeError("quota exceeded"))
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)

    assert service.sent == []
    assert report.email_status.startswith("withheld:")
    assert report.exit_code == EXIT_ERROR
    assert len(poster.calls) == 1
    assert "duplicate verification failed" in poster.calls[0]["data"]["message"]


def test_gmail_send_failure_is_reported_accurately_by_push(
    monkeypatch, full_secrets, poster
):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail([], send_error=RuntimeError("invalid grant"))
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)

    assert report.email_status.startswith("NOT SENT")
    assert "invalid grant" in report.email_status
    assert len(poster.calls) == 1
    assert "NOT SENT" in poster.calls[0]["data"]["message"]


def test_push_failure_is_recorded_after_a_successful_email(monkeypatch, full_secrets):
    from tests.conftest import RecordingPoster

    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    poster = RecordingPoster(status_code=500)
    service = FakeGmail([])
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)

    assert len(service.sent) == 1
    assert "500" in report.push_status
    assert "push failed" in report.actions


def test_unverified_price_does_not_block_the_alert(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(
        monitor,
        "observe",
        lambda *a, **k: replace(AVAILABLE, product_price=None, price_verified=False),
    )
    service = FakeGmail([])
    report = monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)
    assert report.state == "AVAILABLE"
    assert len(service.sent) == 1


# --- mode gating ------------------------------------------------------------

def test_dry_run_sends_nothing_at_all(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail([])
    report = monitor.run(Mode.DRY_RUN, secrets=full_secrets, service=service, poster=poster)

    assert report.state == "AVAILABLE"
    assert service.sent == []
    assert poster.calls == []
    assert "suppressed (dry-run)" == report.email_status


def test_dry_run_inconclusive_sends_nothing(monkeypatch, full_secrets, poster):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: Observation(error="Timeout"))
    report = monitor.run(Mode.DRY_RUN, secrets=full_secrets, service=FakeGmail(), poster=poster)
    assert poster.calls == []
    assert report.push_status == "suppressed (dry-run)"


def test_staging_only_mails_the_approved_test_address(full_secrets, poster):
    service = FakeGmail([])
    report = monitor.run(Mode.STAGING, secrets=full_secrets, service=service, poster=poster)

    assert len(service.sent) == 1
    import base64
    raw = base64.urlsafe_b64decode(service.sent[0]["body"]["raw"]).decode()
    assert "To: staging@example.test" in raw
    for recipient in EMAIL_RECIPIENTS:
        assert recipient not in raw
    assert TEST_PREFIX.strip() in raw
    assert "synthetic NOT_AVAILABLE case classified correctly, no message sent" in report.actions


def test_staging_without_a_recipient_sends_no_email(poster):
    secrets = Secrets(
        gmail_client_id="a", gmail_client_secret="b", gmail_refresh_token="c",
        gmail_sender="d@e.test", pushover_token="t", pushover_user="u",
    )
    service = FakeGmail([])
    report = monitor.run(Mode.STAGING, secrets=secrets, service=service, poster=poster)
    assert service.sent == []
    assert "no recipient configured" in report.email_status


def test_staging_exercises_the_inconclusive_path_too(full_secrets, poster):
    monitor.run(Mode.STAGING, secrets=full_secrets, service=FakeGmail([]), poster=poster)
    titles = [call["data"]["title"] for call in poster.calls]
    assert AVAILABLE_PUSH_TITLE in titles
    assert INCONCLUSIVE_PUSH_TITLE in titles


def test_unknown_mode_falls_back_to_dry_run():
    assert resolve_mode("PRODUCTION!") is Mode.DRY_RUN
    assert resolve_mode("") is Mode.DRY_RUN
    assert resolve_mode(None) is Mode.DRY_RUN


# --- concurrency and idempotency -------------------------------------------

def test_second_concurrent_run_does_not_deliver(monkeypatch, full_secrets, poster, tmp_path):
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    held = AlertLock(tmp_path / "held.lock")
    held.__enter__()
    monkeypatch.setattr(monitor, "_lock_path", lambda: tmp_path / "held.lock")
    try:
        service = FakeGmail([])
        report = monitor.run(
            Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster
        )
        assert service.sent == []
        assert poster.calls == []
        assert "concurrent run" in report.email_status
    finally:
        held.__exit__()


def test_lock_is_released_after_a_run(monkeypatch, full_secrets, poster, tmp_path):
    lock_file = tmp_path / "alert.lock"
    monkeypatch.setattr(monitor, "_lock_path", lambda: lock_file)
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=FakeGmail([]), poster=poster)
    assert not lock_file.exists()


def test_a_stale_lock_is_cleared(tmp_path):
    import os
    import time

    lock_file = tmp_path / "stale.lock"
    lock_file.write_text("999999")
    os.utime(lock_file, (time.time() - 9999, time.time() - 9999))
    with AlertLock(lock_file, stale_seconds=600) as lock:
        assert lock.acquired is True


def test_a_fresh_lock_is_respected(tmp_path):
    lock_file = tmp_path / "fresh.lock"
    lock_file.write_text("123")
    with AlertLock(lock_file, stale_seconds=600) as lock:
        assert lock.acquired is False


def test_repeated_runs_within_six_hours_send_one_email(monkeypatch, full_secrets, poster):
    """Idempotency across runs: the Gmail lookback is the authority."""
    monkeypatch.setattr(monitor, "observe", lambda *a, **k: AVAILABLE)
    service = FakeGmail([])
    sent_log: list[dict] = []

    def record_and_remember(kwargs):
        sent_log.append(kwargs)
        service._messages.insert(
            0,
            {
                "id": f"sent-{len(sent_log)}",
                "sent_at": datetime.now(timezone.utc),
                "subject": AVAILABILITY_SUBJECT,
            },
        )
        return {"id": f"sent-{len(sent_log)}"}

    service._send = record_and_remember

    for _ in range(3):
        monitor.run(Mode.PRODUCTION, secrets=full_secrets, service=service, poster=poster)

    assert len(sent_log) == 1


# --- workflow failure path --------------------------------------------------

def test_workflow_failure_notice_uses_its_own_title(monkeypatch, full_secrets, poster):
    captured = {}

    def fake_send_push(token, user, title, message, **kwargs):
        captured.update(title=title, message=message)
        from vca_monitor.notifications import PushResult

        return PushResult(True, "ok")

    monkeypatch.setattr(monitor.notifications, "send_push", fake_send_push)
    assert monitor.notify_workflow_failure("deps", "pip failed", full_secrets) == EXIT_OK
    assert captured["title"] == "VCA MONITOR WORKFLOW FAILURE"
    assert "pip failed" in captured["message"]

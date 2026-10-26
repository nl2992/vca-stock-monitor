"""Gmail duplicate lookup and send tests against a fake Gmail service.

The fake mirrors the parts of the API the client uses, including
`internalDate` in epoch milliseconds, so the six-hour boundary is exercised
with real timestamp arithmetic.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from vca_monitor.config import AVAILABILITY_SUBJECT, Secrets
from vca_monitor.gmail_client import (
    GmailError,
    build_service,
    find_recent_duplicate,
    recipients_for,
    send_email,
)

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


def epoch_ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


class FakeGmail:
    """Minimal stand-in for the Gmail API client surface we use."""

    def __init__(self, messages=None, list_error=None, get_error=None, send_error=None,
                 send_result=None):
        self._messages = messages or []
        self.list_error = list_error
        self.get_error = get_error
        self.send_error = send_error
        self.send_result = send_result if send_result is not None else {"id": "sent-1"}
        self.sent: list[dict] = []
        self.list_calls: list[dict] = []

    # -- API shape --
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Exec(lambda: self._list(kwargs))

    def get(self, **kwargs):
        return _Exec(lambda: self._get(kwargs))

    def send(self, **kwargs):
        return _Exec(lambda: self._send(kwargs))

    # -- behaviour --
    def _list(self, kwargs):
        if self.list_error:
            raise self.list_error
        assert kwargs["labelIds"] == ["SENT"]
        return {"messages": [{"id": m["id"]} for m in self._messages]}

    def _get(self, kwargs):
        if self.get_error:
            raise self.get_error
        for message in self._messages:
            if message["id"] == kwargs["id"]:
                return {
                    "id": message["id"],
                    "internalDate": epoch_ms(message["sent_at"]),
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": message["subject"]},
                            {"name": "To", "value": message.get("to", "a@b.test")},
                        ]
                    },
                }
        raise AssertionError(f"unexpected id {kwargs['id']}")

    def _send(self, kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append(kwargs)
        return self.send_result


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def message(id_, minutes_ago, subject=AVAILABILITY_SUBJECT):
    return {"id": id_, "sent_at": NOW - timedelta(minutes=minutes_ago), "subject": subject}


# --- duplicate detection ----------------------------------------------------

def test_duplicate_inside_six_hours_is_found():
    service = FakeGmail([message("m1", minutes_ago=90)])
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.ok and result.duplicate_found
    assert result.may_send is False
    assert result.matches


def test_duplicate_just_inside_the_boundary_is_found():
    service = FakeGmail([message("m1", minutes_ago=359)])
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.duplicate_found is True


def test_alert_just_outside_six_hours_does_not_suppress():
    service = FakeGmail([message("m1", minutes_ago=361)])
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.ok and result.duplicate_found is False
    assert result.may_send is True


def test_similar_but_not_identical_subject_does_not_suppress():
    """Subject equality is exact, so a near-miss is not a duplicate."""
    service = FakeGmail([message("m1", 30, subject=AVAILABILITY_SUBJECT + " (resend)")])
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.duplicate_found is False


def test_subject_match_is_case_sensitive():
    service = FakeGmail([message("m1", 30, subject=AVAILABILITY_SUBJECT.upper())])
    assert find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW).duplicate_found is False


def test_unrelated_recent_mail_does_not_suppress():
    service = FakeGmail([message("m1", 10, subject="Lunch?")])
    assert find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW).duplicate_found is False


def test_scan_stops_at_the_first_too_old_message():
    """SENT is newest-first, so the scan need not read the whole mailbox."""
    service = FakeGmail(
        [message("m1", 10, subject="Lunch?"), message("m2", 500), message("m3", 600)]
    )
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.duplicate_found is False
    assert "1 message(s) examined" in result.detail


def test_empty_sent_folder_allows_sending():
    result = find_recent_duplicate(FakeGmail([]), AVAILABILITY_SUBJECT, now=NOW)
    assert result.may_send is True


def test_list_failure_blocks_sending():
    service = FakeGmail(list_error=RuntimeError("quota exceeded"))
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.ok is False
    assert result.may_send is False
    assert "quota exceeded" in result.detail


def test_metadata_read_failure_blocks_sending():
    service = FakeGmail([message("m1", 30)], get_error=RuntimeError("500 backend error"))
    result = find_recent_duplicate(service, AVAILABILITY_SUBJECT, now=NOW)
    assert result.ok is False
    assert result.may_send is False


def test_message_without_internal_date_is_skipped_not_trusted():
    class NoDate(FakeGmail):
        def _get(self, kwargs):
            return {"id": "m1", "payload": {"headers": [
                {"name": "Subject", "value": AVAILABILITY_SUBJECT}]}}

    result = find_recent_duplicate(NoDate([message("m1", 30)]), AVAILABILITY_SUBJECT, now=NOW)
    assert result.ok is True
    assert result.duplicate_found is False


# --- sending ----------------------------------------------------------------

def test_send_email_addresses_both_recipients_in_one_message():
    service = FakeGmail()
    result = send_email(
        service,
        sender="me@example.test",
        recipients=["a@one.test", "b@two.test"],
        subject=AVAILABILITY_SUBJECT,
        body="body text",
    )
    assert result.ok and result.message_id == "sent-1"
    assert len(service.sent) == 1
    raw = base64.urlsafe_b64decode(service.sent[0]["body"]["raw"]).decode()
    assert "To: a@one.test, b@two.test" in raw
    assert f"Subject: {AVAILABILITY_SUBJECT}" in raw


def test_send_failure_is_reported_not_raised():
    service = FakeGmail(send_error=RuntimeError("invalid grant"))
    result = send_email(service, sender="me@example.test", recipients=["a@b.test"],
                        subject="s", body="b")
    assert result.ok is False
    assert "invalid grant" in result.detail


def test_send_without_message_id_counts_as_failure():
    service = FakeGmail(send_result={})
    result = send_email(service, sender="me@example.test", recipients=["a@b.test"],
                        subject="s", body="b")
    assert result.ok is False


# --- configuration ----------------------------------------------------------

def test_build_service_requires_full_credentials():
    with pytest.raises(GmailError):
        build_service(Secrets(gmail_client_id="only-this"))


def test_recipients_depend_on_mode():
    production = recipients_for(True, "staging@x.test", ("a@x.test", "b@x.test"))
    assert production == ("a@x.test", "b@x.test")
    assert recipients_for(False, "staging@x.test", ("a@x.test",)) == ("staging@x.test",)
    assert recipients_for(False, None, ("a@x.test",)) == ()


def test_secrets_repr_hides_values():
    secrets = Secrets(gmail_client_id="super-secret", pushover_token="also-secret")
    assert "super-secret" not in repr(secrets)
    assert "also-secret" not in repr(secrets)

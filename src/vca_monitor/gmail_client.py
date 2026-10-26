"""Gmail delivery and Sent-folder duplicate lookup over OAuth 2.0.

Scopes are deliberately narrow:

* `gmail.send`     - send the alert, nothing else.
* `gmail.metadata` - read message headers in SENT. No message bodies, and the
                     scope does not permit server-side search, which is why
                     duplicate detection lists SENT and compares subjects and
                     `internalDate` values here instead of trusting a Gmail
                     query string.

A refresh token is exchanged for an access token at runtime; no password is
used and no credential is ever logged.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Iterable, Sequence

from .config import DUPLICATE_LOOKBACK_HOURS, Secrets

log = logging.getLogger(__name__)

GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.metadata",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"

# How many recent SENT messages to examine when looking for a duplicate. Six
# hours of alerts cannot plausibly exceed this.
SENT_SCAN_LIMIT = 100


class GmailError(RuntimeError):
    """Raised when Gmail cannot be reached or refuses a request."""


@dataclass(frozen=True)
class DuplicateCheck:
    """Outcome of the Sent-folder lookback.

    `ok` is False when the lookup itself failed, which must suppress the email
    rather than be treated as "no duplicate found".
    """

    ok: bool
    duplicate_found: bool
    detail: str
    matches: tuple[str, ...] = field(default=())

    @property
    def may_send(self) -> bool:
        return self.ok and not self.duplicate_found


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str
    message_id: str | None = None


def build_service(secrets: Secrets, *, _factory: Any = None) -> Any:
    """Build an authorised Gmail service from a refresh token."""
    if not secrets.gmail_configured:
        raise GmailError("Gmail OAuth credentials are not fully configured")

    if _factory is not None:  # test seam
        return _factory()

    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from googleapiclient.discovery import build  # noqa: PLC0415

    credentials = Credentials(
        token=None,
        refresh_token=secrets.gmail_refresh_token,
        client_id=secrets.gmail_client_id,
        client_secret=secrets.gmail_client_secret,
        token_uri=TOKEN_URI,
        scopes=list(GMAIL_SCOPES),
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _header(payload: dict[str, Any], name: str) -> str:
    for entry in (payload.get("payload", {}) or {}).get("headers", []) or []:
        if (entry.get("name") or "").lower() == name.lower():
            return (entry.get("value") or "").strip()
    return ""


def find_recent_duplicate(
    service: Any,
    subject: str,
    *,
    lookback_hours: int = DUPLICATE_LOOKBACK_HOURS,
    now: datetime | None = None,
    scan_limit: int = SENT_SCAN_LIMIT,
) -> DuplicateCheck:
    """Look for an identical alert already sent inside the lookback window.

    Subject equality is checked exactly, in this process, and the send time
    comes from each message's `internalDate` rather than from a Gmail query,
    so a loose server-side match can neither cause nor hide a suppression.
    """
    reference_now = now or datetime.now(timezone.utc)
    cutoff = reference_now - timedelta(hours=lookback_hours)

    try:
        listing = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["SENT"], maxResults=scan_limit)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        return DuplicateCheck(
            ok=False,
            duplicate_found=False,
            detail=f"Gmail SENT listing failed: {type(exc).__name__}: {exc}",
        )

    message_ids = [m.get("id") for m in (listing.get("messages") or []) if m.get("id")]
    if not message_ids:
        return DuplicateCheck(True, False, "no messages in SENT")

    matches: list[str] = []
    examined = 0
    for message_id in message_ids:
        try:
            detail = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["Subject", "To", "Date"],
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            return DuplicateCheck(
                ok=False,
                duplicate_found=False,
                detail=(
                    f"Gmail metadata read failed for one SENT message: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        sent_at = _internal_date(detail)
        if sent_at is None:
            continue
        if sent_at < cutoff:
            # SENT comes back newest-first, so the first too-old message ends
            # the useful part of the scan.
            break
        examined += 1  # counts only messages inside the lookback window
        if _header(detail, "Subject") == subject:
            matches.append(f"{message_id} at {sent_at.isoformat()}")

    if matches:
        return DuplicateCheck(
            ok=True,
            duplicate_found=True,
            detail=(
                f"{len(matches)} alert(s) with this exact subject sent within "
                f"{lookback_hours}h"
            ),
            matches=tuple(matches),
        )

    return DuplicateCheck(
        ok=True,
        duplicate_found=False,
        detail=(
            f"no identical subject in SENT within {lookback_hours}h "
            f"({examined} message(s) examined)"
        ),
    )


def _internal_date(detail: dict[str, Any]) -> datetime | None:
    raw = detail.get("internalDate")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def send_email(
    service: Any,
    *,
    sender: str,
    recipients: Sequence[str],
    subject: str,
    body: str,
) -> SendResult:
    """Send one plain-text message addressed to every recipient together."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    try:
        result = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": encoded})
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        return SendResult(False, f"Gmail send failed: {type(exc).__name__}: {exc}")

    message_id = (result or {}).get("id")
    if not message_id:
        return SendResult(False, "Gmail send returned no message id")
    return SendResult(True, f"Gmail accepted the message (id {message_id})", message_id)


def recipients_for(mode_is_production: bool, staging_recipient: str | None,
                   production_recipients: Iterable[str]) -> tuple[str, ...]:
    """Production mails both recipients; anything else mails only the approved
    staging address, and nothing at all when none is configured."""
    if mode_is_production:
        return tuple(production_recipients)
    return (staging_recipient,) if staging_recipient else ()

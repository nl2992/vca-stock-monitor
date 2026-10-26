"""Orchestration and command-line entry point.

Decision rules, in order:

* NOT_AVAILABLE  -> log only. No email, no push, no further searching.
* INCONCLUSIVE   -> Pushover only, with the technical reason and an explicit
                    statement that no availability claim was made.
* AVAILABLE      -> check Gmail SENT for an identical alert in the last six
                    hours. A duplicate suppresses both channels. A *failed*
                    lookup suppresses the email and is reported by push.

Only `production` mode can send a genuine availability message; `dry-run`
sends nothing at all and `staging` sends clearly-marked TEST content to one
approved address.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import notifications
from .availability import classify
from .browser import inspect_product_page
from .config import (
    AVAILABILITY_SUBJECT,
    DUPLICATE_LOOKBACK_HOURS,
    EMAIL_RECIPIENTS,
    PRODUCT_URL,
    Mode,
    Secrets,
    resolve_mode,
)
from .gmail_client import (
    DuplicateCheck,
    GmailError,
    build_service,
    find_recent_duplicate,
    recipients_for,
    send_email,
)
from .models import Availability, Observation, Verdict

log = logging.getLogger("vca_monitor")

EXIT_OK = 0
EXIT_INCONCLUSIVE = 2
EXIT_ERROR = 1

# An alert attempt holds this lock so two runs on the same runner cannot both
# deliver. Cross-runner safety comes from the workflow concurrency group plus
# the Gmail duplicate lookup.
LOCK_STALE_SECONDS = 600


@dataclass
class RunReport:
    """Everything one run did, suitable for logging as JSON."""

    mode: str
    state: str
    reason: str
    observation: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    email_status: str = "not attempted"
    push_status: str = "not attempted"
    duplicate_detail: str = "not checked"
    exit_code: int = EXIT_OK

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True, default=str)


# --- single-runner lock -----------------------------------------------------


def _lock_path() -> Path:
    base = os.environ.get("VCA_STATE_DIR") or os.environ.get("RUNNER_TEMP") or "/tmp"
    return Path(base) / "vca-monitor-alert.lock"


class AlertLock:
    """Best-effort advisory lock around the alert-delivery section."""

    def __init__(self, path: Path | None = None, stale_seconds: int = LOCK_STALE_SECONDS):
        self.path = path or _lock_path()
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "AlertLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                age = time.time() - self.path.stat().st_mtime
                if age > self.stale_seconds:
                    log.warning("clearing stale alert lock (%.0fs old)", age)
                    self.path.unlink(missing_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
        except FileExistsError:
            self.acquired = False
        except OSError as exc:
            log.warning("could not take alert lock (%s); proceeding", exc)
            self.acquired = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)


# --- notification helpers ---------------------------------------------------


def _push(secrets: Secrets, title: str, message: str, *, priority: int = 1,
          poster: Any = None) -> notifications.PushResult:
    result = notifications.send_push(
        secrets.pushover_token,
        secrets.pushover_user,
        title,
        message,
        priority=priority,
        _poster=poster,
    )
    if not result.ok:
        log.error("push not delivered: %s", result.detail)
    return result


def handle_inconclusive(
    verdict: Verdict, secrets: Secrets, mode: Mode, report: RunReport, *, poster: Any = None
) -> RunReport:
    """Inconclusive sends a push and never an email."""
    report.exit_code = EXIT_INCONCLUSIVE
    if mode is Mode.DRY_RUN:
        report.actions.append("dry-run: would push inconclusive notice")
        report.push_status = "suppressed (dry-run)"
        return report

    message = notifications.build_inconclusive_push(verdict.reason, mode=mode)
    result = _push(
        secrets,
        notifications.INCONCLUSIVE_PUSH_TITLE,
        message,
        priority=0,
        poster=poster,
    )
    report.push_status = result.detail
    report.actions.append("pushed inconclusive notice" if result.ok else "push failed")
    return report


def handle_available(
    verdict: Verdict,
    secrets: Secrets,
    mode: Mode,
    report: RunReport,
    *,
    service: Any = None,
    poster: Any = None,
    now: Any = None,
) -> RunReport:
    """Deduplicate, then deliver the email and the push, reporting both honestly."""
    if mode is Mode.DRY_RUN:
        report.actions.append("dry-run: would check Gmail for duplicates and alert")
        report.email_status = "suppressed (dry-run)"
        report.push_status = "suppressed (dry-run)"
        return report

    subject = notifications.subject_for(mode)
    recipients = recipients_for(mode is Mode.PRODUCTION, secrets.staging_recipient,
                                EMAIL_RECIPIENTS)

    with AlertLock() as lock:
        if not lock.acquired:
            report.actions.append("another run on this runner holds the alert lock")
            report.email_status = "suppressed (concurrent run)"
            report.push_status = "suppressed (concurrent run)"
            report.duplicate_detail = "not checked (concurrent run)"
            return report

        if not recipients:
            report.email_status = "no recipient configured for this mode; email skipped"
            duplicate = DuplicateCheck(True, False, "not applicable without a recipient")
        else:
            duplicate = _duplicate_check(secrets, subject, service, now)
            report.duplicate_detail = duplicate.detail

            if duplicate.duplicate_found:
                report.actions.append("suppressed: identical alert already sent")
                report.email_status = f"suppressed ({duplicate.detail})"
                report.push_status = "suppressed (duplicate)"
                return report

            if not duplicate.ok:
                report.actions.append("duplicate verification failed; email withheld")
                report.email_status = f"withheld: {duplicate.detail}"
                push = _push(
                    secrets,
                    notifications.AVAILABLE_PUSH_TITLE,
                    notifications.build_availability_push(
                        verdict.observation,
                        email_status=(
                            "NOT SENT - duplicate verification failed: "
                            f"{duplicate.detail}"
                        ),
                        mode=mode,
                    ),
                    priority=1,
                    poster=poster,
                )
                report.push_status = push.detail
                report.exit_code = EXIT_ERROR
                return report

            report.email_status = _deliver_email(
                secrets, subject, recipients, verdict, mode, service, report
            )

        push = _push(
            secrets,
            notifications.AVAILABLE_PUSH_TITLE,
            notifications.build_availability_push(
                verdict.observation, email_status=report.email_status, mode=mode
            ),
            priority=1,
            poster=poster,
        )
        report.push_status = push.detail
        report.actions.append("pushed availability alert" if push.ok else "push failed")

    return report


def _duplicate_check(secrets: Secrets, subject: str, service: Any, now: Any) -> DuplicateCheck:
    try:
        resolved = service if service is not None else build_service(secrets)
    except GmailError as exc:
        return DuplicateCheck(False, False, f"Gmail unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001
        return DuplicateCheck(False, False, f"Gmail client build failed: {exc}")

    return find_recent_duplicate(
        resolved, subject, lookback_hours=DUPLICATE_LOOKBACK_HOURS, now=now
    )


def _deliver_email(
    secrets: Secrets,
    subject: str,
    recipients: tuple[str, ...],
    verdict: Verdict,
    mode: Mode,
    service: Any,
    report: RunReport,
) -> str:
    try:
        resolved = service if service is not None else build_service(secrets)
    except Exception as exc:  # noqa: BLE001
        return f"NOT SENT - Gmail unavailable: {exc}"

    body = notifications.build_availability_email(verdict.observation, mode=mode)
    result = send_email(
        resolved,
        sender=secrets.gmail_sender or "",
        recipients=recipients,
        subject=subject,
        body=body,
    )
    if result.ok:
        report.actions.append(f"emailed {', '.join(recipients)}")
        return f"sent to {', '.join(recipients)} ({result.detail})"
    report.actions.append("email failed")
    return f"NOT SENT - {result.detail}"


# --- run modes --------------------------------------------------------------


def observe(mode: Mode, *, headless: bool = True, fixture: str | None = None) -> Observation:
    """Produce the observation for this run."""
    if fixture:
        from .browser import build_observation  # noqa: PLC0415

        return _observe_fixture(fixture, headless=headless)
    return inspect_product_page(PRODUCT_URL, headless=headless)


def _observe_fixture(fixture: str, *, headless: bool = True) -> Observation:
    """Run the real inspection script in Chromium against a local HTML file."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    from .browser import INSPECTION_JS, PAGE_TEXT_JS, build_observation  # noqa: PLC0415

    path = Path(fixture).resolve()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.goto(path.as_uri(), wait_until="domcontentloaded")
            payload = page.evaluate(INSPECTION_JS)
            page_text = page.evaluate(PAGE_TEXT_JS) or ""
            return build_observation(payload, page_text)
        finally:
            browser.close()


def run_staging(secrets: Secrets, *, poster: Any = None, service: Any = None) -> RunReport:
    """Exercise both notification paths with synthetic data only."""
    report = RunReport(mode=Mode.STAGING.value, state="synthetic", reason="staging run")

    positive = Observation(
        url=PRODUCT_URL,
        page_title="Synthetic staging fixture",
        controls_found=True,
        cta_value="add-to-bag",
        add_to_bag_button_count=1,
        visible_add_to_bag_count=1,
        reference_found=True,
        title_match=True,
        product_price="$0,000 (synthetic)",
        price_verified=False,
    )
    positive_verdict = classify(positive)
    assert positive_verdict.state is Availability.AVAILABLE, "staging positive must classify AVAILABLE"
    handle_available(positive_verdict, secrets, Mode.STAGING, report,
                     service=service, poster=poster)

    negative = replace(positive, cta_value="order-by-phone", visible_add_to_bag_count=0)
    negative_verdict = classify(negative)
    assert negative_verdict.state is Availability.NOT_AVAILABLE, (
        "staging negative must classify NOT_AVAILABLE"
    )
    report.actions.append("synthetic NOT_AVAILABLE case classified correctly, no message sent")

    inconclusive_verdict = classify(replace(positive, reference_found=False))
    inconclusive_report = RunReport(
        mode=Mode.STAGING.value,
        state=inconclusive_verdict.state.value,
        reason=inconclusive_verdict.reason,
    )
    handle_inconclusive(inconclusive_verdict, secrets, Mode.STAGING,
                        inconclusive_report, poster=poster)
    report.actions.extend(f"inconclusive path: {a}" for a in inconclusive_report.actions)
    return report


def run(
    mode: Mode,
    *,
    headless: bool = True,
    fixture: str | None = None,
    secrets: Secrets | None = None,
    service: Any = None,
    poster: Any = None,
) -> RunReport:
    """Perform one monitoring run and return its report."""
    resolved_secrets = secrets or Secrets.from_env()

    if mode is Mode.STAGING:
        return run_staging(resolved_secrets, poster=poster, service=service)

    observation = observe(mode, headless=headless, fixture=fixture)
    verdict = classify(observation)
    log.info("verdict=%s evidence=%s", verdict.state.value, observation.log_line())

    report = RunReport(
        mode=mode.value,
        state=verdict.state.value,
        reason=verdict.reason,
        observation=observation.to_dict(),
    )

    if verdict.state is Availability.NOT_AVAILABLE:
        report.actions.append("not available; logging only, no messages sent")
        return report

    if verdict.state is Availability.INCONCLUSIVE:
        return handle_inconclusive(verdict, resolved_secrets, mode, report, poster=poster)

    return handle_available(
        verdict, resolved_secrets, mode, report, service=service, poster=poster
    )


def notify_workflow_failure(stage: str, detail: str, secrets: Secrets | None = None) -> int:
    """Push a workflow-level failure notice, distinct from any stock alert."""
    resolved = secrets or Secrets.from_env()
    result = _push(
        resolved,
        notifications.WORKFLOW_FAILURE_PUSH_TITLE,
        notifications.build_workflow_failure_push(stage, detail),
        priority=1,
    )
    print(f"workflow failure push: {result.detail}")
    return EXIT_OK if result.ok else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vca-monitor", description="Read-only availability monitor for VCARD35600"
    )
    parser.add_argument(
        "--mode",
        default=os.environ.get("VCA_MODE", Mode.DRY_RUN.value),
        help="dry-run (default), staging, or production",
    )
    parser.add_argument("--fixture", help="inspect a local HTML fixture instead of the live page")
    parser.add_argument("--headed", action="store_true", help="run Chromium headed (local debugging)")
    parser.add_argument("--json", action="store_true", help="print the run report as JSON")
    parser.add_argument(
        "--notify-failure",
        nargs=2,
        metavar=("STAGE", "DETAIL"),
        help="send a workflow-failure push instead of running a check",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    if args.notify_failure:
        return notify_workflow_failure(*args.notify_failure)

    mode = resolve_mode(args.mode)
    if mode is not resolve_mode(args.mode.strip().lower()):  # pragma: no cover
        log.warning("mode %r not recognised; falling back to %s", args.mode, mode.value)

    report = run(mode, headless=not args.headed, fixture=args.fixture)

    if args.json:
        print(report.to_json())
    else:
        print(f"mode={report.mode} state={report.state}")
        print(f"reason={report.reason}")
        print(f"email={report.email_status}")
        print(f"push={report.push_status}")
        print(f"duplicate_check={report.duplicate_detail}")
        for action in report.actions:
            print(f"  - {action}")

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

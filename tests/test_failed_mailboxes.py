from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from core.base_identity import MailboxIdentityProvider
from core.base_mailbox import MailboxAccount
from core.db import FailedMailboxModel, engine
from services.failed_mailboxes import (
    clear_failed_mailbox,
    is_mailbox_blocked,
    record_failed_mailbox,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cleanup() -> None:
    with Session(engine) as session:
        items = session.exec(
            select(FailedMailboxModel).where(FailedMailboxModel.email.like("failed-mailbox-test-%"))
        ).all()
        for item in items:
            session.delete(item)
        session.commit()


def test_record_failed_mailbox_creates_permanent_block_for_registered_email():
    _cleanup()
    email = "failed-mailbox-test-registered@example.com"
    try:
        record_failed_mailbox(
            provider_name="testmail",
            resource_identifier="resource-registered",
            email=email,
            platform="chatgpt",
            failure_stage="register_password",
            failure_reason="email_already_registered_on_openai",
            task_id="task_test_1",
        )

        blocked, reason = is_mailbox_blocked(
            provider_name="testmail",
            resource_identifier="resource-registered",
            email=email,
            platform="chatgpt",
        )
        assert blocked is True
        assert reason == "email_already_registered_on_openai"
    finally:
        _cleanup()


def test_record_failed_mailbox_creates_cooldown_for_retryable_reason():
    _cleanup()
    email = "failed-mailbox-test-cooldown@example.com"
    try:
        record_failed_mailbox(
            provider_name="testmail",
            resource_identifier="resource-cooldown",
            email=email,
            platform="chatgpt",
            failure_stage="otp_wait",
            failure_reason="otp_timeout",
            task_id="task_test_2",
        )

        blocked, reason = is_mailbox_blocked(
            provider_name="testmail",
            resource_identifier="resource-cooldown",
            email=email,
            platform="chatgpt",
        )
        assert blocked is True
        assert reason == "otp_timeout"
    finally:
        _cleanup()


def test_expired_cooldown_mailbox_is_not_blocked():
    _cleanup()
    email = "failed-mailbox-test-expired@example.com"
    try:
        with Session(engine) as session:
            session.add(
                FailedMailboxModel(
                    provider_type="mailbox",
                    provider_name="testmail",
                    resource_identifier="resource-expired",
                    email=email,
                    platform="chatgpt",
                    failure_stage="otp_wait",
                    failure_reason="otp_timeout",
                    retryable=True,
                    blocked_until=_utcnow() - timedelta(minutes=1),
                    fail_count=1,
                    last_task_id="task_expired",
                )
            )
            session.commit()

        blocked, reason = is_mailbox_blocked(
            provider_name="testmail",
            resource_identifier="resource-expired",
            email=email,
            platform="chatgpt",
        )
        assert blocked is False
        assert reason == ""
    finally:
        _cleanup()


def test_clear_failed_mailbox_removes_existing_block():
    _cleanup()
    email = "failed-mailbox-test-clear@example.com"
    try:
        record_failed_mailbox(
            provider_name="testmail",
            resource_identifier="resource-clear",
            email=email,
            platform="chatgpt",
            failure_stage="otp_wait",
            failure_reason="otp_timeout",
            task_id="task_test_3",
        )

        clear_failed_mailbox(
            provider_name="testmail",
            resource_identifier="resource-clear",
            email=email,
            platform="chatgpt",
        )

        blocked, reason = is_mailbox_blocked(
            provider_name="testmail",
            resource_identifier="resource-clear",
            email=email,
            platform="chatgpt",
        )
        assert blocked is False
        assert reason == ""
    finally:
        _cleanup()


def test_mailbox_identity_provider_skips_blocked_mailbox():
    _cleanup()

    class FakeMailbox:
        def __init__(self):
            self._items = [
                MailboxAccount(
                    email="failed-mailbox-test-blocked@example.com",
                    extra={
                        "mailbox_provider_key": "testmail",
                        "provider_resource": {
                            "provider_name": "testmail",
                            "resource_identifier": "resource-blocked",
                        },
                    },
                ),
                MailboxAccount(
                    email="failed-mailbox-test-available@example.com",
                    extra={
                        "mailbox_provider_key": "testmail",
                        "provider_resource": {
                            "provider_name": "testmail",
                            "resource_identifier": "resource-available",
                        },
                    },
                ),
            ]

        def get_email(self):
            return self._items.pop(0)

        def get_current_ids(self, account):
            return set()

    try:
        record_failed_mailbox(
            provider_name="testmail",
            resource_identifier="resource-blocked",
            email="failed-mailbox-test-blocked@example.com",
            platform="chatgpt",
            failure_stage="register",
            failure_reason="email_already_registered_on_openai",
            task_id="task_test_4",
        )

        provider = MailboxIdentityProvider(
            mailbox=FakeMailbox(),
            extra={"platform_name": "chatgpt", "mail_provider": "testmail"},
        )
        identity = provider.resolve()
        assert identity.email == "failed-mailbox-test-available@example.com"
    finally:
        _cleanup()

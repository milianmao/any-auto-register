from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from core.db import FailedMailboxModel, engine


DEFAULT_COOLDOWN_HOURS = 12
PERMANENT_FAILURE_REASONS = {
    "email_already_registered_on_openai",
    "email_invalid",
    "email_domain_invalid",
    "email_blocked_by_provider",
    "mailbox_unavailable",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalize_text(value: str | None) -> str:
    return str(value or "").strip()


def classify_failed_mailbox_reason(error: str | None) -> tuple[str, bool]:
    text = _normalize_text(error).lower()
    if not text:
        return "unknown_error", True
    if "already registered" in text or "already exists" in text or "user_exists" in text:
        return "email_already_registered_on_openai", False
    if "invalid email" in text or "邮箱格式" in text:
        return "email_invalid", False
    if "otp" in text and "timeout" in text:
        return "otp_timeout", True
    if "验证码" in text and ("超时" in text or "未收到" in text):
        return "otp_timeout", True
    return "unknown_error", True


def _blocked_until(retryable: bool, hours: int = DEFAULT_COOLDOWN_HOURS) -> datetime | None:
    if not retryable:
        return None
    return _utcnow() + timedelta(hours=max(int(hours or DEFAULT_COOLDOWN_HOURS), 1))


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def record_failed_mailbox(
    *,
    provider_name: str,
    resource_identifier: str,
    email: str,
    platform: str,
    failure_stage: str,
    failure_reason: str,
    task_id: str = "",
    retryable: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    normalized_email = normalize_email(email)
    normalized_platform = _normalize_text(platform)
    if not normalized_email or not normalized_platform:
        return

    normalized_reason = _normalize_text(failure_reason) or "unknown_error"
    resolved_retryable = bool(retryable) if retryable is not None else normalized_reason not in PERMANENT_FAILURE_REASONS

    with Session(engine) as session:
        item = session.exec(
            select(FailedMailboxModel)
            .where(FailedMailboxModel.platform == normalized_platform)
            .where(FailedMailboxModel.email == normalized_email)
        ).first()
        if not item:
            item = FailedMailboxModel(
                provider_type="mailbox",
                provider_name=_normalize_text(provider_name),
                resource_identifier=_normalize_text(resource_identifier),
                platform=normalized_platform,
                email=normalized_email,
                created_at=_utcnow(),
            )
            item.fail_count = 0
        item.provider_name = _normalize_text(provider_name)
        item.resource_identifier = _normalize_text(resource_identifier)
        item.failure_stage = _normalize_text(failure_stage)
        item.failure_reason = normalized_reason
        item.retryable = resolved_retryable
        item.blocked_until = _blocked_until(resolved_retryable)
        item.fail_count = int(item.fail_count or 0) + 1
        item.last_task_id = _normalize_text(task_id)
        item.updated_at = _utcnow()
        item.set_metadata(metadata or {})
        session.add(item)
        session.commit()


def is_mailbox_blocked(
    *,
    provider_name: str,
    resource_identifier: str,
    email: str,
    platform: str,
) -> tuple[bool, str]:
    normalized_email = normalize_email(email)
    normalized_platform = _normalize_text(platform)
    if not normalized_email or not normalized_platform:
        return False, ""

    with Session(engine) as session:
        item = session.exec(
            select(FailedMailboxModel)
            .where(FailedMailboxModel.platform == normalized_platform)
            .where(FailedMailboxModel.email == normalized_email)
        ).first()
        if not item:
            return False, ""
        if not item.retryable:
            return True, item.failure_reason
        blocked_until = _coerce_utc(item.blocked_until)
        if blocked_until and blocked_until > _utcnow():
            return True, item.failure_reason
        return False, ""


def clear_failed_mailbox(
    *,
    provider_name: str,
    resource_identifier: str,
    email: str,
    platform: str,
) -> None:
    normalized_email = normalize_email(email)
    normalized_platform = _normalize_text(platform)
    if not normalized_email or not normalized_platform:
        return

    with Session(engine) as session:
        item = session.exec(
            select(FailedMailboxModel)
            .where(FailedMailboxModel.platform == normalized_platform)
            .where(FailedMailboxModel.email == normalized_email)
        ).first()
        if not item:
            return
        session.delete(item)
        session.commit()

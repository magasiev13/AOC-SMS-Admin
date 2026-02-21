from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from app import db
from app.config import Config as AppConfig
from app.models import AuthEvent, LoginAttempt, UserPasswordHistory


ACCOUNT_SCOPE_IP = "__account__"
IP_SCOPE_USERNAME = ""

_LAST_PRUNE_DATE: date | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_login_username(username: str | None) -> str:
    return (username or "").strip().lower()


def password_policy_errors(password: str, username: str | None = None) -> list[str]:
    if not current_app.config.get("AUTH_PASSWORD_POLICY_ENFORCE", True):
        return []

    errors: list[str] = []
    value = password or ""
    minimum_length = int(current_app.config.get("AUTH_PASSWORD_MIN_LENGTH", 12))

    if len(value) < minimum_length:
        errors.append(f"Password must be at least {minimum_length} characters.")

    if not any(ch.islower() for ch in value):
        errors.append("Password must include at least one lowercase letter.")

    if not any(ch.isupper() for ch in value):
        errors.append("Password must include at least one uppercase letter.")

    if not any(ch.isdigit() for ch in value):
        errors.append("Password must include at least one digit.")

    if not any(not ch.isalnum() for ch in value):
        errors.append("Password must include at least one symbol.")

    normalized_username = normalize_login_username(username)
    if normalized_username and normalized_username in value.lower():
        errors.append("Password cannot contain your username.")

    return errors


def is_password_reused(user, new_password: str, history_count: int) -> bool:
    if user.check_password(new_password):
        return True

    if history_count <= 0:
        return False

    history_rows = (
        UserPasswordHistory.query.filter_by(user_id=user.id)
        .order_by(UserPasswordHistory.created_at.desc(), UserPasswordHistory.id.desc())
        .limit(history_count)
        .all()
    )
    for row in history_rows:
        if check_password_hash(row.password_hash, new_password):
            return True
    return False


def store_password_history(user_id: int, previous_password_hash: str, history_count: int) -> None:
    if not previous_password_hash:
        return

    db.session.add(
        UserPasswordHistory(
            user_id=user_id,
            password_hash=previous_password_hash,
            created_at=utc_now(),
        )
    )
    db.session.flush()

    if history_count <= 0:
        UserPasswordHistory.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        return

    keep_ids = (
        db.session.query(UserPasswordHistory.id)
        .filter(UserPasswordHistory.user_id == user_id)
        .order_by(UserPasswordHistory.created_at.desc(), UserPasswordHistory.id.desc())
        .limit(history_count)
        .all()
    )
    keep_id_set = {row.id for row in keep_ids}
    if not keep_id_set:
        return

    (
        UserPasswordHistory.query.filter(UserPasswordHistory.user_id == user_id)
        .filter(~UserPasswordHistory.id.in_(keep_id_set))
        .delete(synchronize_session=False)
    )


def _load_attempt(client_ip: str, username: str) -> LoginAttempt | None:
    return LoginAttempt.query.filter_by(client_ip=client_ip, username=username).first()


def _remove_expired_attempt(record: LoginAttempt, now: datetime, window_seconds: int) -> bool:
    if record.locked_until:
        locked_until = record.locked_until.replace(tzinfo=timezone.utc)
        if now >= locked_until:
            db.session.delete(record)
            return True
        return False

    first_attempt = record.first_attempt_at.replace(tzinfo=timezone.utc)
    if (now - first_attempt).total_seconds() > window_seconds:
        db.session.delete(record)
        return True

    return False


def _attempt_window_seconds() -> int:
    lockout_window = int(current_app.config.get("AUTH_LOCKOUT_WINDOW_SECONDS", 300))
    legacy_window = int(current_app.config.get("AUTH_ATTEMPT_WINDOW_SECONDS", 300))
    default_window = int(getattr(AppConfig, "AUTH_LOCKOUT_WINDOW_SECONDS", 300))
    if (
        "AUTH_LOCKOUT_WINDOW_SECONDS" in current_app.config
        and lockout_window != default_window
    ):
        return lockout_window
    return legacy_window


def _lockout_seconds() -> int:
    return int(current_app.config.get("AUTH_LOCKOUT_SECONDS", 600))


def _legacy_limit_was_overridden(config_key: str) -> bool:
    default_value = getattr(AppConfig, config_key, None)
    current_value = current_app.config.get(config_key)
    return (
        current_value != default_value
        or config_key in os.environ
    )


def _limit_for_scope(scope: str) -> int:
    fallback_max = int(current_app.config.get("AUTH_LOCKOUT_MAX_ATTEMPTS", 5))
    if scope == "ip_account":
        if _legacy_limit_was_overridden("AUTH_MAX_ATTEMPTS_IP_ACCOUNT"):
            return int(current_app.config.get("AUTH_MAX_ATTEMPTS_IP_ACCOUNT", fallback_max))
        return fallback_max
    if scope == "account":
        if _legacy_limit_was_overridden("AUTH_MAX_ATTEMPTS_ACCOUNT"):
            return int(current_app.config.get("AUTH_MAX_ATTEMPTS_ACCOUNT", fallback_max))
        return fallback_max
    if _legacy_limit_was_overridden("AUTH_MAX_ATTEMPTS_IP"):
        return int(current_app.config.get("AUTH_MAX_ATTEMPTS_IP", fallback_max))
    return fallback_max


def check_login_limited(client_ip: str, username: str) -> tuple[bool, int | None, str | None]:
    now = utc_now()
    window_seconds = _attempt_window_seconds()
    normalized_username = normalize_login_username(username)

    ip_record = _load_attempt(client_ip, IP_SCOPE_USERNAME)
    account_record = _load_attempt(ACCOUNT_SCOPE_IP, normalized_username)
    ip_account_record = _load_attempt(client_ip, normalized_username) if normalized_username else None

    mutated = False
    for record in (ip_record, account_record, ip_account_record):
        if record and _remove_expired_attempt(record, now, window_seconds):
            mutated = True

    if mutated:
        db.session.commit()
        ip_record = _load_attempt(client_ip, IP_SCOPE_USERNAME)
        account_record = _load_attempt(ACCOUNT_SCOPE_IP, normalized_username)
        ip_account_record = _load_attempt(client_ip, normalized_username) if normalized_username else None

    limited_seconds: int | None = None
    scope: str | None = None

    if ip_record and ip_record.locked_until:
        remaining = int((ip_record.locked_until.replace(tzinfo=timezone.utc) - now).total_seconds())
        if remaining > 0:
            limited_seconds = remaining
            scope = "ip"

    if account_record and account_record.locked_until:
        remaining = int((account_record.locked_until.replace(tzinfo=timezone.utc) - now).total_seconds())
        if remaining > 0 and (limited_seconds is None or remaining > limited_seconds):
            limited_seconds = remaining
            scope = "account"

    if ip_account_record and ip_account_record.locked_until:
        remaining = int((ip_account_record.locked_until.replace(tzinfo=timezone.utc) - now).total_seconds())
        if remaining > 0 and (limited_seconds is None or remaining > limited_seconds):
            limited_seconds = remaining
            scope = "ip_account"

    return limited_seconds is not None, limited_seconds, scope


def _record_failed_attempt(scope_ip: str, scope_username: str, now: datetime, scope: str) -> bool:
    max_attempts = _limit_for_scope(scope)
    window_seconds = _attempt_window_seconds()
    lockout_seconds = _lockout_seconds()

    record = _load_attempt(scope_ip, scope_username)
    if not record:
        record = LoginAttempt(
            client_ip=scope_ip,
            username=scope_username,
            attempt_count=1,
            first_attempt_at=now,
        )
        if max_attempts <= 1:
            record.locked_until = now + timedelta(seconds=lockout_seconds)
            db.session.add(record)
            return True
        db.session.add(record)
        return False

    if _remove_expired_attempt(record, now, window_seconds):
        record = LoginAttempt(
            client_ip=scope_ip,
            username=scope_username,
            attempt_count=1,
            first_attempt_at=now,
        )
        db.session.add(record)
        return False

    record.attempt_count += 1
    just_locked = False
    if record.attempt_count >= max_attempts:
        locked_until = now + timedelta(seconds=lockout_seconds)
        if not record.locked_until or record.locked_until.replace(tzinfo=timezone.utc) < now:
            just_locked = True
        record.locked_until = locked_until

    return just_locked


def record_failed_login(client_ip: str, username: str) -> dict[str, bool]:
    now = utc_now()
    normalized_username = normalize_login_username(username)
    ip_locked_now = _record_failed_attempt(client_ip, IP_SCOPE_USERNAME, now, "ip")
    account_locked_now = False
    ip_account_locked_now = False
    if normalized_username:
        account_locked_now = _record_failed_attempt(ACCOUNT_SCOPE_IP, normalized_username, now, "account")
        ip_account_locked_now = _record_failed_attempt(client_ip, normalized_username, now, "ip_account")

    db.session.commit()
    return {
        "ip_locked_now": ip_locked_now,
        "account_locked_now": account_locked_now,
        "ip_account_locked_now": ip_account_locked_now,
    }


def clear_failed_logins(client_ip: str, username: str) -> None:
    normalized_username = normalize_login_username(username)
    LoginAttempt.query.filter_by(client_ip=client_ip, username=IP_SCOPE_USERNAME).delete()
    if normalized_username:
        LoginAttempt.query.filter_by(client_ip=ACCOUNT_SCOPE_IP, username=normalized_username).delete()
        LoginAttempt.query.filter_by(client_ip=client_ip, username=normalized_username).delete()
    db.session.commit()


def prune_auth_events(retention_days: int) -> None:
    if retention_days <= 0:
        return

    cutoff = utc_now() - timedelta(days=retention_days)
    AuthEvent.query.filter(AuthEvent.created_at < cutoff).delete(synchronize_session=False)


def maybe_prune_auth_events() -> None:
    global _LAST_PRUNE_DATE

    today = utc_now().date()
    if _LAST_PRUNE_DATE == today:
        return

    retention_days = current_app.config.get("AUTH_EVENT_RETENTION_DAYS", 180)
    prune_auth_events(retention_days)
    _LAST_PRUNE_DATE = today


def record_auth_event(
    event_type: str,
    *,
    outcome: str = "success",
    user=None,
    username: str | None = None,
    client_ip: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        maybe_prune_auth_events()

        event = AuthEvent(
            event_type=event_type,
            outcome=outcome,
            user_id=(user.id if user else None),
            username=(username or (user.username if user else None)),
            client_ip=client_ip,
            created_at=utc_now(),
        )
        event.set_metadata(metadata)
        db.session.add(event)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Failed to persist auth event: %s", event_type)

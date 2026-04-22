from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app

from app import db
from app.services.twilio_service import get_twilio_service, record_usage_candidates
from app.utils import normalize_phone, validate_phone


_EVENT_LABELS = {
    "password_changed": "Your password was changed.",
    "admin_password_reset": "An administrator reset your password.",
    "account_lockout": "Your account was temporarily locked after multiple failed sign-in attempts.",
}


def _format_alert_message(event_type: str, username: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    event_text = _EVENT_LABELS.get(event_type, "A security event occurred on your account.")
    return (
        f"Twinevia security alert for {username}: {event_text} "
        f"Time: {stamp}. If this was not expected, contact an administrator immediately."
    )


def send_security_alert(user, event_type: str) -> dict:
    if not current_app.config.get("AUTH_ALERTS_ENABLED", True):
        return {"success": False, "skipped": True, "reason": "alerts_disabled"}

    if not user:
        return {"success": False, "skipped": True, "reason": "no_user"}

    phone = normalize_phone(user.phone or "")
    if not validate_phone(phone):
        return {"success": False, "skipped": True, "reason": "missing_or_invalid_phone"}

    try:
        service = get_twilio_service()
        body = _format_alert_message(event_type, user.username)
        result = service.send_message(phone, body, send_kind="auth_alert")
        if result.get("success"):
            organization_id = getattr(user, "organization_id", None)
            if organization_id:
                try:
                    record_usage_candidates(organization_id, [result], source="auth_alert")
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    current_app.logger.exception(
                        "Failed recording auth alert usage user_id=%s organization_id=%s sid=%s: %s",
                        getattr(user, "id", None),
                        organization_id,
                        result.get("sid"),
                        exc,
                    )
            else:
                current_app.logger.info(
                    "Auth alert sent without organization attribution user_id=%s username=%s sid=%s event_type=%s",
                    getattr(user, "id", None),
                    getattr(user, "username", None),
                    result.get("sid"),
                    event_type,
                )
            return {"success": True, "skipped": False, "reason": None}
        if result.get("skipped"):
            return {
                "success": False,
                "skipped": True,
                "reason": result.get("reason") or result.get("error") or "twilio_send_skipped",
            }
        return {
            "success": False,
            "skipped": False,
            "reason": result.get("error") or "twilio_send_failed",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "skipped": False,
            "reason": str(exc),
        }

from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app

from app.services.twilio_service import get_twilio_service
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
        f"AOC SMS security alert for {username}: {event_text} "
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
        result = service.send_message(phone, body)
        if result.get("success"):
            return {"success": True, "skipped": False, "reason": None}
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

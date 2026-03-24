from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

from flask import current_app

from app import db
from app.models import AppUser, PlatformServiceRestartRequest, utc_now
from app.services.auth_security_service import record_auth_event
from app.utils import as_utc_datetime


ACTIVE_RESTART_REQUEST_STATUSES = ("pending", "queued")


class PlatformServiceRestartError(RuntimeError):
    """Raised when the SaaS restart helper cannot be queued successfully."""


def _restart_script_path() -> str:
    configured_path = (current_app.config.get("PLATFORM_SERVICE_RESTART_SCRIPT") or "").strip()
    if not configured_path:
        raise PlatformServiceRestartError("Platform service restart script is not configured.")

    path = Path(configured_path)
    if not path.is_absolute():
        raise PlatformServiceRestartError("Platform service restart script must use an absolute path.")
    if not path.exists():
        raise PlatformServiceRestartError(f"Platform service restart script does not exist: {path}.")
    if not path.is_file():
        raise PlatformServiceRestartError(f"Platform service restart script must point to a file: {path}.")
    if not os.access(path, os.X_OK):
        raise PlatformServiceRestartError(f"Platform service restart script is not executable: {path}.")
    return str(path)


def _restart_timeout_seconds() -> int:
    return int(current_app.config.get("PLATFORM_SERVICE_RESTART_TIMEOUT_SECONDS", 15))


def _restart_stale_after() -> timedelta:
    return timedelta(seconds=int(current_app.config.get("PLATFORM_SERVICE_RESTART_STALE_AFTER_SECONDS", 300)))


def _summarize_process_output(*, stdout: str, stderr: str) -> str | None:
    for raw_value in (stderr, stdout):
        text = (raw_value or "").strip()
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1][:280]
    return None


def _parse_restart_helper_payload(*, stdout: str, stderr: str) -> dict[str, object]:
    for raw_value in (stdout, stderr):
        text = (raw_value or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise PlatformServiceRestartError("Platform restart helper returned invalid JSON.")


def _run_restart_helper_json(*args: str, timeout_seconds: int | None = None) -> dict[str, object]:
    script_path = _restart_script_path()
    command = ["sudo", "-n", script_path, *args]
    timeout = int(timeout_seconds or _restart_timeout_seconds())

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlatformServiceRestartError(
            "Timed out while contacting the SaaS restart helper."
        ) from exc
    except FileNotFoundError as exc:
        raise PlatformServiceRestartError(
            f"Platform restart helper is unavailable: {exc.filename or 'missing executable'}."
        ) from exc
    except OSError as exc:
        raise PlatformServiceRestartError(
            f"Failed to execute the platform restart helper: {exc}."
        ) from exc

    payload = _parse_restart_helper_payload(stdout=completed.stdout, stderr=completed.stderr)
    if "detail" not in payload or not payload.get("detail"):
        detail = _summarize_process_output(stdout=completed.stdout, stderr=completed.stderr)
        if detail:
            payload["detail"] = detail
    payload["script_path"] = script_path
    payload["returncode"] = completed.returncode
    return payload


def request_platform_service_restart(*, timeout_seconds: int | None = None) -> dict[str, object]:
    payload = _run_restart_helper_json(timeout_seconds=timeout_seconds)
    if str(payload.get("status") or "queued").strip().lower() != "queued":
        raise PlatformServiceRestartError(
            str(payload.get("detail") or payload.get("summary") or "Platform restart helper did not queue the restart.")
        )
    return payload


def request_platform_service_restart_status(
    transient_unit: str,
    *,
    timeout_seconds: int | None = None,
) -> dict[str, object]:
    if not (transient_unit or "").strip():
        raise PlatformServiceRestartError("Transient restart unit is required.")
    return _run_restart_helper_json("--status", transient_unit.strip(), timeout_seconds=timeout_seconds)


def active_platform_service_restart_request() -> PlatformServiceRestartRequest | None:
    return (
        PlatformServiceRestartRequest.query
        .filter(PlatformServiceRestartRequest.status.in_(ACTIVE_RESTART_REQUEST_STATUSES))
        .order_by(PlatformServiceRestartRequest.requested_at.desc(), PlatformServiceRestartRequest.id.desc())
        .first()
    )


def latest_platform_service_restart_request() -> PlatformServiceRestartRequest | None:
    return (
        PlatformServiceRestartRequest.query
        .order_by(PlatformServiceRestartRequest.requested_at.desc(), PlatformServiceRestartRequest.id.desc())
        .first()
    )


def _restart_request_auth_metadata(request_row: PlatformServiceRestartRequest) -> dict[str, object]:
    return {
        "request_id": request_row.id,
        "status": request_row.status,
        "transient_unit": request_row.transient_unit,
        "summary": request_row.summary,
        "detail": request_row.detail,
    }


def _record_terminal_restart_auth_event(request_row: PlatformServiceRestartRequest) -> None:
    user = db.session.get(AppUser, request_row.requested_by_user_id) if request_row.requested_by_user_id else None
    record_auth_event(
        "platform_service_restart",
        outcome="success" if request_row.status == "succeeded" else "failed",
        user=user,
        username=request_row.requested_username,
        client_ip=request_row.client_ip,
        metadata=_restart_request_auth_metadata(request_row),
    )


def enqueue_platform_service_restart_request(
    *,
    requested_by_user=None,
    client_ip: str | None = None,
) -> tuple[PlatformServiceRestartRequest, bool]:
    active_request = active_platform_service_restart_request()
    if active_request is not None:
        return active_request, False

    request_row = PlatformServiceRestartRequest(
        requested_by_user_id=(requested_by_user.id if requested_by_user is not None else None),
        requested_username=(
            requested_by_user.username
            if requested_by_user is not None and getattr(requested_by_user, "username", None)
            else None
        ),
        client_ip=client_ip,
        status="pending",
        summary="Restart request queued. Waiting for the host processor.",
        requested_at=utc_now(),
    )
    db.session.add(request_row)
    db.session.commit()
    current_app.logger.info(
        "Queued platform restart request id=%s user_id=%s username=%s client_ip=%s.",
        request_row.id,
        request_row.requested_by_user_id,
        request_row.requested_username,
        request_row.client_ip,
    )
    return request_row, True


def _finalize_restart_request(
    request_row: PlatformServiceRestartRequest,
    *,
    status: str,
    summary: str,
    detail: str | None,
    now,
) -> PlatformServiceRestartRequest:
    previous_status = request_row.status
    request_row.status = status
    request_row.summary = summary
    request_row.detail = detail
    request_row.completed_at = now
    request_row.last_checked_at = now
    if request_row.started_at is None:
        request_row.started_at = now
    db.session.commit()
    if previous_status != status and status in {"succeeded", "failed"}:
        _record_terminal_restart_auth_event(request_row)
    return request_row


def dispatch_platform_service_restart(
    request_row: PlatformServiceRestartRequest,
) -> PlatformServiceRestartRequest:
    if request_row.status != "pending":
        return request_row

    now = utc_now()
    try:
        payload = request_platform_service_restart()
        transient_unit = str(payload.get("transient_unit") or "").strip()
        if not transient_unit:
            raise PlatformServiceRestartError("Platform restart helper did not return a transient unit.")
        helper_status = str(payload.get("status") or "queued").strip().lower()
        if helper_status != "queued":
            raise PlatformServiceRestartError(
                str(payload.get("detail") or payload.get("summary") or "Platform restart helper did not queue the restart.")
            )

        request_row.status = "queued"
        request_row.attempt_count = int(request_row.attempt_count or 0) + 1
        request_row.transient_unit = transient_unit
        request_row.summary = str(payload.get("summary") or "Restart queued. The SaaS services will recycle shortly.")
        request_row.detail = (
            str(payload.get("detail")).strip()
            if payload.get("detail") is not None
            else None
        )
        if request_row.started_at is None:
            request_row.started_at = now
        request_row.last_checked_at = now
        db.session.commit()
        current_app.logger.info(
            "Dispatched platform restart request id=%s transient_unit=%s.",
            request_row.id,
            request_row.transient_unit,
        )
        return request_row
    except Exception as exc:
        db.session.rollback()
        persisted = db.session.get(PlatformServiceRestartRequest, request_row.id)
        if persisted is None:
            raise
        persisted.attempt_count = int(persisted.attempt_count or 0) + 1
        persisted.summary = "Restart request failed before queueing."
        persisted.detail = str(exc)
        current_app.logger.exception(
            "Failed to dispatch platform restart request id=%s.",
            request_row.id,
        )
        return _finalize_restart_request(
            persisted,
            status="failed",
            summary=persisted.summary,
            detail=persisted.detail,
            now=utc_now(),
        )


def refresh_platform_service_restart_status(
    request_row: PlatformServiceRestartRequest,
) -> PlatformServiceRestartRequest:
    if request_row.status != "queued":
        return request_row

    now = utc_now()
    reference_time = as_utc_datetime(request_row.started_at or request_row.requested_at)
    if not request_row.transient_unit:
        return _finalize_restart_request(
            request_row,
            status="failed",
            summary="Restart request lost its transient unit reference.",
            detail="Transient restart unit was missing before status polling.",
            now=now,
        )

    if reference_time is not None and now - reference_time > _restart_stale_after():
        return _finalize_restart_request(
            request_row,
            status="failed",
            summary="Restart request timed out before reaching a terminal state.",
            detail=f"Transient unit {request_row.transient_unit} did not report success or failure within the allowed window.",
            now=now,
        )

    try:
        payload = request_platform_service_restart_status(request_row.transient_unit)
        helper_status = str(payload.get("status") or "").strip().lower()
        summary = str(payload.get("summary") or "").strip()
        detail = str(payload.get("detail")).strip() if payload.get("detail") is not None else None

        if helper_status == "queued":
            request_row.summary = summary or "Restart queued. The SaaS services are restarting."
            request_row.detail = detail
            request_row.last_checked_at = now
            db.session.commit()
            return request_row

        if helper_status == "succeeded":
            return _finalize_restart_request(
                request_row,
                status="succeeded",
                summary=summary or "Restart completed successfully.",
                detail=detail,
                now=now,
            )

        if helper_status == "failed":
            return _finalize_restart_request(
                request_row,
                status="failed",
                summary=summary or "Restart failed.",
                detail=detail,
                now=now,
            )

        raise PlatformServiceRestartError(
            f"Platform restart helper returned an unknown status {helper_status!r}."
        )
    except Exception as exc:
        db.session.rollback()
        persisted = db.session.get(PlatformServiceRestartRequest, request_row.id)
        if persisted is None:
            raise
        current_app.logger.exception(
            "Failed to refresh platform restart request id=%s transient_unit=%s.",
            request_row.id,
            request_row.transient_unit,
        )
        return _finalize_restart_request(
            persisted,
            status="failed",
            summary="Restart status check failed.",
            detail=str(exc),
            now=utc_now(),
        )


def process_platform_service_restart_queue() -> dict[str, object]:
    summary: dict[str, object] = {
        "processed": 0,
        "mode": "idle",
        "request_id": None,
        "status": None,
    }

    pending_request = (
        PlatformServiceRestartRequest.query
        .filter_by(status="pending")
        .order_by(PlatformServiceRestartRequest.requested_at.asc(), PlatformServiceRestartRequest.id.asc())
        .first()
    )
    if pending_request is not None:
        updated = dispatch_platform_service_restart(pending_request)
        summary.update(
            {
                "processed": 1,
                "mode": "dispatch",
                "request_id": updated.id,
                "status": updated.status,
            }
        )
        return summary

    queued_request = (
        PlatformServiceRestartRequest.query
        .filter_by(status="queued")
        .order_by(PlatformServiceRestartRequest.requested_at.asc(), PlatformServiceRestartRequest.id.asc())
        .first()
    )
    if queued_request is not None:
        updated = refresh_platform_service_restart_status(queued_request)
        summary.update(
            {
                "processed": 1,
                "mode": "poll",
                "request_id": updated.id,
                "status": updated.status,
            }
        )
    return summary

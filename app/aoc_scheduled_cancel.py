from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import func, select, update

from app import create_app, db
from app.models import CommunityMember, Event, EventRegistration, Organization, ScheduledMessage


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _scheduled_message_record(message: Mapping[str, Any]) -> dict[str, Any]:
    event = None
    if message["event_id"] is not None:
        event = db.session.execute(
            select(
                Event.id,
                Event.title,
                Event.external_source,
                Event.external_event_id,
            ).where(
                Event.id == int(message["event_id"]),
                Event.organization_id == int(message["organization_id"]),
            )
        ).mappings().one_or_none()
    if message["target"] == "community":
        recipient_count = db.session.scalar(
            select(func.count(CommunityMember.id)).where(
                CommunityMember.organization_id == int(message["organization_id"])
            )
        )
        audience_filter = "all community members in the organization at dispatch time"
    else:
        recipient_count = db.session.scalar(
            select(func.count(EventRegistration.id)).where(
                EventRegistration.organization_id == int(message["organization_id"]),
                EventRegistration.event_id == int(message["event_id"]),
            )
        )
        audience_filter = f"all registrations for event_id={message['event_id']} at dispatch time"

    return {
        "attempt_count": int(message["attempt_count"] or 0),
        "audience": {
            "current_recipient_count": int(recipient_count or 0),
            "filter": audience_filter,
            "target": str(message["target"]),
            "test_mode": bool(message["test_mode"]),
            "test_recipient_selection_mode": message["test_recipient_selection_mode"],
        },
        "automation": {
            "key": message["automation_key"],
            "kind": message["automation_kind"],
            "source": message["automation_source"],
        },
        "created_at": _serialize_datetime(message["created_at"]),
        "event": {
            "external_event_id": event["external_event_id"] if event is not None else None,
            "external_source": event["external_source"] if event is not None else None,
            "id": message["event_id"],
            "title": event["title"] if event is not None else None,
        },
        "last_attempt_at": _serialize_datetime(message["last_attempt_at"]),
        "message_body": str(message["message_body"]),
        "message_log_id": message["message_log_id"],
        "next_retry_at": _serialize_datetime(message["next_retry_at"]),
        "scheduled_at": _serialize_datetime(message["scheduled_at"]),
        "scheduled_message_id": int(message["id"]),
        "status_before_cancellation": str(message["status"]),
    }


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary_file:
        json.dump(payload, temporary_file, indent=2, sort_keys=True)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def cancel_dispatchable_messages(
    organization_slug: str,
    expected_count: int,
    record_path: Path,
) -> dict[str, Any]:
    organization = db.session.execute(
        select(Organization.id, Organization.name, Organization.slug)
        .where(Organization.slug == organization_slug)
        .with_for_update()
    ).mappings().one_or_none()
    if organization is None:
        raise RuntimeError(f"Organization {organization_slug!r} was not found.")

    messages = list(
        db.session.execute(
            select(
                ScheduledMessage.id,
                ScheduledMessage.organization_id,
                ScheduledMessage.created_at,
                ScheduledMessage.scheduled_at,
                ScheduledMessage.message_body,
                ScheduledMessage.target,
                ScheduledMessage.event_id,
                ScheduledMessage.status,
                ScheduledMessage.test_mode,
                ScheduledMessage.test_recipient_selection_mode,
                ScheduledMessage.attempt_count,
                ScheduledMessage.last_attempt_at,
                ScheduledMessage.next_retry_at,
                ScheduledMessage.message_log_id,
                ScheduledMessage.automation_source,
                ScheduledMessage.automation_key,
                ScheduledMessage.automation_kind,
            )
            .where(
                ScheduledMessage.organization_id == int(organization["id"]),
                ScheduledMessage.status.in_(("pending", "processing")),
            )
            .order_by(ScheduledMessage.id)
            .with_for_update()
        ).mappings()
    )
    if len(messages) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} dispatchable AOC messages, found {len(messages)}. No messages were changed."
        )

    recorded_at = datetime.now(timezone.utc).isoformat()
    records = [_scheduled_message_record(message) for message in messages]
    payload: dict[str, Any] = {
        "cancellation_state": "prepared",
        "expected_count": expected_count,
        "organization": {
            "id": int(organization["id"]),
            "name": organization["name"],
            "slug": organization["slug"],
        },
        "queue_verification": "Scheduled messages dispatch synchronously from the systemd scheduler and do not create RQ jobs.",
        "recorded_at": recorded_at,
        "scheduled_messages": records,
    }
    _write_private_json(record_path, payload)

    message_ids = [int(message["id"]) for message in messages]
    update_result = db.session.execute(
        update(ScheduledMessage)
        .where(
            ScheduledMessage.organization_id == int(organization["id"]),
            ScheduledMessage.id.in_(message_ids),
            ScheduledMessage.status.in_(("pending", "processing")),
        )
        .values(
            status="cancelled",
            processing_started_at=None,
            next_retry_at=None,
            error_message="Canceled for the Twinevia managed-pilot launch; recreation requires explicit approval.",
        )
    )
    if int(update_result.rowcount or 0) != expected_count:
        db.session.rollback()
        raise RuntimeError("AOC scheduled-message state changed during cancellation. No cancellation was committed.")
    db.session.commit()

    remaining_count = db.session.scalar(
        select(func.count(ScheduledMessage.id)).where(
            ScheduledMessage.organization_id == int(organization["id"]),
            ScheduledMessage.status.in_(("pending", "processing")),
        )
    )
    if int(remaining_count or 0) != 0:
        raise RuntimeError("AOC scheduled-message cancellation verification failed.")

    payload["cancellation_state"] = "confirmed"
    payload["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    payload["dispatchable_count_after_cancellation"] = 0
    _write_private_json(record_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Record and cancel AOC scheduled messages for launch maintenance.")
    parser.add_argument("--confirm-organization-slug", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    args = parser.parse_args()

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    record_path = Path(str(app.config.get("AOC_SCHEDULED_CANCELLATION_RECORD_FILE") or ""))
    if not record_path.is_absolute() or record_path == Path("/"):
        raise RuntimeError("AOC_SCHEDULED_CANCELLATION_RECORD_FILE must be a dedicated absolute file path.")
    with app.app_context():
        payload = cancel_dispatchable_messages(
            args.confirm_organization_slug,
            args.expected_count,
            record_path,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

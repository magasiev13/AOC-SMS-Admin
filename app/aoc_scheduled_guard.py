from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text

from app import create_app, db


def _validated_record_message_ids(organization_slug: str, record_path: Path) -> tuple[list[int], str | None]:
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        message_rows = list(payload["scheduled_messages"])
        message_ids = [int(row["scheduled_message_id"]) for row in message_rows]
        if payload["cancellation_state"] != "confirmed":
            return [], "cancellation record is not confirmed"
        expected_count = int(payload["expected_count"])
        if expected_count < 1 or expected_count > 100 or len(message_ids) != expected_count:
            return [], "cancellation record does not contain the expected messages"
        if str(payload["organization"]["slug"]) != organization_slug:
            return [], "cancellation record organization does not match"
        if int(payload["dispatchable_count_after_cancellation"]) != 0:
            return [], "cancellation record did not verify zero dispatchable messages"
        return message_ids, None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return [], f"cancellation record is unavailable or invalid: {exc}"


def build_dispatchable_report(organization_slug: str, record_path: Path) -> dict[str, Any]:
    record_message_ids, record_error = _validated_record_message_ids(organization_slug, record_path)
    organization_row = db.session.execute(
        text("SELECT id, slug FROM organizations WHERE slug = :slug"),
        {"slug": organization_slug},
    ).mappings().one_or_none()
    if organization_row is None:
        return {
            "dispatchable_count": 0,
            "dispatchable_ids": [],
            "organization_found": False,
            "organization_slug": organization_slug,
            "record_confirmed": False,
            "record_error": record_error or "organization was not found",
        }

    messages = list(
        db.session.execute(
            text(
                """
                SELECT id, status
                FROM scheduled_messages
                WHERE organization_id = :organization_id
                  AND status IN ('pending', 'processing')
                ORDER BY id
                """
            ),
            {"organization_id": int(organization_row["id"])},
        ).mappings()
    )
    recorded_statuses: dict[int, str] = {}
    if record_message_ids:
        recorded_rows = db.session.execute(
            text(
                """
                SELECT id, status
                FROM scheduled_messages
                WHERE organization_id = :organization_id
                  AND id IN :message_ids
                """
            ).bindparams(bindparam("message_ids", expanding=True)),
            {
                "organization_id": int(organization_row["id"]),
                "message_ids": record_message_ids,
            },
        ).mappings()
        recorded_statuses = {int(row["id"]): str(row["status"]) for row in recorded_rows}
    record_confirmed = (
        record_error is None
        and len(recorded_statuses) == len(record_message_ids)
        and all(recorded_statuses.get(message_id) == "cancelled" for message_id in record_message_ids)
    )
    return {
        "dispatchable_count": len(messages),
        "dispatchable_ids": [int(message["id"]) for message in messages],
        "organization_found": True,
        "organization_slug": str(organization_row["slug"]),
        "record_confirmed": record_confirmed,
        "record_error": record_error,
        "recorded_statuses": recorded_statuses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Assert that AOC has no dispatchable scheduled messages.")
    parser.add_argument("--organization-slug", required=True)
    parser.add_argument("--expect-dispatchable-count", required=True, type=int)
    args = parser.parse_args()

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        record_path = Path(str(app.config.get("AOC_SCHEDULED_CANCELLATION_RECORD_FILE") or ""))
        report = build_dispatchable_report(args.organization_slug, record_path)
    print(json.dumps(report, indent=2, sort_keys=True))
    sys.exit(
        0
        if report["dispatchable_count"] == args.expect_dispatchable_count and report["record_confirmed"] is True
        else 1
    )


if __name__ == "__main__":
    main()

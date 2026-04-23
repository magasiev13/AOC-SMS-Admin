from __future__ import annotations

import json

from app import db
from app.models import (
    AppUser,
    Organization,
    OrganizationMembership,
    OrganizationSettingsAuditLog,
    OrganizationTestRecipient,
)
from app.services.recipient_service import load_recipient_snapshot
from app.utils import normalize_phone, validate_phone


TEST_RECIPIENT_AUDIT_CATEGORY = "test_recipients"
TEST_RECIPIENT_SELECTION_ONE = "one"
TEST_RECIPIENT_SELECTION_ALL = "all"
TEST_RECIPIENT_SELECTION_MODES = {
    TEST_RECIPIENT_SELECTION_ONE,
    TEST_RECIPIENT_SELECTION_ALL,
}
TEST_RECIPIENT_MAX_COUNT = 25


def normalize_test_recipient_selection_mode(value: str | None) -> str:
    normalized = (value or TEST_RECIPIENT_SELECTION_ONE).strip().lower()
    if normalized not in TEST_RECIPIENT_SELECTION_MODES:
        return TEST_RECIPIENT_SELECTION_ONE
    return normalized


def mask_phone_for_audit(phone: str | None) -> str:
    normalized = normalize_phone(phone)
    if not normalized:
        return "unknown"

    digits = "".join(character for character in normalized if character.isdigit())
    if len(digits) <= 4:
        return f"+***{digits}"
    return f"+{digits[:1]}***{digits[-4:]}"


def _masked_phone_list(rows: list[OrganizationTestRecipient] | list[dict]) -> list[str]:
    masked = []
    seen = set()
    for row in rows:
        phone = row.phone if isinstance(row, OrganizationTestRecipient) else row.get("phone")
        value = mask_phone_for_audit(phone)
        if value in seen:
            continue
        seen.add(value)
        masked.append(value)
    return sorted(masked)


def _recipient_query(organization_id: int):
    return (
        OrganizationTestRecipient.query
        .filter_by(organization_id=organization_id)
        .order_by(OrganizationTestRecipient.created_at.asc(), OrganizationTestRecipient.id.asc())
    )


def list_test_recipients(organization_id: int) -> list[OrganizationTestRecipient]:
    return _recipient_query(organization_id).all()


def count_test_recipients(organization_id: int) -> int:
    return _recipient_query(organization_id).count()


def test_recipient_view_rows(organization_id: int) -> list[dict[str, str]]:
    rows = []
    for recipient in list_test_recipients(organization_id):
        rows.append(
            {
                "phone": recipient.phone,
                "label": recipient.label or "",
                "display_label": recipient.label or recipient.phone,
            }
        )
    return rows


def recent_test_recipient_audit_entries(
    organization_id: int,
    *,
    limit: int = 5,
) -> list[OrganizationSettingsAuditLog]:
    return (
        OrganizationSettingsAuditLog.query
        .filter_by(
            organization_id=organization_id,
            category=TEST_RECIPIENT_AUDIT_CATEGORY,
        )
        .order_by(
            OrganizationSettingsAuditLog.created_at.desc(),
            OrganizationSettingsAuditLog.id.desc(),
        )
        .limit(limit)
        .all()
    )


def normalize_test_recipient_entries(entries: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    normalized_entries: list[dict[str, str | None]] = []
    seen_phones: set[str] = set()

    for entry in entries:
        raw_phone = (entry.get("phone") or "").strip()
        raw_label = (entry.get("label") or "").strip()
        if not raw_phone and not raw_label:
            continue

        normalized_phone = normalize_phone(raw_phone)
        if not validate_phone(normalized_phone):
            raise ValueError("Each test recipient must use a valid E.164 phone number.")
        if normalized_phone in seen_phones:
            continue

        normalized_entries.append(
            {
                "phone": normalized_phone,
                "label": raw_label[:120] or None,
            }
        )
        seen_phones.add(normalized_phone)

    if len(normalized_entries) > TEST_RECIPIENT_MAX_COUNT:
        raise ValueError(
            f"Save up to {TEST_RECIPIENT_MAX_COUNT} internal test recipients per workspace."
        )

    return normalized_entries


def _record_settings_audit(
    *,
    organization_id: int,
    actor_user_id: int | None,
    action: str,
    metadata: dict[str, object],
) -> None:
    db.session.add(
        OrganizationSettingsAuditLog(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            category=TEST_RECIPIENT_AUDIT_CATEGORY,
            action=action,
            metadata_json=json.dumps(metadata, sort_keys=True),
        )
    )


def replace_test_recipients(
    organization_id: int,
    entries: list[dict[str, str | None]],
    *,
    actor_user_id: int | None = None,
) -> list[OrganizationTestRecipient]:
    organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise ValueError(f"Organization {organization_id} was not found.")

    normalized_entries = normalize_test_recipient_entries(entries)
    before_rows = list_test_recipients(organization_id)

    for row in before_rows:
        db.session.delete(row)
    db.session.flush()

    for entry in normalized_entries:
        db.session.add(
            OrganizationTestRecipient(
                organization_id=organization_id,
                phone=entry["phone"],
                label=entry["label"],
            )
        )

    _record_settings_audit(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="replace",
        metadata={
            "before_count": len(before_rows),
            "after_count": len(normalized_entries),
            "before_phones": _masked_phone_list(before_rows),
            "after_phones": _masked_phone_list(normalized_entries),
        },
    )
    db.session.flush()
    return list_test_recipients(organization_id)


def upsert_test_recipient(
    organization_id: int,
    *,
    phone: str | None,
    label: str | None = None,
) -> OrganizationTestRecipient | None:
    normalized_phone = normalize_phone(phone)
    if not validate_phone(normalized_phone):
        return None

    row = OrganizationTestRecipient.query.filter_by(
        organization_id=organization_id,
        phone=normalized_phone,
    ).first()
    normalized_label = (label or "").strip()[:120] or None
    if row is None:
        row = OrganizationTestRecipient(
            organization_id=organization_id,
            phone=normalized_phone,
            label=normalized_label,
        )
        db.session.add(row)
        db.session.flush()
        return row

    if not row.label and normalized_label:
        row.label = normalized_label
        db.session.flush()
    return row


def seed_owner_test_recipient(organization_id: int, user: AppUser | None) -> OrganizationTestRecipient | None:
    if user is None:
        return None
    label = (user.full_name or user.username or "").strip() or None
    return upsert_test_recipient(
        organization_id,
        phone=user.phone,
        label=label,
    )


def seed_test_recipients_from_owner_phones(organization_id: int) -> list[OrganizationTestRecipient]:
    owner_users = (
        AppUser.query
        .join(OrganizationMembership, OrganizationMembership.user_id == AppUser.id)
        .filter(OrganizationMembership.organization_id == organization_id)
        .filter(OrganizationMembership.role == "owner")
        .order_by(AppUser.id.asc())
        .all()
    )
    for user in owner_users:
        seed_owner_test_recipient(organization_id, user)
    db.session.flush()
    return list_test_recipients(organization_id)


def resolve_workspace_test_recipients(
    organization_id: int,
    *,
    selection_mode: str | None,
    selected_phone: str | None = None,
) -> list[dict[str, str]]:
    rows = list_test_recipients(organization_id)
    if not rows:
        raise ValueError("No saved test recipients are configured for this workspace.")

    normalized_mode = normalize_test_recipient_selection_mode(selection_mode)
    if normalized_mode == TEST_RECIPIENT_SELECTION_ALL:
        return [
            {"phone": row.phone, "name": row.label or ""}
            for row in rows
        ]

    if len(rows) == 1:
        row = rows[0]
        return [{"phone": row.phone, "name": row.label or ""}]

    normalized_selected_phone = normalize_phone(selected_phone)
    if not validate_phone(normalized_selected_phone):
        raise ValueError("Choose one saved test recipient before sending.")

    for row in rows:
        if row.phone == normalized_selected_phone:
            return [{"phone": row.phone, "name": row.label or ""}]

    raise ValueError("Choose a valid saved test recipient for test mode.")


def build_test_recipient_snapshot(
    organization_id: int,
    *,
    selection_mode: str | None,
    selected_phone: str | None = None,
) -> tuple[str, str, list[dict[str, str]]]:
    normalized_mode = normalize_test_recipient_selection_mode(selection_mode)
    recipients = resolve_workspace_test_recipients(
        organization_id,
        selection_mode=normalized_mode,
        selected_phone=selected_phone,
    )
    return normalized_mode, json.dumps(recipients, sort_keys=True), recipients


def load_test_recipient_snapshot(snapshot_json: str | None) -> list[dict[str, str]]:
    return load_recipient_snapshot(
        snapshot_json,
        missing_message="This scheduled test message predates saved test-recipient snapshots. Recreate it to continue.",
        invalid_message="This scheduled test message has an invalid recipient snapshot. Recreate it to continue.",
        unusable_message="This scheduled test message does not have a usable recipient snapshot. Recreate it to continue.",
    )

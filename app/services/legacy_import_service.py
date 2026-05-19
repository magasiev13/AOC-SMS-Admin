from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import inspect, text

from app import create_app, db
from app.models import (
    AppUser,
    AuthEvent,
    CommunityMember,
    Event,
    EventRegistration,
    InboxMessage,
    InboxThread,
    KeywordAutomationRule,
    MessageLog,
    Organization,
    OrganizationMembership,
    OrganizationMessagingProfile,
    OrganizationSubscription,
    ScheduledMessage,
    SuppressedContact,
    SurveyFlow,
    SurveyResponse,
    SurveySession,
    UnsubscribedContact,
    new_session_nonce,
    slugify_organization_name,
    utc_now,
)


LOGGER = logging.getLogger(__name__)


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value

    raw_value = str(value).strip()
    if not raw_value:
        return None

    normalized = raw_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_date(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed.date()
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _legacy_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _legacy_rows(connection: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
    if not _legacy_table_exists(connection, table_name):
        return []
    return connection.execute(f"SELECT * FROM {table_name} ORDER BY id").fetchall()


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _require_import_audit_ready() -> None:
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if "saas_import_runs" not in table_names:
        raise RuntimeError(
            "SaaS import audit table is missing. Run `python -m app.saas_db --apply` before importing."
        )


def _require_import_ready() -> None:
    _require_import_audit_ready()
    if Organization.query.first() is not None or OrganizationMembership.query.first() is not None:
        raise RuntimeError("Target SaaS database already contains organization data.")
    existing_non_platform_users = AppUser.query.filter_by(is_platform_admin=False).count()
    if existing_non_platform_users:
        raise RuntimeError("Target SaaS database already contains non-platform users.")


def _start_import_run(source_db_path: str) -> int:
    result = db.session.execute(
        text(
            """
            INSERT INTO saas_import_runs (source_db_path, status, started_at)
            VALUES (:source_db_path, :status, :started_at)
            RETURNING id
            """
        ),
        {
            "source_db_path": source_db_path,
            "status": "processing",
            "started_at": utc_now(),
        },
    )
    run_id = int(result.scalar_one())
    db.session.commit()
    return run_id


def _finish_import_run(
    run_id: int,
    *,
    organization_id: int | None,
    status: str,
    counts: dict[str, int],
    error_message: str | None = None,
) -> None:
    db.session.execute(
        text(
            """
            UPDATE saas_import_runs
            SET organization_id = :organization_id,
                status = :status,
                row_counts_json = :row_counts_json,
                completed_at = :completed_at,
                error_message = :error_message
            WHERE id = :run_id
            """
        ),
        {
            "organization_id": organization_id,
            "status": status,
            "row_counts_json": json.dumps(counts, sort_keys=True),
            "completed_at": utc_now(),
            "error_message": error_message,
            "run_id": run_id,
        },
    )
    db.session.commit()


def _new_import_counts() -> dict[str, int]:
    return {
        "users": 0,
        "community_members": 0,
        "events": 0,
        "event_registrations": 0,
        "unsubscribed_contacts": 0,
        "suppressed_contacts": 0,
        "message_logs": 0,
        "scheduled_messages": 0,
        "inbox_threads": 0,
        "inbox_messages": 0,
        "keyword_automation_rules": 0,
        "survey_flows": 0,
        "survey_sessions": 0,
        "survey_responses": 0,
        "auth_events": 0,
    }


def _normalized_username_remaps(username_remaps: dict[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    target_usernames: set[str] = set()
    for legacy_username, target_username in (username_remaps or {}).items():
        source = str(legacy_username or "").strip()
        target = str(target_username or "").strip()
        if not source or not target:
            raise RuntimeError("Username remaps must use non-empty source and target usernames.")
        source_key = source.lower()
        target_key = target.lower()
        if source_key in normalized and normalized[source_key].lower() != target_key:
            raise RuntimeError(f"Username remap for {source!r} is defined more than once.")
        if target_key in target_usernames and normalized.get(source_key, "").lower() != target_key:
            raise RuntimeError(f"Username remap target {target!r} is used more than once.")
        normalized[source_key] = target
        target_usernames.add(target_key)
    return normalized


def _mapped_legacy_username(username: Any, username_remaps: dict[str, str]) -> str:
    normalized = str(username or "").strip()
    if not normalized:
        raise RuntimeError("Legacy user is missing username.")
    return username_remaps.get(normalized.lower(), normalized)


def _assert_import_org_slug_available(slug: str) -> None:
    if Organization.query.filter_by(slug=slug).first() is not None:
        raise RuntimeError(f"Organization slug {slug!r} already exists in the target SaaS database.")


def _assert_username_available(username: str) -> None:
    existing_user = AppUser.query.filter(db.func.lower(AppUser.username) == username.lower()).first()
    if existing_user is not None:
        raise RuntimeError(f"Username conflict importing legacy user {username!r}. Add an explicit username remap.")


def _assert_email_available(email: str | None) -> None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return
    existing_user = AppUser.query.filter(db.func.lower(AppUser.email) == normalized).first()
    if existing_user is not None:
        raise RuntimeError(f"Email conflict importing legacy user {normalized!r}.")


def _apply_model_values(model: Any, values: dict[str, Any]) -> bool:
    changed = False
    for field_name, field_value in values.items():
        if getattr(model, field_name) != field_value:
            setattr(model, field_name, field_value)
            changed = True
    return changed


def _counts_for_organization(organization_id: int) -> dict[str, int]:
    return {
        "users": (
            AppUser.query.join(OrganizationMembership)
            .filter(OrganizationMembership.organization_id == organization_id)
            .count()
        ),
        "community_members": CommunityMember.query.filter_by(organization_id=organization_id).count(),
        "events": Event.query.filter_by(organization_id=organization_id).count(),
        "event_registrations": EventRegistration.query.filter_by(organization_id=organization_id).count(),
        "unsubscribed_contacts": UnsubscribedContact.query.filter_by(organization_id=organization_id).count(),
        "suppressed_contacts": SuppressedContact.query.filter_by(organization_id=organization_id).count(),
        "message_logs": MessageLog.query.filter_by(organization_id=organization_id).count(),
        "scheduled_messages": ScheduledMessage.query.filter_by(organization_id=organization_id).count(),
        "inbox_threads": InboxThread.query.filter_by(organization_id=organization_id).count(),
        "inbox_messages": InboxMessage.query.filter_by(organization_id=organization_id).count(),
        "keyword_automation_rules": KeywordAutomationRule.query.filter_by(organization_id=organization_id).count(),
        "survey_flows": SurveyFlow.query.filter_by(organization_id=organization_id).count(),
        "survey_sessions": SurveySession.query.filter_by(organization_id=organization_id).count(),
        "survey_responses": SurveyResponse.query.filter_by(organization_id=organization_id).count(),
        "auth_events": AuthEvent.query.filter_by(organization_id=organization_id).count(),
    }


def _count_mismatches(source_counts: dict[str, int], target_counts: dict[str, int]) -> dict[str, dict[str, int]]:
    mismatches: dict[str, dict[str, int]] = {}
    for key, source_count in source_counts.items():
        target_count = int(target_counts.get(key, 0))
        if source_count != target_count:
            mismatches[key] = {
                "source": int(source_count),
                "target": target_count,
                "delta": int(source_count) - target_count,
            }
    return mismatches


def _new_sync_operations() -> dict[str, dict[str, int]]:
    return {
        table_name: {"created": 0, "updated": 0, "skipped": 0}
        for table_name in _new_import_counts().keys()
    }


def _record_sync_operation(
    operations: dict[str, dict[str, int]],
    table_name: str,
    outcome: str,
) -> None:
    operations.setdefault(table_name, {"created": 0, "updated": 0, "skipped": 0})
    operations[table_name][outcome] = int(operations[table_name].get(outcome, 0)) + 1


def _latest_completed_import_at(organization_id: int) -> datetime | None:
    row = db.session.execute(
        text(
            """
            SELECT completed_at
            FROM saas_import_runs
            WHERE organization_id = :organization_id
              AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
            """
        ),
        {"organization_id": organization_id},
    ).first()
    if row is None:
        return None
    return _parse_datetime(row[0])


def _import_legacy_rows_into_organization(
    legacy_connection: sqlite3.Connection,
    *,
    organization: Organization,
    counts: dict[str, int],
    username_remaps: dict[str, str] | None = None,
) -> None:
    normalized_remaps = _normalized_username_remaps(username_remaps)
    user_id_map: dict[int, int] = {}
    event_id_map: dict[int, int] = {}
    log_id_map: dict[int, int] = {}
    thread_id_map: dict[int, int] = {}
    survey_id_map: dict[int, int] = {}
    session_id_map: dict[int, int] = {}
    imported_usernames_lower: set[str] = set()
    imported_emails_lower: set[str] = set()
    imported_message_sids: set[str] = set()

    for row in _legacy_rows(legacy_connection, "users"):
        password_hash = _row_value(row, "password_hash")
        if not password_hash:
            raise RuntimeError(f"Legacy user {row['id']} is missing password_hash.")

        role = (_row_value(row, "role", "admin") or "admin").strip().lower()
        if role not in {"admin", "social_manager"}:
            role = "admin"

        username = _mapped_legacy_username(_row_value(row, "username"), normalized_remaps)
        username_key = username.lower()
        if username_key in imported_usernames_lower:
            raise RuntimeError(f"Duplicate username {username!r} encountered during legacy import.")
        _assert_username_available(username)

        email = (_row_value(row, "email") or "").strip().lower() or None
        if email:
            if email in imported_emails_lower:
                raise RuntimeError(f"Duplicate email {email!r} encountered during legacy import.")
            _assert_email_available(email)

        user = AppUser(
            username=username,
            email=email,
            full_name=_row_value(row, "full_name"),
            password_hash=password_hash,
            phone=_row_value(row, "phone"),
            role=role,
            is_platform_admin=False,
            must_change_password=_bool_value(_row_value(row, "must_change_password")),
            session_nonce=_row_value(row, "session_nonce") or new_session_nonce(),
            created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="owner" if role == "admin" else "staff",
            )
        )
        user_id_map[int(row["id"])] = user.id
        imported_usernames_lower.add(username_key)
        if email:
            imported_emails_lower.add(email)
        counts["users"] += 1

    for row in _legacy_rows(legacy_connection, "community_members"):
        db.session.add(
            CommunityMember(
                organization_id=organization.id,
                name=_row_value(row, "name"),
                phone=_row_value(row, "phone"),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            )
        )
        counts["community_members"] += 1

    for row in _legacy_rows(legacy_connection, "events"):
        event = Event(
            organization_id=organization.id,
            title=_row_value(row, "title"),
            date=_parse_date(_row_value(row, "date")),
            created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
        )
        db.session.add(event)
        db.session.flush()
        event_id_map[int(row["id"])] = event.id
        counts["events"] += 1

    for row in _legacy_rows(legacy_connection, "message_logs"):
        legacy_event_id = _row_value(row, "event_id")
        message_log = MessageLog(
            organization_id=organization.id,
            created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            message_body=_row_value(row, "message_body"),
            target=_row_value(row, "target"),
            event_id=event_id_map.get(int(legacy_event_id)) if legacy_event_id is not None else None,
            status=_row_value(row, "status", "sent"),
            total_recipients=int(_row_value(row, "total_recipients", 0) or 0),
            success_count=int(_row_value(row, "success_count", 0) or 0),
            failure_count=int(_row_value(row, "failure_count", 0) or 0),
            details=_row_value(row, "details"),
        )
        db.session.add(message_log)
        db.session.flush()
        log_id_map[int(row["id"])] = message_log.id
        counts["message_logs"] += 1

    for row in _legacy_rows(legacy_connection, "event_registrations"):
        legacy_event_id = _row_value(row, "event_id")
        mapped_event_id = event_id_map.get(int(legacy_event_id)) if legacy_event_id is not None else None
        if mapped_event_id is None:
            continue
        db.session.add(
            EventRegistration(
                organization_id=organization.id,
                event_id=mapped_event_id,
                name=_row_value(row, "name"),
                phone=_row_value(row, "phone"),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            )
        )
        counts["event_registrations"] += 1

    for row in _legacy_rows(legacy_connection, "unsubscribed_contacts"):
        db.session.add(
            UnsubscribedContact(
                organization_id=organization.id,
                name=_row_value(row, "name"),
                phone=_row_value(row, "phone"),
                reason=_row_value(row, "reason"),
                source=_row_value(row, "source", "manual"),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            )
        )
        counts["unsubscribed_contacts"] += 1

    for row in _legacy_rows(legacy_connection, "suppressed_contacts"):
        legacy_source_log_id = _row_value(row, "source_message_log_id")
        db.session.add(
            SuppressedContact(
                organization_id=organization.id,
                phone=_row_value(row, "phone"),
                reason=_row_value(row, "reason"),
                category=_row_value(row, "category", "hard_fail"),
                source=_row_value(row, "source"),
                source_type=_row_value(row, "source_type"),
                source_message_log_id=(
                    log_id_map.get(int(legacy_source_log_id))
                    if legacy_source_log_id is not None
                    else None
                ),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
                updated_at=_parse_datetime(_row_value(row, "updated_at")) or utc_now(),
            )
        )
        counts["suppressed_contacts"] += 1

    for row in _legacy_rows(legacy_connection, "inbox_threads"):
        thread = InboxThread(
            organization_id=organization.id,
            phone=_row_value(row, "phone"),
            contact_name=_row_value(row, "contact_name"),
            unread_count=int(_row_value(row, "unread_count", 0) or 0),
            last_message_at=_parse_datetime(_row_value(row, "last_message_at")) or utc_now(),
            last_message_preview=_row_value(row, "last_message_preview"),
            last_direction=_row_value(row, "last_direction"),
            created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            updated_at=_parse_datetime(_row_value(row, "updated_at")) or utc_now(),
        )
        db.session.add(thread)
        db.session.flush()
        thread_id_map[int(row["id"])] = thread.id
        counts["inbox_threads"] += 1

    for row in _legacy_rows(legacy_connection, "inbox_messages"):
        legacy_thread_id = _row_value(row, "thread_id")
        mapped_thread_id = thread_id_map.get(int(legacy_thread_id)) if legacy_thread_id is not None else None
        if mapped_thread_id is None:
            continue
        message_sid = (_row_value(row, "message_sid") or "").strip() or None
        if message_sid:
            if message_sid in imported_message_sids:
                raise RuntimeError(f"Duplicate message SID {message_sid!r} encountered during legacy import.")
            existing_message = InboxMessage.query.filter_by(message_sid=message_sid).first()
            if existing_message is not None:
                raise RuntimeError(f"Message SID conflict importing legacy inbox message {message_sid!r}.")
            imported_message_sids.add(message_sid)
        db.session.add(
            InboxMessage(
                organization_id=organization.id,
                thread_id=mapped_thread_id,
                phone=_row_value(row, "phone"),
                direction=_row_value(row, "direction"),
                body=_row_value(row, "body"),
                message_sid=message_sid,
                automation_source=_row_value(row, "automation_source"),
                automation_source_id=_row_value(row, "automation_source_id"),
                matched_keyword=_row_value(row, "matched_keyword"),
                delivery_status=_row_value(row, "delivery_status"),
                delivery_error=_row_value(row, "delivery_error"),
                raw_payload=_row_value(row, "raw_payload"),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            )
        )
        counts["inbox_messages"] += 1

    for row in _legacy_rows(legacy_connection, "keyword_automation_rules"):
        db.session.add(
            KeywordAutomationRule(
                organization_id=organization.id,
                keyword=_row_value(row, "keyword"),
                response_body=_row_value(row, "response_body"),
                is_active=_bool_value(_row_value(row, "is_active"), default=True),
                match_count=int(_row_value(row, "match_count", 0) or 0),
                last_matched_at=_parse_datetime(_row_value(row, "last_matched_at")),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
                updated_at=_parse_datetime(_row_value(row, "updated_at")) or utc_now(),
            )
        )
        counts["keyword_automation_rules"] += 1

    for row in _legacy_rows(legacy_connection, "survey_flows"):
        legacy_linked_event_id = _row_value(row, "linked_event_id")
        survey = SurveyFlow(
            organization_id=organization.id,
            name=_row_value(row, "name"),
            trigger_keyword=_row_value(row, "trigger_keyword"),
            intro_message=_row_value(row, "intro_message"),
            questions_json=_row_value(row, "questions_json", "[]"),
            completion_message=_row_value(row, "completion_message"),
            linked_event_id=(
                event_id_map.get(int(legacy_linked_event_id))
                if legacy_linked_event_id is not None
                else None
            ),
            is_active=_bool_value(_row_value(row, "is_active"), default=True),
            start_count=int(_row_value(row, "start_count", 0) or 0),
            completion_count=int(_row_value(row, "completion_count", 0) or 0),
            created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            updated_at=_parse_datetime(_row_value(row, "updated_at")) or utc_now(),
        )
        db.session.add(survey)
        db.session.flush()
        survey_id_map[int(row["id"])] = survey.id
        counts["survey_flows"] += 1

    for row in _legacy_rows(legacy_connection, "survey_sessions"):
        legacy_survey_id = _row_value(row, "survey_id")
        legacy_thread_id = _row_value(row, "thread_id")
        mapped_survey_id = survey_id_map.get(int(legacy_survey_id)) if legacy_survey_id is not None else None
        mapped_thread_id = thread_id_map.get(int(legacy_thread_id)) if legacy_thread_id is not None else None
        if mapped_survey_id is None or mapped_thread_id is None:
            continue
        session = SurveySession(
            organization_id=organization.id,
            survey_id=mapped_survey_id,
            thread_id=mapped_thread_id,
            phone=_row_value(row, "phone"),
            status=_row_value(row, "status", "active"),
            current_question_index=int(_row_value(row, "current_question_index", 0) or 0),
            started_at=_parse_datetime(_row_value(row, "started_at")) or utc_now(),
            last_activity_at=_parse_datetime(_row_value(row, "last_activity_at")) or utc_now(),
            completed_at=_parse_datetime(_row_value(row, "completed_at")),
        )
        db.session.add(session)
        db.session.flush()
        session_id_map[int(row["id"])] = session.id
        counts["survey_sessions"] += 1

    for row in _legacy_rows(legacy_connection, "survey_responses"):
        legacy_session_id = _row_value(row, "session_id")
        legacy_survey_id = _row_value(row, "survey_id")
        mapped_session_id = session_id_map.get(int(legacy_session_id)) if legacy_session_id is not None else None
        mapped_survey_id = survey_id_map.get(int(legacy_survey_id)) if legacy_survey_id is not None else None
        if mapped_session_id is None or mapped_survey_id is None:
            continue
        db.session.add(
            SurveyResponse(
                organization_id=organization.id,
                session_id=mapped_session_id,
                survey_id=mapped_survey_id,
                phone=_row_value(row, "phone"),
                question_index=int(_row_value(row, "question_index", 0) or 0),
                question_prompt=_row_value(row, "question_prompt"),
                answer=_row_value(row, "answer"),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            )
        )
        counts["survey_responses"] += 1

    for row in _legacy_rows(legacy_connection, "scheduled_messages"):
        legacy_event_id = _row_value(row, "event_id")
        legacy_message_log_id = _row_value(row, "message_log_id")
        db.session.add(
            ScheduledMessage(
                organization_id=organization.id,
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
                scheduled_at=_parse_datetime(_row_value(row, "scheduled_at")) or utc_now(),
                message_body=_row_value(row, "message_body"),
                target=_row_value(row, "target"),
                event_id=event_id_map.get(int(legacy_event_id)) if legacy_event_id is not None else None,
                status=_row_value(row, "status", "pending"),
                test_mode=_bool_value(_row_value(row, "test_mode")),
                attempt_count=int(_row_value(row, "attempt_count", 0) or 0),
                last_attempt_at=_parse_datetime(_row_value(row, "last_attempt_at")),
                next_retry_at=_parse_datetime(_row_value(row, "next_retry_at")),
                processing_started_at=_parse_datetime(_row_value(row, "processing_started_at")),
                sent_at=_parse_datetime(_row_value(row, "sent_at")),
                error_message=_row_value(row, "error_message"),
                message_log_id=(
                    log_id_map.get(int(legacy_message_log_id))
                    if legacy_message_log_id is not None
                    else None
                ),
            )
        )
        counts["scheduled_messages"] += 1

    for row in _legacy_rows(legacy_connection, "auth_events"):
        legacy_user_id = _row_value(row, "user_id")
        legacy_username = (_row_value(row, "username") or "").strip() or None
        mapped_username = (
            _mapped_legacy_username(legacy_username, normalized_remaps)
            if legacy_username
            else None
        )
        db.session.add(
            AuthEvent(
                organization_id=organization.id,
                event_type=_row_value(row, "event_type"),
                outcome=_row_value(row, "outcome", "success"),
                user_id=user_id_map.get(int(legacy_user_id)) if legacy_user_id is not None else None,
                username=mapped_username,
                client_ip=_row_value(row, "client_ip"),
                metadata_json=_row_value(row, "metadata_json"),
                created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
            )
        )
        counts["auth_events"] += 1


def import_legacy_sqlite_snapshot(
    *,
    legacy_db_path: str,
    organization_name: str,
    organization_slug: str,
    logger=None,
) -> dict[str, Any]:
    logger = logger or LOGGER
    source_path = str(Path(legacy_db_path).expanduser().resolve())
    if not Path(source_path).is_file():
        raise RuntimeError(f"Legacy SQLite snapshot not found at {source_path}.")

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        _require_import_ready()
        run_id = _start_import_run(source_path)
        counts = _new_import_counts()
        organization: Organization | None = None

        legacy_connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        legacy_connection.row_factory = sqlite3.Row
        try:
            slug = slugify_organization_name(organization_slug or organization_name)
            if not slug:
                raise RuntimeError("Organization slug is required for legacy import.")

            organization = Organization(name=organization_name, slug=slug, status="active")
            db.session.add(organization)
            db.session.flush()

            db.session.add(
                OrganizationSubscription(
                    organization_id=organization.id,
                    stripe_price_id=current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID"),
                    status="incomplete",
                )
            )
            db.session.add(
                OrganizationMessagingProfile(
                    organization_id=organization.id,
                    status="pending",
                )
            )
            _import_legacy_rows_into_organization(
                legacy_connection,
                organization=organization,
                counts=counts,
            )

            db.session.commit()
            _finish_import_run(
                run_id,
                organization_id=organization.id,
                status="completed",
                counts=counts,
            )
            logger.info(
                "Legacy import completed: source=%s organization_id=%s counts=%s",
                source_path,
                organization.id,
                json.dumps(counts, sort_keys=True),
            )
            return {
                "import_run_id": run_id,
                "organization_id": organization.id,
                "organization_slug": organization.slug,
                "counts": counts,
            }
        except Exception as exc:
            db.session.rollback()
            _finish_import_run(
                run_id,
                organization_id=organization.id if organization is not None and organization.id else None,
                status="failed",
                counts=counts,
                error_message=str(exc)[:2000],
            )
            logger.exception("Legacy import failed for source=%s", source_path)
            raise
        finally:
            legacy_connection.close()


def import_legacy_sqlite_snapshot_into_new_org(
    *,
    legacy_db_path: str,
    organization_name: str,
    organization_slug: str,
    subscription_status: str = "incomplete",
    provider_mode: str = "platform_managed",
    username_remaps: dict[str, str] | None = None,
    logger=None,
) -> dict[str, Any]:
    logger = logger or LOGGER
    source_path = str(Path(legacy_db_path).expanduser().resolve())
    if not Path(source_path).is_file():
        raise RuntimeError(f"Legacy SQLite snapshot not found at {source_path}.")

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        _require_import_audit_ready()
        run_id = _start_import_run(source_path)
        counts = _new_import_counts()
        organization: Organization | None = None

        legacy_connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        legacy_connection.row_factory = sqlite3.Row
        try:
            slug = slugify_organization_name(organization_slug or organization_name)
            if not slug:
                raise RuntimeError("Organization slug is required for legacy import.")
            _assert_import_org_slug_available(slug)

            organization = Organization(name=organization_name, slug=slug, status="active")
            db.session.add(organization)
            db.session.flush()

            db.session.add(
                OrganizationSubscription(
                    organization_id=organization.id,
                    stripe_price_id=current_app.config.get("STRIPE_MONTHLY_PRICE_ID") or current_app.config.get("STRIPE_PRICE_ID"),
                    status=(subscription_status or "incomplete").strip().lower() or "incomplete",
                )
            )
            db.session.add(
                OrganizationMessagingProfile(
                    organization_id=organization.id,
                    provider_mode=(provider_mode or "platform_managed").strip().lower() or "platform_managed",
                    status="pending",
                    provider_status="pending",
                )
            )

            _import_legacy_rows_into_organization(
                legacy_connection,
                organization=organization,
                counts=counts,
                username_remaps=username_remaps,
            )

            db.session.commit()
            _finish_import_run(
                run_id,
                organization_id=organization.id,
                status="completed",
                counts=counts,
            )
            logger.info(
                "Legacy org import completed: source=%s organization_id=%s counts=%s",
                source_path,
                organization.id,
                json.dumps(counts, sort_keys=True),
            )
            return {
                "import_run_id": run_id,
                "organization_id": organization.id,
                "organization_slug": organization.slug,
                "counts": counts,
                "subscription_status": subscription_status,
                "provider_mode": provider_mode,
            }
        except Exception as exc:
            db.session.rollback()
            _finish_import_run(
                run_id,
                organization_id=organization.id if organization is not None and organization.id else None,
                status="failed",
                counts=counts,
                error_message=str(exc)[:2000],
            )
            logger.exception("Legacy org import failed for source=%s", source_path)
            raise
        finally:
            legacy_connection.close()


def sync_legacy_sqlite_snapshot_into_existing_org(
    *,
    legacy_db_path: str,
    organization_slug: str,
    username_remaps: dict[str, str] | None = None,
    since: datetime | None = None,
    dry_run: bool = False,
    logger=None,
) -> dict[str, Any]:
    logger = logger or LOGGER
    source_path = str(Path(legacy_db_path).expanduser().resolve())
    if not Path(source_path).is_file():
        raise RuntimeError(f"Legacy SQLite snapshot not found at {source_path}.")

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        _require_import_audit_ready()
        organization = Organization.query.filter_by(slug=slugify_organization_name(organization_slug)).first()
        if organization is None:
            raise RuntimeError(f"Target organization slug {organization_slug!r} was not found in the SaaS database.")

        baseline_since = since or _latest_completed_import_at(organization.id)
        if baseline_since is None:
            raise RuntimeError(
                "A completed legacy import run was not found for this organization. "
                "Pass an explicit --since timestamp or run the initial legacy import first."
            )

        run_id = _start_import_run(source_path)
        normalized_remaps = _normalized_username_remaps(username_remaps)
        operations = _new_sync_operations()
        source_counts = _new_import_counts()
        target_counts_before = _counts_for_organization(organization.id)

        legacy_connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        legacy_connection.row_factory = sqlite3.Row
        try:
            legacy_users = _legacy_rows(legacy_connection, "users")
            legacy_community_members = _legacy_rows(legacy_connection, "community_members")
            legacy_events = _legacy_rows(legacy_connection, "events")
            legacy_event_registrations = _legacy_rows(legacy_connection, "event_registrations")
            legacy_unsubscribed_contacts = _legacy_rows(legacy_connection, "unsubscribed_contacts")
            legacy_suppressed_contacts = _legacy_rows(legacy_connection, "suppressed_contacts")
            legacy_message_logs = _legacy_rows(legacy_connection, "message_logs")
            legacy_scheduled_messages = _legacy_rows(legacy_connection, "scheduled_messages")
            legacy_inbox_threads = _legacy_rows(legacy_connection, "inbox_threads")
            legacy_inbox_messages = _legacy_rows(legacy_connection, "inbox_messages")
            legacy_keyword_rules = _legacy_rows(legacy_connection, "keyword_automation_rules")
            legacy_survey_flows = _legacy_rows(legacy_connection, "survey_flows")
            legacy_survey_sessions = _legacy_rows(legacy_connection, "survey_sessions")
            legacy_survey_responses = _legacy_rows(legacy_connection, "survey_responses")
            legacy_auth_events = _legacy_rows(legacy_connection, "auth_events")

            source_counts.update(
                {
                    "users": len(legacy_users),
                    "community_members": len(legacy_community_members),
                    "events": len(legacy_events),
                    "event_registrations": len(legacy_event_registrations),
                    "unsubscribed_contacts": len(legacy_unsubscribed_contacts),
                    "suppressed_contacts": len(legacy_suppressed_contacts),
                    "message_logs": len(legacy_message_logs),
                    "scheduled_messages": len(legacy_scheduled_messages),
                    "inbox_threads": len(legacy_inbox_threads),
                    "inbox_messages": len(legacy_inbox_messages),
                    "keyword_automation_rules": len(legacy_keyword_rules),
                    "survey_flows": len(legacy_survey_flows),
                    "survey_sessions": len(legacy_survey_sessions),
                    "survey_responses": len(legacy_survey_responses),
                    "auth_events": len(legacy_auth_events),
                }
            )

            user_memberships = OrganizationMembership.query.filter_by(organization_id=organization.id).all()
            users_by_username = {
                membership.user.username.lower(): membership.user
                for membership in user_memberships
                if membership.user is not None
            }
            memberships_by_user_id = {
                membership.user_id: membership
                for membership in user_memberships
            }
            community_by_phone = {
                member.phone: member
                for member in CommunityMember.query.filter_by(organization_id=organization.id).all()
            }
            unsubscribed_by_phone = {
                contact.phone: contact
                for contact in UnsubscribedContact.query.filter_by(organization_id=organization.id).all()
            }
            suppressed_by_phone = {
                contact.phone: contact
                for contact in SuppressedContact.query.filter_by(organization_id=organization.id).all()
            }
            events_by_key = {
                ((event.title or "").strip(), event.date, event.created_at): event
                for event in Event.query.filter_by(organization_id=organization.id).all()
            }
            threads_by_phone = {
                thread.phone: thread
                for thread in InboxThread.query.filter_by(organization_id=organization.id).all()
            }
            keyword_rules_by_keyword = {
                rule.keyword: rule
                for rule in KeywordAutomationRule.query.filter_by(organization_id=organization.id).all()
            }
            survey_flows_by_keyword = {
                flow.trigger_keyword: flow
                for flow in SurveyFlow.query.filter_by(organization_id=organization.id).all()
            }
            event_registrations_by_key = {
                (registration.event_id, registration.phone): registration
                for registration in EventRegistration.query.filter_by(organization_id=organization.id).all()
            }
            message_logs_by_key = {
                (
                    log.created_at,
                    log.message_body,
                    log.target,
                    log.event_id,
                    log.status,
                    log.total_recipients,
                    log.success_count,
                    log.failure_count,
                    log.details,
                ): log
                for log in MessageLog.query.filter_by(organization_id=organization.id).all()
            }
            inbox_messages_by_sid = {
                message.message_sid: message
                for message in InboxMessage.query.filter_by(organization_id=organization.id).all()
                if message.message_sid
            }
            inbox_messages_by_key = {
                (
                    message.thread_id,
                    message.phone,
                    message.direction,
                    message.body,
                    message.created_at,
                ): message
                for message in InboxMessage.query.filter_by(organization_id=organization.id).all()
                if not message.message_sid
            }
            survey_sessions_by_key = {
                (
                    session.survey_id,
                    session.thread_id,
                    session.phone,
                    session.started_at,
                ): session
                for session in SurveySession.query.filter_by(organization_id=organization.id).all()
            }
            survey_responses_by_key = {
                (
                    response.session_id,
                    response.question_index,
                    response.created_at,
                    response.answer,
                ): response
                for response in SurveyResponse.query.filter_by(organization_id=organization.id).all()
            }
            scheduled_messages_by_key = {
                (
                    scheduled.scheduled_at,
                    scheduled.message_body,
                    scheduled.target,
                    scheduled.event_id,
                    scheduled.message_log_id,
                ): scheduled
                for scheduled in ScheduledMessage.query.filter_by(organization_id=organization.id).all()
            }
            auth_events_by_key = {
                (
                    auth_event.event_type,
                    auth_event.outcome,
                    auth_event.user_id,
                    auth_event.username,
                    auth_event.client_ip,
                    auth_event.created_at,
                ): auth_event
                for auth_event in AuthEvent.query.filter_by(organization_id=organization.id).all()
            }

            user_id_map: dict[int, int] = {}
            event_id_map: dict[int, int] = {}
            log_id_map: dict[int, int] = {}
            thread_id_map: dict[int, int] = {}
            survey_id_map: dict[int, int] = {}
            session_id_map: dict[int, int] = {}

            for row in legacy_users:
                role = (_row_value(row, "role", "admin") or "admin").strip().lower()
                if role not in {"admin", "social_manager"}:
                    role = "admin"
                membership_role = "owner" if role == "admin" else "staff"
                username = _mapped_legacy_username(_row_value(row, "username"), normalized_remaps)
                username_key = username.lower()
                email = (_row_value(row, "email") or "").strip().lower() or None
                existing_user = users_by_username.get(username_key)
                if existing_user is None:
                    conflicting_user = AppUser.query.filter(
                        db.func.lower(AppUser.username) == username_key
                    ).first()
                    if conflicting_user is not None:
                        raise RuntimeError(
                            f"Username conflict syncing legacy user {username!r}; the username is already used outside the target organization."
                        )
                    if email:
                        email_conflict = AppUser.query.filter(db.func.lower(AppUser.email) == email).first()
                        if email_conflict is not None:
                            raise RuntimeError(
                                f"Email conflict syncing legacy user {email!r}; the email is already used by another account."
                            )
                    user = AppUser(
                        username=username,
                        email=email,
                        full_name=_row_value(row, "full_name"),
                        password_hash=_row_value(row, "password_hash"),
                        phone=_row_value(row, "phone"),
                        role=role,
                        is_platform_admin=False,
                        must_change_password=_bool_value(_row_value(row, "must_change_password")),
                        session_nonce=_row_value(row, "session_nonce") or new_session_nonce(),
                        created_at=_parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    )
                    db.session.add(user)
                    db.session.flush()
                    membership = OrganizationMembership(
                        organization_id=organization.id,
                        user_id=user.id,
                        role=membership_role,
                    )
                    db.session.add(membership)
                    users_by_username[username_key] = user
                    memberships_by_user_id[user.id] = membership
                    user_id_map[int(row["id"])] = user.id
                    _record_sync_operation(operations, "users", "created")
                    continue

                if existing_user.is_platform_admin:
                    raise RuntimeError(
                        f"Cannot sync legacy user {username!r} onto a platform admin account."
                    )
                membership = memberships_by_user_id.get(existing_user.id)
                if membership is None:
                    raise RuntimeError(
                        f"User {username!r} exists but is not a member of organization {organization.slug!r}."
                    )
                if email:
                    email_conflict = AppUser.query.filter(
                        db.func.lower(AppUser.email) == email,
                        AppUser.id != existing_user.id,
                    ).first()
                    if email_conflict is not None:
                        raise RuntimeError(
                            f"Email conflict syncing legacy user {email!r}; the email is already used by another account."
                        )
                changed = _apply_model_values(
                    existing_user,
                    {
                        "username": username,
                        "email": email,
                        "full_name": _row_value(row, "full_name"),
                        "password_hash": _row_value(row, "password_hash"),
                        "phone": _row_value(row, "phone"),
                        "role": role,
                        "is_platform_admin": False,
                        "must_change_password": _bool_value(_row_value(row, "must_change_password")),
                        "session_nonce": _row_value(row, "session_nonce") or existing_user.session_nonce or new_session_nonce(),
                        "created_at": _parse_datetime(_row_value(row, "created_at")) or existing_user.created_at or utc_now(),
                    },
                )
                if membership.role != membership_role:
                    membership.role = membership_role
                    changed = True
                user_id_map[int(row["id"])] = existing_user.id
                _record_sync_operation(operations, "users", "updated" if changed else "skipped")

            for row in legacy_community_members:
                phone = _row_value(row, "phone")
                existing_member = community_by_phone.get(phone)
                values = {
                    "organization_id": organization.id,
                    "name": _row_value(row, "name"),
                    "phone": phone,
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                }
                if existing_member is None:
                    existing_member = CommunityMember(**values)
                    db.session.add(existing_member)
                    community_by_phone[phone] = existing_member
                    _record_sync_operation(operations, "community_members", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "community_members",
                        "updated" if _apply_model_values(existing_member, values) else "skipped",
                    )

            for row in legacy_events:
                event_key = (
                    (_row_value(row, "title") or "").strip(),
                    _parse_date(_row_value(row, "date")),
                    _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                )
                existing_event = events_by_key.get(event_key)
                values = {
                    "organization_id": organization.id,
                    "title": event_key[0],
                    "date": event_key[1],
                    "created_at": event_key[2],
                }
                if existing_event is None:
                    existing_event = Event(**values)
                    db.session.add(existing_event)
                    db.session.flush()
                    events_by_key[event_key] = existing_event
                    _record_sync_operation(operations, "events", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "events",
                        "updated" if _apply_model_values(existing_event, values) else "skipped",
                    )
                event_id_map[int(row["id"])] = existing_event.id

            for row in legacy_unsubscribed_contacts:
                phone = _row_value(row, "phone")
                existing_contact = unsubscribed_by_phone.get(phone)
                values = {
                    "organization_id": organization.id,
                    "name": _row_value(row, "name"),
                    "phone": phone,
                    "reason": _row_value(row, "reason"),
                    "source": _row_value(row, "source", "manual"),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                }
                if existing_contact is None:
                    existing_contact = UnsubscribedContact(**values)
                    db.session.add(existing_contact)
                    unsubscribed_by_phone[phone] = existing_contact
                    _record_sync_operation(operations, "unsubscribed_contacts", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "unsubscribed_contacts",
                        "updated" if _apply_model_values(existing_contact, values) else "skipped",
                    )

            for row in legacy_inbox_threads:
                phone = _row_value(row, "phone")
                existing_thread = threads_by_phone.get(phone)
                values = {
                    "organization_id": organization.id,
                    "phone": phone,
                    "contact_name": _row_value(row, "contact_name"),
                    "unread_count": int(_row_value(row, "unread_count", 0) or 0),
                    "last_message_at": _parse_datetime(_row_value(row, "last_message_at")) or utc_now(),
                    "last_message_preview": _row_value(row, "last_message_preview"),
                    "last_direction": _row_value(row, "last_direction"),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    "updated_at": _parse_datetime(_row_value(row, "updated_at")) or utc_now(),
                }
                if existing_thread is None:
                    existing_thread = InboxThread(**values)
                    db.session.add(existing_thread)
                    db.session.flush()
                    threads_by_phone[phone] = existing_thread
                    _record_sync_operation(operations, "inbox_threads", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "inbox_threads",
                        "updated" if _apply_model_values(existing_thread, values) else "skipped",
                    )
                thread_id_map[int(row["id"])] = existing_thread.id

            for row in legacy_keyword_rules:
                keyword = _row_value(row, "keyword")
                existing_rule = keyword_rules_by_keyword.get(keyword)
                values = {
                    "organization_id": organization.id,
                    "keyword": keyword,
                    "response_body": _row_value(row, "response_body"),
                    "is_active": _bool_value(_row_value(row, "is_active"), default=True),
                    "match_count": int(_row_value(row, "match_count", 0) or 0),
                    "last_matched_at": _parse_datetime(_row_value(row, "last_matched_at")),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    "updated_at": _parse_datetime(_row_value(row, "updated_at")) or utc_now(),
                }
                if existing_rule is None:
                    existing_rule = KeywordAutomationRule(**values)
                    db.session.add(existing_rule)
                    keyword_rules_by_keyword[keyword] = existing_rule
                    _record_sync_operation(operations, "keyword_automation_rules", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "keyword_automation_rules",
                        "updated" if _apply_model_values(existing_rule, values) else "skipped",
                    )

            for row in legacy_survey_flows:
                trigger_keyword = _row_value(row, "trigger_keyword")
                existing_flow = survey_flows_by_keyword.get(trigger_keyword)
                legacy_linked_event_id = _row_value(row, "linked_event_id")
                values = {
                    "organization_id": organization.id,
                    "name": _row_value(row, "name"),
                    "trigger_keyword": trigger_keyword,
                    "intro_message": _row_value(row, "intro_message"),
                    "questions_json": _row_value(row, "questions_json", "[]"),
                    "completion_message": _row_value(row, "completion_message"),
                    "linked_event_id": (
                        event_id_map.get(int(legacy_linked_event_id))
                        if legacy_linked_event_id is not None
                        else None
                    ),
                    "is_active": _bool_value(_row_value(row, "is_active"), default=True),
                    "start_count": int(_row_value(row, "start_count", 0) or 0),
                    "completion_count": int(_row_value(row, "completion_count", 0) or 0),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    "updated_at": _parse_datetime(_row_value(row, "updated_at")) or utc_now(),
                }
                if existing_flow is None:
                    existing_flow = SurveyFlow(**values)
                    db.session.add(existing_flow)
                    db.session.flush()
                    survey_flows_by_keyword[trigger_keyword] = existing_flow
                    _record_sync_operation(operations, "survey_flows", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "survey_flows",
                        "updated" if _apply_model_values(existing_flow, values) else "skipped",
                    )
                survey_id_map[int(row["id"])] = existing_flow.id

            for row in legacy_message_logs:
                legacy_event_id = _row_value(row, "event_id")
                message_log_key = (
                    _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    _row_value(row, "message_body"),
                    _row_value(row, "target"),
                    event_id_map.get(int(legacy_event_id)) if legacy_event_id is not None else None,
                    _row_value(row, "status", "sent"),
                    int(_row_value(row, "total_recipients", 0) or 0),
                    int(_row_value(row, "success_count", 0) or 0),
                    int(_row_value(row, "failure_count", 0) or 0),
                    _row_value(row, "details"),
                )
                existing_log = message_logs_by_key.get(message_log_key)
                values = {
                    "organization_id": organization.id,
                    "created_at": message_log_key[0],
                    "message_body": message_log_key[1],
                    "target": message_log_key[2],
                    "event_id": message_log_key[3],
                    "status": message_log_key[4],
                    "total_recipients": message_log_key[5],
                    "success_count": message_log_key[6],
                    "failure_count": message_log_key[7],
                    "details": message_log_key[8],
                }
                if existing_log is None:
                    existing_log = MessageLog(**values)
                    db.session.add(existing_log)
                    db.session.flush()
                    message_logs_by_key[message_log_key] = existing_log
                    _record_sync_operation(operations, "message_logs", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "message_logs",
                        "updated" if _apply_model_values(existing_log, values) else "skipped",
                    )
                log_id_map[int(row["id"])] = existing_log.id

            for row in legacy_suppressed_contacts:
                phone = _row_value(row, "phone")
                legacy_log_id = _row_value(row, "source_message_log_id")
                existing_contact = suppressed_by_phone.get(phone)
                values = {
                    "organization_id": organization.id,
                    "phone": phone,
                    "reason": _row_value(row, "reason"),
                    "category": _row_value(row, "category", "hard_fail"),
                    "source": _row_value(row, "source"),
                    "source_type": _row_value(row, "source_type"),
                    "source_message_log_id": (
                        log_id_map.get(int(legacy_log_id))
                        if legacy_log_id is not None
                        else None
                    ),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    "updated_at": _parse_datetime(_row_value(row, "updated_at")) or utc_now(),
                }
                if existing_contact is None:
                    existing_contact = SuppressedContact(**values)
                    db.session.add(existing_contact)
                    suppressed_by_phone[phone] = existing_contact
                    _record_sync_operation(operations, "suppressed_contacts", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "suppressed_contacts",
                        "updated" if _apply_model_values(existing_contact, values) else "skipped",
                    )

            for row in legacy_event_registrations:
                legacy_event_id = _row_value(row, "event_id")
                mapped_event_id = event_id_map.get(int(legacy_event_id)) if legacy_event_id is not None else None
                if mapped_event_id is None:
                    _record_sync_operation(operations, "event_registrations", "skipped")
                    continue
                key = (mapped_event_id, _row_value(row, "phone"))
                existing_registration = event_registrations_by_key.get(key)
                values = {
                    "organization_id": organization.id,
                    "event_id": mapped_event_id,
                    "name": _row_value(row, "name"),
                    "phone": _row_value(row, "phone"),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                }
                if existing_registration is None:
                    existing_registration = EventRegistration(**values)
                    db.session.add(existing_registration)
                    event_registrations_by_key[key] = existing_registration
                    _record_sync_operation(operations, "event_registrations", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "event_registrations",
                        "updated" if _apply_model_values(existing_registration, values) else "skipped",
                    )

            for row in legacy_inbox_messages:
                legacy_thread_id = _row_value(row, "thread_id")
                mapped_thread_id = thread_id_map.get(int(legacy_thread_id)) if legacy_thread_id is not None else None
                if mapped_thread_id is None:
                    _record_sync_operation(operations, "inbox_messages", "skipped")
                    continue
                message_sid = (_row_value(row, "message_sid") or "").strip() or None
                existing_message = None
                if message_sid:
                    existing_message = inbox_messages_by_sid.get(message_sid)
                    if existing_message is None:
                        existing_message = InboxMessage.query.filter_by(message_sid=message_sid).first()
                        if existing_message is not None and existing_message.organization_id != organization.id:
                            raise RuntimeError(
                                f"Message SID conflict syncing legacy inbox message {message_sid!r}; the SID is already used by another organization."
                            )
                else:
                    message_key = (
                        mapped_thread_id,
                        _row_value(row, "phone"),
                        _row_value(row, "direction"),
                        _row_value(row, "body"),
                        _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    )
                    existing_message = inbox_messages_by_key.get(message_key)
                values = {
                    "organization_id": organization.id,
                    "thread_id": mapped_thread_id,
                    "phone": _row_value(row, "phone"),
                    "direction": _row_value(row, "direction"),
                    "body": _row_value(row, "body"),
                    "message_sid": message_sid,
                    "automation_source": _row_value(row, "automation_source"),
                    "automation_source_id": _row_value(row, "automation_source_id"),
                    "matched_keyword": _row_value(row, "matched_keyword"),
                    "delivery_status": _row_value(row, "delivery_status"),
                    "delivery_error": _row_value(row, "delivery_error"),
                    "raw_payload": _row_value(row, "raw_payload"),
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                }
                if existing_message is None:
                    existing_message = InboxMessage(**values)
                    db.session.add(existing_message)
                    if message_sid:
                        inbox_messages_by_sid[message_sid] = existing_message
                    else:
                        inbox_messages_by_key[message_key] = existing_message
                    _record_sync_operation(operations, "inbox_messages", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "inbox_messages",
                        "updated" if _apply_model_values(existing_message, values) else "skipped",
                    )

            for row in legacy_survey_sessions:
                legacy_survey_id = _row_value(row, "survey_id")
                legacy_thread_id = _row_value(row, "thread_id")
                mapped_survey_id = survey_id_map.get(int(legacy_survey_id)) if legacy_survey_id is not None else None
                mapped_thread_id = thread_id_map.get(int(legacy_thread_id)) if legacy_thread_id is not None else None
                if mapped_survey_id is None or mapped_thread_id is None:
                    _record_sync_operation(operations, "survey_sessions", "skipped")
                    continue
                session_key = (
                    mapped_survey_id,
                    mapped_thread_id,
                    _row_value(row, "phone"),
                    _parse_datetime(_row_value(row, "started_at")) or utc_now(),
                )
                existing_session = survey_sessions_by_key.get(session_key)
                values = {
                    "organization_id": organization.id,
                    "survey_id": mapped_survey_id,
                    "thread_id": mapped_thread_id,
                    "phone": _row_value(row, "phone"),
                    "status": _row_value(row, "status", "active"),
                    "current_question_index": int(_row_value(row, "current_question_index", 0) or 0),
                    "started_at": session_key[3],
                    "last_activity_at": _parse_datetime(_row_value(row, "last_activity_at")) or session_key[3],
                    "completed_at": _parse_datetime(_row_value(row, "completed_at")),
                }
                if existing_session is None:
                    existing_session = SurveySession(**values)
                    db.session.add(existing_session)
                    db.session.flush()
                    survey_sessions_by_key[session_key] = existing_session
                    _record_sync_operation(operations, "survey_sessions", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "survey_sessions",
                        "updated" if _apply_model_values(existing_session, values) else "skipped",
                    )
                session_id_map[int(row["id"])] = existing_session.id

            for row in legacy_survey_responses:
                legacy_session_id = _row_value(row, "session_id")
                legacy_survey_id = _row_value(row, "survey_id")
                mapped_session_id = session_id_map.get(int(legacy_session_id)) if legacy_session_id is not None else None
                mapped_survey_id = survey_id_map.get(int(legacy_survey_id)) if legacy_survey_id is not None else None
                if mapped_session_id is None or mapped_survey_id is None:
                    _record_sync_operation(operations, "survey_responses", "skipped")
                    continue
                response_key = (
                    mapped_session_id,
                    int(_row_value(row, "question_index", 0) or 0),
                    _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    _row_value(row, "answer"),
                )
                existing_response = survey_responses_by_key.get(response_key)
                values = {
                    "organization_id": organization.id,
                    "session_id": mapped_session_id,
                    "survey_id": mapped_survey_id,
                    "phone": _row_value(row, "phone"),
                    "question_index": response_key[1],
                    "question_prompt": _row_value(row, "question_prompt"),
                    "answer": response_key[3],
                    "created_at": response_key[2],
                }
                if existing_response is None:
                    existing_response = SurveyResponse(**values)
                    db.session.add(existing_response)
                    survey_responses_by_key[response_key] = existing_response
                    _record_sync_operation(operations, "survey_responses", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "survey_responses",
                        "updated" if _apply_model_values(existing_response, values) else "skipped",
                    )

            for row in legacy_scheduled_messages:
                legacy_event_id = _row_value(row, "event_id")
                legacy_log_id = _row_value(row, "message_log_id")
                scheduled_key = (
                    _parse_datetime(_row_value(row, "scheduled_at")) or utc_now(),
                    _row_value(row, "message_body"),
                    _row_value(row, "target"),
                    event_id_map.get(int(legacy_event_id)) if legacy_event_id is not None else None,
                    log_id_map.get(int(legacy_log_id)) if legacy_log_id is not None else None,
                )
                existing_scheduled = scheduled_messages_by_key.get(scheduled_key)
                values = {
                    "organization_id": organization.id,
                    "created_at": _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                    "scheduled_at": scheduled_key[0],
                    "message_body": scheduled_key[1],
                    "target": scheduled_key[2],
                    "event_id": scheduled_key[3],
                    "status": _row_value(row, "status", "pending"),
                    "test_mode": _bool_value(_row_value(row, "test_mode")),
                    "attempt_count": int(_row_value(row, "attempt_count", 0) or 0),
                    "last_attempt_at": _parse_datetime(_row_value(row, "last_attempt_at")),
                    "next_retry_at": _parse_datetime(_row_value(row, "next_retry_at")),
                    "processing_started_at": _parse_datetime(_row_value(row, "processing_started_at")),
                    "sent_at": _parse_datetime(_row_value(row, "sent_at")),
                    "error_message": _row_value(row, "error_message"),
                    "message_log_id": scheduled_key[4],
                }
                if existing_scheduled is None:
                    existing_scheduled = ScheduledMessage(**values)
                    db.session.add(existing_scheduled)
                    scheduled_messages_by_key[scheduled_key] = existing_scheduled
                    _record_sync_operation(operations, "scheduled_messages", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "scheduled_messages",
                        "updated" if _apply_model_values(existing_scheduled, values) else "skipped",
                    )

            for row in legacy_auth_events:
                legacy_user_id = _row_value(row, "user_id")
                legacy_username = (_row_value(row, "username") or "").strip() or None
                mapped_username = (
                    _mapped_legacy_username(legacy_username, normalized_remaps)
                    if legacy_username
                    else None
                )
                auth_event_key = (
                    _row_value(row, "event_type"),
                    _row_value(row, "outcome", "success"),
                    user_id_map.get(int(legacy_user_id)) if legacy_user_id is not None else None,
                    mapped_username,
                    _row_value(row, "client_ip"),
                    _parse_datetime(_row_value(row, "created_at")) or utc_now(),
                )
                existing_auth_event = auth_events_by_key.get(auth_event_key)
                values = {
                    "organization_id": organization.id,
                    "event_type": auth_event_key[0],
                    "outcome": auth_event_key[1],
                    "user_id": auth_event_key[2],
                    "username": auth_event_key[3],
                    "client_ip": auth_event_key[4],
                    "metadata_json": _row_value(row, "metadata_json"),
                    "created_at": auth_event_key[5],
                }
                if existing_auth_event is None:
                    existing_auth_event = AuthEvent(**values)
                    db.session.add(existing_auth_event)
                    auth_events_by_key[auth_event_key] = existing_auth_event
                    _record_sync_operation(operations, "auth_events", "created")
                else:
                    _record_sync_operation(
                        operations,
                        "auth_events",
                        "updated" if _apply_model_values(existing_auth_event, values) else "skipped",
                    )

            db.session.flush()
            target_counts_after = _counts_for_organization(organization.id)
            summary = {
                "import_run_id": run_id,
                "organization_id": organization.id,
                "organization_slug": organization.slug,
                "baseline_since": baseline_since.isoformat(),
                "dry_run": dry_run,
                "source_counts": source_counts,
                "target_counts_before": target_counts_before,
                "target_counts_after": target_counts_after,
                "count_mismatches_before": _count_mismatches(source_counts, target_counts_before),
                "count_mismatches_after": _count_mismatches(source_counts, target_counts_after),
                "operations": operations,
            }

            if dry_run:
                db.session.rollback()
                _finish_import_run(
                    run_id,
                    organization_id=organization.id,
                    status="dry_run",
                    counts=target_counts_after,
                )
            else:
                db.session.commit()
                _finish_import_run(
                    run_id,
                    organization_id=organization.id,
                    status="completed",
                    counts=target_counts_after,
                )
            logger.info(
                "Legacy org sync %s: source=%s organization_id=%s mismatches_after=%s",
                "dry-run" if dry_run else "completed",
                source_path,
                organization.id,
                json.dumps(summary["count_mismatches_after"], sort_keys=True),
            )
            return summary
        except Exception as exc:
            db.session.rollback()
            _finish_import_run(
                run_id,
                organization_id=organization.id,
                status="failed",
                counts=target_counts_before,
                error_message=str(exc)[:2000],
            )
            logger.exception("Legacy org sync failed for source=%s organization_id=%s", source_path, organization.id)
            raise
        finally:
            legacy_connection.close()

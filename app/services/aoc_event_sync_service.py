from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import db
from app.models import (
    CommunityMember,
    Event,
    EventRegistration,
    Organization,
    OrganizationEventSyncIntegration,
    ScheduledMessage,
    utc_now,
)
from app.services.provider_secret_service import decrypt_provider_secret, encrypt_provider_secret
from app.tenant import organization_context, without_tenant_scope
from app.utils import normalize_phone, normalize_sms_body, validate_phone


AOC_EXTERNAL_SOURCE = "aoc-wordpress"
AOC_AUTOMATION_SOURCE = "aoc_events"
EVENT_SYNC_AUTOMATION_SOURCE = "event_sync"
WORDPRESS_PROVIDER = "wordpress"
WORDPRESS_EVENTS_MANAGER_SOURCE = "wordpress_events_manager"
WORDPRESS_WPFORMS_SOURCE = "wordpress_wpforms"
WORDPRESS_EVENT_SYNC_SOURCES = {WORDPRESS_EVENTS_MANAGER_SOURCE, WORDPRESS_WPFORMS_SOURCE, AOC_EXTERNAL_SOURCE}
AOC_UNSUBSCRIBE_FOOTER = "\n\nReply STOP to unsubscribe."
ACTIVE_BOOKING_STATUSES = {"active", "approved", "pending", "reserved", "submitted", "completed", "paid"}
CANCELLED_BOOKING_STATUSES = {"cancelled", "canceled", "rejected", "deleted", "inactive"}
PENDING_AUTO_STATUSES = {"pending", "cancelled"}


class AocEventSyncError(Exception):
    """Base error for AOC event sync failures."""


class AocWebhookAuthError(AocEventSyncError):
    """Raised when AOC webhook authentication fails."""


class AocEventSyncPayloadError(AocEventSyncError):
    """Raised when an AOC webhook payload is missing required data."""


def verify_aoc_webhook_signature(
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    secret: str,
    tolerance_seconds: int,
    now_timestamp: int,
) -> None:
    if not secret:
        raise AocWebhookAuthError("AOC webhook secret is not configured.")
    if not timestamp_header:
        raise AocWebhookAuthError("Missing X-AOC-Timestamp header.")
    if not signature_header:
        raise AocWebhookAuthError("Missing X-AOC-Signature header.")

    try:
        timestamp = int(timestamp_header)
    except ValueError as exc:
        raise AocWebhookAuthError("X-AOC-Timestamp must be a Unix timestamp.") from exc

    if abs(now_timestamp - timestamp) > tolerance_seconds:
        raise AocWebhookAuthError("AOC webhook timestamp is outside the allowed tolerance.")

    if not signature_header.startswith("sha256="):
        raise AocWebhookAuthError("X-AOC-Signature must start with sha256=.")

    signed_payload = timestamp_header.encode("utf-8") + b"." + body
    expected_signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    provided_signature = signature_header.removeprefix("sha256=").strip()
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise AocWebhookAuthError("AOC webhook signature did not match.")


def verify_event_sync_webhook_signature(
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    secret: str,
    tolerance_seconds: int,
    now_timestamp: int,
) -> None:
    verify_aoc_webhook_signature(
        body=body,
        timestamp_header=timestamp_header,
        signature_header=signature_header,
        secret=secret,
        tolerance_seconds=tolerance_seconds,
        now_timestamp=now_timestamp,
    )


def parse_aoc_webhook_json(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AocEventSyncPayloadError("AOC webhook payload must be valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise AocEventSyncPayloadError("AOC webhook payload must be a JSON object.")
    return payload


def generate_event_sync_secret() -> str:
    return secrets.token_urlsafe(32)


def event_sync_setup_view(organization: Organization, base_url: str) -> dict[str, Any]:
    integration = OrganizationEventSyncIntegration.query.filter_by(
        organization_id=organization.id,
        provider=WORDPRESS_PROVIDER,
    ).first()
    endpoint = _event_sync_webhook_url(base_url, organization.slug)
    enabled = integration.enabled if integration is not None else False
    has_secret = bool(integration.webhook_secret_encrypted) if integration is not None else False
    return {
        "enabled": enabled,
        "provider": WORDPRESS_PROVIDER,
        "endpoint": endpoint,
        "has_secret": has_secret,
        "last_event_synced_at": integration.last_event_synced_at if integration is not None else None,
        "last_signup_synced_at": integration.last_signup_synced_at if integration is not None else None,
        "last_reconcile_synced_at": integration.last_reconcile_synced_at if integration is not None else None,
        "last_error_at": integration.last_error_at if integration is not None else None,
        "last_error_message": integration.last_error_message if integration is not None else None,
        "setup_block": _wordpress_setup_block(endpoint, has_secret),
    }


def ensure_event_sync_integration(organization_id: int) -> OrganizationEventSyncIntegration:
    integration = OrganizationEventSyncIntegration.query.filter_by(
        organization_id=organization_id,
        provider=WORDPRESS_PROVIDER,
    ).first()
    if integration is not None:
        return integration
    integration = OrganizationEventSyncIntegration(
        organization_id=organization_id,
        provider=WORDPRESS_PROVIDER,
        enabled=False,
    )
    db.session.add(integration)
    db.session.flush()
    return integration


def configure_event_sync_integration(
    organization_id: int,
    enabled: bool,
    actor_user_id: int,
) -> tuple[OrganizationEventSyncIntegration, str | None]:
    integration = ensure_event_sync_integration(organization_id)
    generated_secret = None
    if enabled and not integration.webhook_secret_encrypted:
        generated_secret = generate_event_sync_secret()
        integration.webhook_secret_encrypted = encrypt_provider_secret(generated_secret)
    integration.enabled = enabled
    _record_event_sync_audit(
        organization_id,
        actor_user_id,
        "enabled" if enabled else "disabled",
        {"provider": integration.provider},
    )
    db.session.flush()
    return integration, generated_secret


def rotate_event_sync_secret(
    organization_id: int,
    actor_user_id: int,
) -> tuple[OrganizationEventSyncIntegration, str]:
    integration = ensure_event_sync_integration(organization_id)
    generated_secret = generate_event_sync_secret()
    integration.webhook_secret_encrypted = encrypt_provider_secret(generated_secret)
    integration.enabled = True
    integration.last_error_at = None
    integration.last_error_message = None
    _record_event_sync_audit(
        organization_id,
        actor_user_id,
        "rotated_secret",
        {"provider": integration.provider},
    )
    db.session.flush()
    return integration, generated_secret


def event_sync_integration_for_webhook(
    organization_slug: str,
    provider: str,
) -> OrganizationEventSyncIntegration:
    normalized_provider = (provider or "").strip().lower()
    if normalized_provider != WORDPRESS_PROVIDER:
        raise AocEventSyncPayloadError(f"Unsupported event sync provider {provider!r}.")
    with without_tenant_scope():
        organization = Organization.query.filter_by(slug=organization_slug.strip()).first()
        if organization is None:
            raise AocEventSyncPayloadError(f"Organization slug {organization_slug!r} was not found.")
        integration = OrganizationEventSyncIntegration.query.filter_by(
            organization_id=organization.id,
            provider=WORDPRESS_PROVIDER,
        ).first()
    if integration is None or not integration.enabled:
        raise AocWebhookAuthError("Event sync is not enabled for this organization.")
    if not integration.webhook_secret_encrypted:
        raise AocWebhookAuthError("Event sync webhook secret is not configured.")
    return integration


def decrypted_event_sync_secret(integration: OrganizationEventSyncIntegration) -> str:
    secret = decrypt_provider_secret(integration.webhook_secret_encrypted)
    if not secret:
        raise AocWebhookAuthError("Event sync webhook secret is not configured.")
    return secret


def process_event_sync_payload(
    payload: dict[str, Any],
    integration: OrganizationEventSyncIntegration,
    received_at: datetime,
) -> dict[str, Any]:
    try:
        summary = _process_event_sync_payload_for_organization(
            payload=payload,
            organization_id=integration.organization_id,
            received_at=received_at,
        )
    except AocEventSyncError as exc:
        record_event_sync_error(integration, str(exc), received_at)
        raise

    action = summary["action"]
    if action in {"event_upsert", "event_deleted"}:
        integration.last_event_synced_at = _as_utc_naive(received_at)
    elif action in {"booking_upsert", "booking_deleted"}:
        integration.last_signup_synced_at = _as_utc_naive(received_at)
    elif action == "reconcile_complete":
        integration.last_reconcile_synced_at = _as_utc_naive(received_at)
    integration.last_error_at = None
    integration.last_error_message = None
    db.session.flush()
    return summary


def record_event_sync_error(
    integration: OrganizationEventSyncIntegration,
    message: str,
    received_at: datetime,
) -> None:
    integration.last_error_at = _as_utc_naive(received_at)
    integration.last_error_message = message[:1000]
    db.session.flush()


def process_aoc_event_sync_payload(
    payload: dict[str, Any],
    organization_slug: str,
    received_at: datetime,
) -> dict[str, Any]:
    organization = _organization_for_slug(organization_slug)
    return _process_event_sync_payload_for_organization(
        payload=payload,
        organization_id=organization.id,
        received_at=received_at,
    )


def _process_event_sync_payload_for_organization(
    payload: dict[str, Any],
    organization_id: int,
    received_at: datetime,
) -> dict[str, Any]:
    action = _required_text(payload, "action")
    source = _optional_text(payload, "source") or AOC_EXTERNAL_SOURCE
    if source not in WORDPRESS_EVENT_SYNC_SOURCES:
        raise AocEventSyncPayloadError(f"Unsupported event sync source {source!r}.")

    organization = _organization_for_id(organization_id)
    received_at_utc = _as_utc_naive(received_at)
    summary: dict[str, Any] = {
        "organization_id": organization.id,
        "organization_slug": organization.slug,
        "action": action,
        "event_id": None,
        "registration_id": None,
        "community_member_id": None,
        "scheduled_created": 0,
        "scheduled_updated": 0,
        "scheduled_cancelled": 0,
        "warnings": [],
    }

    with organization_context(organization.id):
        if action == "reconcile_complete":
            summary["reconciled"] = True
            db.session.flush()
            return summary

        if action in {"event_upsert", "event_deleted"}:
            event_payload = _required_dict(payload, "event")
            event = _upsert_event(event_payload, source, received_at_utc)
            summary["event_id"] = event.id
            if _event_should_schedule(event, received_at_utc) and action == "event_upsert":
                _sync_event_reminders(event, received_at_utc, summary)
            else:
                summary["scheduled_cancelled"] += _cancel_pending_event_reminders(event)
            db.session.flush()
            return summary

        if action in {"booking_upsert", "booking_deleted"}:
            event_payload = _required_dict(payload, "event")
            booking_payload = _booking_payload_for_source(_required_dict(payload, "booking"), source)
            event = _upsert_event(event_payload, _event_source_for_booking_source(source), received_at_utc)
            summary["event_id"] = event.id
            if _event_should_schedule(event, received_at_utc):
                _sync_event_reminders(event, received_at_utc, summary)
            else:
                summary["scheduled_cancelled"] += _cancel_pending_event_reminders(event)
            if action == "booking_deleted" or _booking_is_cancelled(booking_payload):
                summary["registration_id"] = _delete_event_registration_for_booking(
                    event,
                    booking_payload,
                    source,
                )
            else:
                registration, community_member_id, warning = _upsert_booking_registration(
                    event,
                    booking_payload,
                    source,
                    received_at_utc,
                )
                if registration is not None:
                    summary["registration_id"] = registration.id
                if community_member_id is not None:
                    summary["community_member_id"] = community_member_id
                if warning:
                    summary["warnings"].append(warning)
            db.session.flush()
            return summary

    raise AocEventSyncPayloadError(f"Unsupported event sync action {action!r}.")


def _organization_for_slug(organization_slug: str) -> Organization:
    normalized_slug = organization_slug.strip()
    if not normalized_slug:
        raise AocEventSyncPayloadError("AOC organization slug is not configured.")
    with without_tenant_scope():
        organization = Organization.query.filter_by(slug=normalized_slug).first()
    if organization is None:
        raise AocEventSyncPayloadError(f"Organization slug {normalized_slug!r} was not found.")
    return organization


def _organization_for_id(organization_id: int) -> Organization:
    with without_tenant_scope():
        organization = db.session.get(Organization, organization_id)
    if organization is None:
        raise AocEventSyncPayloadError(f"Organization id {organization_id!r} was not found.")
    return organization


def _upsert_event(payload: dict[str, Any], source: str, received_at: datetime) -> Event:
    external_event_id = _required_text(payload, "event_id")
    title = _required_text(payload, "title")
    start_at = _required_datetime(payload, "start_at")
    timezone_name = _required_text(payload, "timezone")
    event_timezone = _timezone_for_name(timezone_name)
    event_date = _local_date(start_at, event_timezone)

    event = Event.query.filter_by(external_source=source, external_event_id=external_event_id).first()
    if event is None:
        event = Event(title=title, date=event_date, external_source=source, external_event_id=external_event_id)
        db.session.add(event)

    location_payload = _optional_dict(payload, "location")
    event.title = title
    event.date = event_date
    event.external_source = source
    event.external_event_id = external_event_id
    event.external_post_id = _optional_text(payload, "post_id")
    event.external_slug = _optional_text(payload, "slug")
    event.external_url = _optional_text(payload, "permalink")
    event.external_status = _optional_text(payload, "status")
    event.external_start_at = _to_utc_naive(start_at)
    event.external_end_at = _optional_utc_naive_datetime(payload, "end_at")
    event.external_timezone = timezone_name
    event.external_modified_at = _optional_utc_naive_datetime(payload, "modified_at")
    event.location_name = _optional_text(location_payload, "name")
    event.location_address = _optional_text(location_payload, "address")
    event.location_town = _optional_text(location_payload, "town")
    event.location_state = _optional_text(location_payload, "state")
    event.location_postcode = _optional_text(location_payload, "postcode")
    event.location_country = _optional_text(location_payload, "country")
    event.rsvp_enabled = _optional_bool(payload, "rsvp_enabled")
    event.capacity = _optional_int(payload, "capacity")
    event.synced_at = received_at
    db.session.flush()
    return event


def _upsert_booking_registration(
    event: Event,
    payload: dict[str, Any],
    source: str,
    received_at: datetime,
) -> tuple[EventRegistration | None, int | None, str | None]:
    external_booking_id = _external_booking_id(payload)
    if _optional_bool(payload, "sms_consent") is not True:
        _delete_event_registration_for_booking(event, payload, source)
        return None, None, f"Booking {external_booking_id} did not include affirmative SMS consent."

    raw_phone = _optional_text(payload, "phone")
    if not raw_phone:
        return None, None, f"Booking {external_booking_id} did not include a phone number."

    phone = normalize_phone(raw_phone)
    if not validate_phone(phone):
        return None, None, f"Booking {external_booking_id} included invalid phone {raw_phone!r}."

    name = _optional_text(payload, "name")
    registration = EventRegistration.query.filter_by(
        external_source=source,
        external_booking_id=external_booking_id,
    ).first()
    if registration is None:
        registration = EventRegistration.query.filter_by(event_id=event.id, phone=phone).first()
    if registration is None:
        registration = EventRegistration(
            event_id=event.id,
            name=name,
            phone=phone,
            external_source=source,
            external_booking_id=external_booking_id,
        )
        db.session.add(registration)

    registration.event_id = event.id
    registration.name = name or registration.name
    registration.phone = phone
    registration.external_source = source
    registration.external_booking_id = external_booking_id
    registration.external_person_id = _optional_text(payload, "person_id")
    registration.external_booking_status = _optional_text(payload, "status")
    registration.booking_spaces = _optional_int(payload, "spaces")
    selections = _optional_selections(payload, "selections")
    registration.selections_json = (
        json.dumps(selections, ensure_ascii=False, sort_keys=True)
        if selections
        else None
    )
    registration.booking_comment = _optional_text(payload, "comment")
    registration.external_updated_at = _optional_utc_naive_datetime(payload, "updated_at")
    registration.synced_at = received_at

    community_member = CommunityMember.query.filter_by(phone=phone).first()
    if community_member is None:
        community_member = CommunityMember(name=name, phone=phone)
        db.session.add(community_member)
    elif not community_member.name and name:
        community_member.name = name

    db.session.flush()
    return registration, community_member.id, None


def _delete_event_registration_for_booking(
    event: Event,
    payload: dict[str, Any],
    source: str,
) -> int | None:
    external_booking_id = _external_booking_id(payload)
    registration = EventRegistration.query.filter_by(
        external_source=source,
        external_booking_id=external_booking_id,
    ).first()
    if registration is None:
        raw_phone = _optional_text(payload, "phone")
        phone = normalize_phone(raw_phone) if raw_phone else ""
        if phone:
            registration = EventRegistration.query.filter_by(event_id=event.id, phone=phone).first()
    if registration is None:
        return None
    registration_id = registration.id
    db.session.delete(registration)
    db.session.flush()
    return registration_id


def _sync_event_reminders(event: Event, received_at: datetime, summary: dict[str, Any]) -> None:
    reminder_specs = (
        ("invite", "community", _invite_scheduled_at(event, received_at), _invite_message(event)),
        ("seven_day", "community", _days_before_scheduled_at(event, 7), _before_event_message(event, "one week")),
        ("one_day", "community", _days_before_scheduled_at(event, 1), _before_event_message(event, "tomorrow")),
        ("day_of", "event", _day_of_scheduled_at(event), _day_of_message(event)),
    )

    for automation_kind, target, scheduled_at, message_body in reminder_specs:
        automation_key = _automation_key(event, automation_kind)
        automation_source = _automation_source_for_event(event)
        scheduled = ScheduledMessage.query.filter_by(
            automation_source=automation_source,
            automation_key=automation_key,
        ).first()
        if scheduled_at is None or scheduled_at <= received_at:
            if scheduled is not None and scheduled.status in PENDING_AUTO_STATUSES:
                scheduled.status = "cancelled"
                scheduled.error_message = "AOC event reminder no longer has a future scheduled time."
                summary["scheduled_cancelled"] += 1
            continue
        if scheduled is not None and scheduled.status not in PENDING_AUTO_STATUSES:
            continue
        if scheduled is None:
            scheduled = ScheduledMessage(
                scheduled_at=scheduled_at,
                message_body=message_body,
                target=target,
                event_id=event.id if target == "event" else None,
                automation_source=automation_source,
                automation_key=automation_key,
                automation_kind=automation_kind,
            )
            db.session.add(scheduled)
            summary["scheduled_created"] += 1
        else:
            scheduled.status = "pending"
            scheduled.scheduled_at = scheduled_at
            scheduled.message_body = message_body
            scheduled.target = target
            scheduled.event_id = event.id if target == "event" else None
            scheduled.error_message = None
            scheduled.next_retry_at = None
            scheduled.processing_started_at = None
            scheduled.sent_at = None
            summary["scheduled_updated"] += 1


def _cancel_pending_event_reminders(event: Event) -> int:
    automation_keys = [_automation_key(event, automation_kind) for automation_kind in ("invite", "seven_day", "one_day", "day_of")]
    scheduled_messages = ScheduledMessage.query.filter(
        ScheduledMessage.automation_source.in_([AOC_AUTOMATION_SOURCE, EVENT_SYNC_AUTOMATION_SOURCE]),
        ScheduledMessage.status == "pending",
        ScheduledMessage.automation_key.in_(automation_keys),
    ).all()
    cancelled = 0
    for scheduled in scheduled_messages:
        scheduled.status = "cancelled"
        scheduled.error_message = "AOC event is no longer published and future-dated."
        cancelled += 1
    return cancelled


def _automation_key(event: Event, automation_kind: str) -> str:
    external_event_id = event.external_event_id or str(event.id)
    external_source = event.external_source or AOC_EXTERNAL_SOURCE
    return f"{external_source}:{external_event_id}:{event.id}:{automation_kind}"


def _automation_source_for_event(event: Event) -> str:
    return AOC_AUTOMATION_SOURCE if event.external_source == AOC_EXTERNAL_SOURCE else EVENT_SYNC_AUTOMATION_SOURCE


def _event_should_schedule(event: Event, received_at: datetime) -> bool:
    if event.external_status != "publish":
        return False
    if event.external_start_at is None:
        return False
    return event.external_start_at > received_at


def _booking_is_cancelled(payload: dict[str, Any]) -> bool:
    status = (_optional_text(payload, "status") or "").lower()
    if status in CANCELLED_BOOKING_STATUSES:
        return True
    if status in ACTIVE_BOOKING_STATUSES:
        return False
    active = _optional_bool(payload, "active")
    return active is False


def _booking_payload_for_source(payload: dict[str, Any], source: str) -> dict[str, Any]:
    copied = dict(payload)
    if _optional_text(copied, "provider") or _optional_text(copied, "source_type"):
        return copied
    if source == WORDPRESS_WPFORMS_SOURCE:
        copied["provider"] = "wpforms"
    else:
        copied["provider"] = "events_manager"
    return copied


def _event_source_for_booking_source(source: str) -> str:
    if source == WORDPRESS_WPFORMS_SOURCE:
        return WORDPRESS_EVENTS_MANAGER_SOURCE
    return source


def _invite_scheduled_at(event: Event, received_at: datetime) -> datetime | None:
    if event.external_start_at is None:
        return None
    if event.external_start_at <= received_at + timedelta(minutes=15):
        return None
    return received_at + timedelta(minutes=5)


def _days_before_scheduled_at(event: Event, days_before: int) -> datetime | None:
    local_start = _event_local_start(event)
    reminder_date = local_start.date() - timedelta(days=days_before)
    reminder_local = datetime.combine(reminder_date, time(hour=9), tzinfo=local_start.tzinfo)
    return _to_utc_naive(reminder_local)


def _day_of_scheduled_at(event: Event) -> datetime | None:
    local_start = _event_local_start(event)
    reminder_local = datetime.combine(local_start.date(), time(hour=9), tzinfo=local_start.tzinfo)
    if reminder_local >= local_start:
        reminder_local = local_start - timedelta(hours=2)
    return _to_utc_naive(reminder_local)


def _invite_message(event: Event) -> str:
    return normalize_sms_body(
        f"Hi {{first_name}}, {event.title} is coming up on {_event_date_label(event)}. "
        f"Sign up here: {event.external_url}{AOC_UNSUBSCRIBE_FOOTER}"
    )


def _before_event_message(event: Event, timing_label: str) -> str:
    return normalize_sms_body(
        f"Reminder: {event.title} is {timing_label}. "
        f"Please sign up if you have not yet: {event.external_url}{AOC_UNSUBSCRIBE_FOOTER}"
    )


def _day_of_message(event: Event) -> str:
    location_sentence = ""
    if event.sms_location_note:
        location_sentence = f" Meet at {event.sms_location_note.strip()}"
    return normalize_sms_body(
        f"Armenians of Colorado: Today, {event.title} starts at {_event_time_label(event)}."
        f"{location_sentence} "
        f"Details: {event.external_url}{AOC_UNSUBSCRIBE_FOOTER}"
    )


def _event_date_label(event: Event) -> str:
    local_start = _event_local_start(event)
    return f"{local_start.strftime('%b')} {local_start.day}, {local_start.year}"


def _event_time_label(event: Event) -> str:
    local_start = _event_local_start(event)
    hour = local_start.hour
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{local_start.minute:02d} {suffix}"


def _event_local_start(event: Event) -> datetime:
    if event.external_start_at is None:
        raise AocEventSyncPayloadError("AOC event is missing external_start_at.")
    timezone_name = event.external_timezone or ""
    event_timezone = _timezone_for_name(timezone_name)
    return event.external_start_at.replace(tzinfo=timezone.utc).astimezone(event_timezone)


def _local_date(value: datetime, event_timezone: ZoneInfo) -> date:
    return value.astimezone(event_timezone).date()


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AocEventSyncPayloadError("Datetime values must include timezone information.")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _timezone_for_name(timezone_name: str) -> ZoneInfo:
    if not timezone_name:
        raise AocEventSyncPayloadError("AOC event timezone is required.")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise AocEventSyncPayloadError(f"AOC event timezone {timezone_name!r} is invalid.") from exc


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = _optional_text(payload, key)
    if not value:
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} is required.")
    return value


def _optional_text(payload: dict[str, Any] | None, key: str) -> str | None:
    if payload is None or key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, (str, int, float)):
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be scalar text.")
    normalized = str(value).strip()
    return normalized or None


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = _optional_dict(payload, key)
    if value is None:
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be an object.")
    return value


def _optional_dict(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be an object.")
    return value


def _required_datetime(payload: dict[str, Any], key: str) -> datetime:
    value = _optional_datetime(payload, key)
    if value is None:
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} is required.")
    return value


def _optional_datetime(payload: dict[str, Any], key: str) -> datetime | None:
    value = _optional_text(payload, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be an ISO-8601 datetime.") from exc
    if parsed.tzinfo is None:
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must include timezone information.")
    return parsed


def _optional_utc_naive_datetime(payload: dict[str, Any], key: str) -> datetime | None:
    parsed = _optional_datetime(payload, key)
    if parsed is None:
        return None
    return _to_utc_naive(parsed)


def _external_booking_id(payload: dict[str, Any]) -> str:
    booking_id = _required_text(payload, "booking_id")
    provider = _optional_text(payload, "provider") or _optional_text(payload, "source_type") or "events_manager"
    return f"{provider}:{booking_id}"


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in {0, 1}:
            return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be boolean.")


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload or payload[key] is None or payload[key] == "":
        return None
    try:
        return int(payload[key])
    except (TypeError, ValueError) as exc:
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be an integer.") from exc


def _optional_selections(payload: dict[str, Any], key: str) -> list[dict[str, str | int]]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise AocEventSyncPayloadError(f"AOC payload field {key!r} must be an array.")

    selections: list[dict[str, str | int]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise AocEventSyncPayloadError(
                f"AOC payload field {key!r} item {index} must be an object."
            )
        label = _required_text(item, "label")
        quantity = _optional_int(item, "quantity")
        if quantity is None or quantity <= 0:
            raise AocEventSyncPayloadError(
                f"AOC payload field {key!r} item {index} quantity must be positive."
            )
        selections.append({"label": label[:200], "quantity": quantity})
    return selections


def utc_now_for_aoc_sync() -> datetime:
    return utc_now().replace(tzinfo=None)


def _event_sync_webhook_url(base_url: str, organization_slug: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    return f"{normalized_base_url}/webhooks/event-sync/{organization_slug}/wordpress"


def _wordpress_setup_block(endpoint: str, has_secret: bool) -> str:
    secret_value = "PASTE_GENERATED_SECRET_HERE" if has_secret else "GENERATE_AND_PASTE_SECRET_HERE"
    return "\n".join(
        [
            "define('TWINEVIA_EVENT_SYNC_ENABLED', true);",
            f"define('TWINEVIA_EVENT_SYNC_WEBHOOK_URL', '{endpoint}');",
            f"define('TWINEVIA_EVENT_SYNC_WEBHOOK_SECRET', '{secret_value}');",
        ]
    )


def _record_event_sync_audit(
    organization_id: int,
    actor_user_id: int,
    action: str,
    metadata: dict[str, Any],
) -> None:
    from app.models import OrganizationSettingsAuditLog

    entry = OrganizationSettingsAuditLog(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        category="event_sync",
        action=action,
        metadata_json=json.dumps(metadata, sort_keys=True),
    )
    db.session.add(entry)

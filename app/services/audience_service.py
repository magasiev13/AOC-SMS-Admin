"""Current audience projections without deleting attendance or consent history."""
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy import case, exists, literal, select

from app import db
from app.models import CommunityMember, Event, EventRegistration, RecipientPermissionEvidence, SuppressedContact, UnsubscribedContact, utc_now
from app.tenant import get_current_organization_id, saas_mode_enabled
from app.utils import as_utc_datetime, phone_digits_sql


def _query(model, organization_id=None):
    query = model.query
    if saas_mode_enabled():
        organization_id = organization_id or get_current_organization_id()
        # No implicit platform-wide audience when a caller lacks tenant context.
        if not organization_id:
            return query.filter(db.false())
        return query.filter(model.organization_id == organization_id)
    return query


def _phone_digits(column):
    digits = phone_digits_sql(column)
    return case((db.func.length(digits) == 10, literal('1') + digits), else_=digits)


def contactable_predicate(model):
    """Transport restrictions only; normal purpose permission still gates sends."""
    clauses = []
    for restriction in (UnsubscribedContact, SuppressedContact):
        blocked = exists(select(restriction.id).where(
            restriction.organization_id.is_not_distinct_from(model.organization_id),
            _phone_digits(restriction.phone) == _phone_digits(model.phone),
        ).correlate(model))
        clauses.append(~blocked)
    if saas_mode_enabled():
        latest_transport = select(RecipientPermissionEvidence.decision).where(
            RecipientPermissionEvidence.organization_id == model.organization_id,
            _phone_digits(RecipientPermissionEvidence.phone) == _phone_digits(model.phone),
            RecipientPermissionEvidence.purpose == 'all',
        ).order_by(RecipientPermissionEvidence.occurred_at.desc(), RecipientPermissionEvidence.id.desc()).limit(1).correlate(model).scalar_subquery()
        clauses.append(db.func.coalesce(latest_transport, 'unknown') != 'denied')
    return db.and_(*clauses)


def current_event_predicate(*, now=None):
    """Known end times expire precisely; date-only events last their local day."""
    now = as_utc_datetime(now or utc_now())
    try:
        zone = ZoneInfo(current_app.config.get('APP_TIMEZONE', 'UTC'))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = timezone.utc
    today = now.astimezone(zone).date()
    # Source timestamps and manual dates are retained; no midnight cleanup write
    # is needed for an event to leave the current audience automatically.
    date_current = db.or_(Event.date.is_(None), Event.date >= today)
    return db.and_(
        db.or_(Event.external_status.is_(None), ~db.func.lower(Event.external_status).in_(
            ('trash', 'deleted', 'cancelled', 'canceled', 'draft', 'private'),
        )),
        db.or_(
            db.and_(Event.external_end_at.isnot(None), Event.external_end_at > now.replace(tzinfo=None)),
            db.and_(Event.external_end_at.is_(None), date_current),
        ),
    )


def events_query(*, current_only=True, organization_id=None, now=None):
    query = _query(Event, organization_id)
    return query.filter(current_event_predicate(now=now)) if current_only else query


def event_is_current(event, *, now=None):
    return events_query(organization_id=event.organization_id, now=now).filter(Event.id == event.id).first() is not None


def community_query(*, contactable_only=True, organization_id=None):
    query = _query(CommunityMember, organization_id)
    return query.filter(contactable_predicate(CommunityMember)) if contactable_only else query


def event_registrations_query(*, current_only=True, contactable_only=True, organization_id=None, now=None):
    query = _query(EventRegistration, organization_id)
    if current_only:
        query = query.join(Event, db.and_(
            Event.id == EventRegistration.event_id,
            Event.organization_id.is_not_distinct_from(EventRegistration.organization_id),
        )).filter(current_event_predicate(now=now))
    if contactable_only:
        query = query.filter(contactable_predicate(EventRegistration))
    return query


def current_audience_counts(*, organization_id=None, now=None):
    community = community_query(organization_id=organization_id)
    registrations = event_registrations_query(organization_id=organization_id, now=now)
    phones = community.with_entities(CommunityMember.phone).union(
        registrations.with_entities(EventRegistration.phone),
    ).subquery()
    return {
        'community': community.count(),
        'event_registrations': registrations.count(),
        'unique_contacts': int(db.session.query(db.func.count()).select_from(phones).scalar() or 0),
    }


def pre_event_reminder_block_reason(scheduled, *, now=None):
    """Only proven synced pre-event reminders expire; manual feedback stays valid."""
    from app.services.aoc_event_sync_service import (
        AOC_AUTOMATION_SOURCE, EVENT_SYNC_AUTOMATION_SOURCE, EVENT_REMINDER_KINDS,
    )
    if scheduled.test_mode or scheduled.automation_source not in {AOC_AUTOMATION_SOURCE, EVENT_SYNC_AUTOMATION_SOURCE}:
        return None
    if scheduled.automation_kind not in EVENT_REMINDER_KINDS:
        return None
    # The retained automation key encodes source, external ID, local ID, kind.
    # Validate the exact reconstructed key before interpreting any association.
    parts = str(scheduled.automation_key or '').rsplit(':', 2)
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] != scheduled.automation_kind:
        return None
    event = _query(Event, scheduled.organization_id).filter(Event.id == int(parts[1])).first()
    if event is None:
        return 'The automatic event reminder has no retained event; it was not submitted.'
    from app.services.aoc_event_sync_service import _automation_key
    if _automation_key(event, scheduled.automation_kind) != scheduled.automation_key:
        return 'The automatic event reminder identity no longer matches its event; it was not submitted.'
    if not event_is_current(event, now=now):
        return 'The event has ended or is no longer current; its automatic reminder was not submitted.'
    return None

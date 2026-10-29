"""Project independent registration transactions into a deduplicated SMS list.

The booking ledger owns source identity and seats. EventRegistration owns the
current destination projection and retains its existing row ID where possible.
Only the source inventory review can establish historical roster coverage.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from app import db
from app.models import Event, EventBooking, EventRegistration, utc_now
from app.tenant import get_current_organization_id, tenant_scope_is_active
from app.utils import normalize_phone, validate_phone

ACTIVE_STATUSES = {'active', 'approved', 'pending', 'reserved', 'submitted', 'completed', 'paid', 'confirmed', 'legacy'}
CANCELLED_STATUSES = {'cancelled', 'canceled', 'rejected', 'deleted', 'inactive'}
LOCAL_SOURCES = {'manual', 'csv_import', 'registration_survey', 'legacy', 'legacy_import'}


@dataclass(frozen=True, repr=False)
class BookingPermissionTransition:
    """Explicit transaction-local effect; source payloads are never display data."""
    booking_id: int
    organization_id: int | None
    event_id: int
    payload_json: str
    old_phone: str | None
    received_at: datetime


def _permission_transition(event, booking, payload, old_phone, received_at):
    return BookingPermissionTransition(booking.id, event.organization_id, event.id,
        json.dumps(payload, sort_keys=True, default=str), old_phone, _naive(received_at))


def _naive(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _lock_event(event):
    if tenant_scope_is_active() and get_current_organization_id() != event.organization_id:
        raise ValueError('Booking belongs to another organization.')
    locked = Event.query.filter_by(id=event.id, organization_id=event.organization_id).with_for_update().first()
    if locked is None:
        raise ValueError('Booking event was not found.')


def _identity(value, name, maximum):
    value = str(value or '').strip()
    if not value or len(value) > maximum:
        raise ValueError(f'{name} must contain 1–{maximum} characters.')
    return value


def _revision(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError('Source revision must be a nonnegative integer.')
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Source revision must be a nonnegative integer.') from exc
    if parsed < 0 or parsed > 9223372036854775807 or str(parsed) != str(value).strip():
        raise ValueError('Source revision must be a nonnegative integer.')
    return parsed


def _is_newer(booking, revision, updated_at, *, reactivating=False):
    if reactivating:
        inventory_fences = [_naive(entry.get('source_updated_at')) for entry in booking.cancellation_history
            if entry.get('reason') in {'inventory_missing', 'complete_inventory_missing'} and entry.get('source_updated_at')]
        if inventory_fences and (updated_at is None or updated_at <= max(inventory_fences)):
            # A per-record numeric revision does not prove it happened after an
            # inventory's absence watermark. Require later source-time evidence.
            return False
    if booking.source_revision is not None and revision is None:
        # Queued callbacks from an older integration cannot downgrade a numeric
        # revision, even when their replay-time timestamp happens to be later.
        return False
    if booking.legacy_evidence:
        # Recovery of a retained source identity may use the same revision as
        # its old projection, but an older replay must not rewrite that evidence.
        return not booking.source_updated_at or (updated_at is not None and updated_at >= _naive(booking.source_updated_at))
    if revision is not None and booking.source_revision is not None:
        return revision > booking.source_revision
    if updated_at is not None and booking.source_updated_at is not None:
        return updated_at > _naive(booking.source_updated_at)
    # A versioned identity must not be overwritten by an unversioned webhook.
    if booking.source_revision is not None or booking.source_updated_at is not None:
        return False
    return not reactivating


def ensure_legacy_bookings(event):
    """Retain old projection rows as unverified evidence before first mutation."""
    linked = {value for (value,) in EventBooking.query.with_entities(EventBooking.projection_registration_id).filter_by(
        organization_id=event.organization_id, event_id=event.id,
    ).filter(EventBooking.projection_registration_id.isnot(None)).all()}
    for registration in EventRegistration.query.filter_by(organization_id=event.organization_id, event_id=event.id).all():
        if registration.id in linked:
            continue
        source = registration.external_source or 'legacy'
        key = registration.external_booking_id or f'legacy:{registration.id}'
        existing = EventBooking.query.filter_by(organization_id=event.organization_id, source=source,
            source_scope='', external_booking_id=key).first()
        if existing is not None:
            continue
        db.session.add(EventBooking(
            organization_id=event.organization_id, event_id=event.id, source=source, source_scope='',
            external_booking_id=key, status=registration.external_booking_status or 'legacy',
            is_active=True, party_size=registration.capacity_spaces, name=registration.name,
            phone=registration.phone, contact_state='valid', selections_json=registration.selections_json,
            comment=registration.booking_comment, external_person_id=registration.external_person_id,
            source_updated_at=registration.external_updated_at, last_received_at=registration.synced_at or utc_now(),
            projection_registration_id=registration.id, legacy_evidence=True,
            created_at=registration.created_at or utc_now(),
        ))
    db.session.flush()


def upsert_booking(event, payload, source, received_at, source_scope='', source_revision=None, deleted=False,
                   *, project=True, seed_legacy=True, sync_permissions=True):
    """Apply one source revision, retaining cancellation tombstones and seats."""
    _lock_event(event)
    if seed_legacy:
        ensure_legacy_bookings(event)
    source = _identity(source, 'Source', 80)
    source_scope = str(source_scope or '').strip()
    if len(source_scope) > 160:
        raise ValueError('Source scope is too long.')
    if source in {'aoc-wordpress', 'wordpress_events_manager', 'wordpress_wpforms'}:
        from app.services.aoc_event_sync_service import (
            _booking_payload_for_source, _external_booking_id, _optional_bool,
            _optional_int, _optional_selections, _optional_utc_naive_datetime,
        )
        payload = _booking_payload_for_source(payload, source)
        for field in ('active', 'sms_consent', 'community_sms_consent'):
            if field in payload:
                payload[field] = _optional_bool(payload, field)
        if 'spaces' in payload:
            if isinstance(payload['spaces'], (float, bool)):
                raise ValueError('Booking places must be a nonnegative integer.')
            payload['spaces'] = _optional_int(payload, 'spaces')
        if 'selections' in payload:
            payload['selections'] = _optional_selections(payload, 'selections')
        _optional_utc_naive_datetime(payload, 'updated_at')
        key = _identity(payload.get('external_booking_id') or _external_booking_id(payload), 'Booking identity', 120)
    else:
        key = _identity(payload.get('external_booking_id') or payload.get('booking_id'), 'Booking identity', 120)
    revision = _revision(source_revision if source_revision is not None else payload.get('source_revision', payload.get('revision')))
    updated_at = _naive(payload.get('updated_at'))
    received_at = _naive(received_at or utc_now())
    booking = EventBooking.query.filter_by(organization_id=event.organization_id, source=source,
        source_scope=source_scope, external_booking_id=key).populate_existing().first()
    adopting_scope = False
    # Old in-flight WordPress callbacks omit scope. Once an identity has been
    # recovered, reuse its unique retained scope instead of cloning a booking.
    # Provider is part of the canonical key; phone never participates in lookup.
    if booking is None and not source_scope and source in {
        'aoc-wordpress', 'wordpress_events_manager', 'wordpress_wpforms',
    }:
        retained = EventBooking.query.filter_by(organization_id=event.organization_id, source=source,
            event_id=event.id, external_booking_id=key).filter(
                EventBooking.source_scope != '',
            ).populate_existing().limit(2).all()
        if len(retained) > 1:
            raise ValueError('Unscoped source booking identity is ambiguous; retain its original form scope.')
        if retained:
            booking = retained[0]
        elif EventBooking.query.filter_by(organization_id=event.organization_id, source=source,
            external_booking_id=key).filter(EventBooking.source_scope != '').first() is not None:
            raise ValueError('A retained booking identity cannot move to a different event.')
    # Earlier plugin versions did not include a form scope. Recover that exact
    # identity only when its event matches, without moving historical entries.
    if booking is None and source_scope:
        unscoped = EventBooking.query.filter_by(organization_id=event.organization_id, source=source,
            source_scope='', external_booking_id=key).populate_existing().first()
        if unscoped is not None:
            wordpress_source = source in {'aoc-wordpress', 'wordpress_events_manager', 'wordpress_wpforms'}
            if wordpress_source and unscoped.event_id != event.id:
                raise ValueError('A retained booking identity cannot move to a different event.')
            compatible_wordpress = wordpress_source and unscoped.source_revision is None
            if unscoped.event_id == event.id and (unscoped.legacy_evidence or compatible_wordpress):
                booking = unscoped
                adopting_scope = True
    if booking is not None and booking.event_id != event.id:
        raise ValueError('A retained booking identity cannot move to a different event.')
    status = str(payload.get('status') or 'active').strip().lower()[:30]
    active = not deleted and payload.get('active') is not False and status not in CANCELLED_STATUSES
    if booking is not None and not _is_newer(booking, revision, updated_at, reactivating=active and not booking.is_active):
        booking._ledger_applied = False
        return booking
    if booking is not None and source_scope and (booking.legacy_evidence or adopting_scope):
        booking.source_scope = source_scope
    raw_spaces = payload.get('spaces', payload.get('party_size', 1))
    try:
        spaces = int(raw_spaces) if raw_spaces is not None else 1
    except (TypeError, ValueError) as exc:
        raise ValueError('Booking places must be a nonnegative integer.') from exc
    if spaces < 0 or spaces > 2147483647 or isinstance(raw_spaces, (bool, float)):
        raise ValueError('Booking places must be a nonnegative integer.')
    if booking is None:
        booking = EventBooking(organization_id=event.organization_id, event_id=event.id,
            source=source, source_scope=source_scope, external_booking_id=key)
        db.session.add(booking)
    was_active = booking.is_active
    old_phone = booking.phone
    if not deleted or 'phone' in payload:
        raw_phone = str(payload.get('phone') or '').strip()
        normalized = normalize_phone(raw_phone) if raw_phone else ''
        booking.phone = normalized if validate_phone(normalized) else None
        booking.contact_state = 'valid' if booking.phone else ('invalid' if raw_phone else 'missing')
    elif booking.contact_state is None:
        booking.contact_state = 'valid' if old_phone else 'missing'
    # A minimal source deletion is a tombstone, not a new contact
    # representation. Retain omitted metadata; explicitly supplied blanks clear.
    if not deleted or 'name' in payload:
        booking.name = str(payload.get('name') or '').strip()[:100] or None
    booking.party_size = spaces if ('spaces' in payload or 'party_size' in payload or booking.party_size is None) else booking.party_size
    booking.status = 'cancelled' if deleted else status
    booking.is_active = active
    if not deleted or 'sms_consent' in payload:
        booking.sms_consent = payload.get('sms_consent') if isinstance(payload.get('sms_consent'), bool) else None
    if not deleted or 'person_id' in payload:
        booking.external_person_id = str(payload.get('person_id') or '').strip()[:80] or None
    if not deleted or 'selections' in payload:
        booking.selections_json = json.dumps(payload['selections'], ensure_ascii=False, sort_keys=True) if isinstance(payload.get('selections'), list) else None
    if not deleted or 'comment' in payload:
        booking.comment = str(payload.get('comment') or '').strip() or None
    booking.source_revision = revision
    booking.source_updated_at = updated_at
    booking.last_received_at = received_at
    booking.payload_fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    booking.legacy_evidence = False
    booking._ledger_applied = True
    booking._previous_phone = old_phone
    if not active and (was_active or booking.cancelled_at is None):
        _retain_cancellation(booking, received_at, 'source_cancelled')
    db.session.flush()
    if project:
        reconcile_event_projection(event, seed_legacy=False)
    booking._permission_transition = _permission_transition(event, booking, payload, old_phone, received_at)
    if sync_permissions:
        _sync_source_permission(event, booking, payload, old_phone, received_at)
    return booking


def _retain_cancellation(booking, received_at, reason):
    booking.cancelled_at = received_at
    history = booking.cancellation_history
    history.append({'at': received_at.isoformat(), 'reason': reason, 'revision': booking.source_revision,
                    'source_updated_at': _naive(booking.source_updated_at).isoformat() if booking.source_updated_at else None})
    booking.cancellation_history_json = json.dumps(history, sort_keys=True)


def _sync_source_permission(event, booking, payload, old_phone, received_at):
    if booking.source in LOCAL_SOURCES:
        return
    from app.services.aoc_event_sync_service import (
        _record_booking_permission, _record_booking_withdrawal, upsert_community_member_contact,
    )
    permission_identity = f'ledger:{booking.id}' if booking.source_scope else booking.external_booking_id
    community_signal = payload.get('community_sms_consent')
    has_community_signal = isinstance(community_signal, bool)
    if booking.is_active and booking.phone:
        upsert_community_member_contact(booking.name, booking.phone)
        # Keep explicit community-purpose evidence independent from the event
        # booking's active state, without recording it twice on an active row.
        purposes = ('event',) if has_community_signal else ('event', 'community')
        _record_booking_permission(event, payload, booking.source, booking.phone, permission_identity, received_at,
                                   purposes=purposes)
    if has_community_signal:
        # A deletion can carry an explicit community-purpose signal even when
        # its phone was omitted (retained) or explicitly cleared in this update.
        # A positive signal cannot invent a destination after a contact clear.
        destination = booking.phone or (old_phone if community_signal is False else None)
        if destination:
            # Preserve each organization's existing consent contract. In
            # particular AOC's owner-confirmed signup policy retains raw false
            # source fields without treating them as a verified broad opt-out.
            _record_booking_permission(event, payload, booking.source, destination,
                                       permission_identity, received_at, purposes=('community',))
    withdrawn_phone = old_phone if old_phone and old_phone != booking.phone else (booking.phone if not booking.is_active else None)
    if withdrawn_phone:
        sibling = EventBooking.query.filter_by(organization_id=event.organization_id,
            event_id=event.id, is_active=True, phone=withdrawn_phone).filter(EventBooking.id != booking.id).first()
        if sibling is None:
            _record_booking_withdrawal(event, payload, booking.source, withdrawn_phone,
                permission_identity, received_at,
                reason='booking_phone_changed' if old_phone != booking.phone else 'booking_cancelled')


def apply_booking_permission_transitions(event, transitions):
    """Apply a complete batch's explicit effects against its final booking state."""
    _lock_event(event)
    db.session.flush()
    for transition in transitions:
        if not isinstance(transition, BookingPermissionTransition) or (
                transition.organization_id != event.organization_id or transition.event_id != event.id):
            raise ValueError('Booking permission transition belongs to another event or organization.')
        booking = EventBooking.query.filter_by(id=transition.booking_id,
            organization_id=event.organization_id, event_id=event.id).populate_existing().first()
        if booking is None:
            raise ValueError('Booking permission transition has no retained booking.')
        _sync_source_permission(event, booking, json.loads(transition.payload_json),
            transition.old_phone, transition.received_at)


def cancel_booking(booking, received_at, source_revision=None, source_updated_at=None, reason='inventory_missing', *, project=True, sync_permissions=True):
    """Withdraw one contribution only when it is no newer than the inventory."""
    event = Event.query.filter_by(id=booking.event_id, organization_id=booking.organization_id).first()
    if event is None:
        raise ValueError('Booking event was not found.')
    _lock_event(event)
    booking = EventBooking.query.filter_by(id=booking.id, organization_id=event.organization_id).populate_existing().first()
    if booking is None:
        raise ValueError('Booking was not found.')
    revision = _revision(source_revision)
    watermark = _naive(source_updated_at)
    if revision is not None and booking.source_revision is not None and booking.source_revision > revision:
        return False
    if watermark is not None and booking.source_updated_at is not None and _naive(booking.source_updated_at) > watermark:
        return False
    if reason == 'inventory_missing' and watermark is not None and _naive(booking.last_received_at) > watermark and booking.source_updated_at is None:
        return False
    if not booking.is_active:
        return False
    booking.is_active = False
    booking.status = 'cancelled'
    booking.source_revision = revision if revision is not None else booking.source_revision
    booking.source_updated_at = watermark if watermark is not None else booking.source_updated_at
    booking.last_received_at = _naive(received_at)
    _retain_cancellation(booking, _naive(received_at), reason)
    db.session.flush()
    if project:
        reconcile_event_projection(event, seed_legacy=False)
    permission_payload = ({'updated_at': booking.source_updated_at.replace(tzinfo=timezone.utc).isoformat()}
                          if booking.source_updated_at else {})
    booking._permission_transition = _permission_transition(event, booking, permission_payload, booking.phone, received_at)
    if sync_permissions:
        _sync_source_permission(event, booking, permission_payload, booking.phone, _naive(received_at))
    return True


def _active_bookings(event):
    return EventBooking.query.filter_by(organization_id=event.organization_id, event_id=event.id,
        is_active=True, duplicate_of_id=None).order_by(EventBooking.id).populate_existing().all()


def reconcile_event_projection(event, *, seed_legacy=True):
    _lock_event(event)
    if seed_legacy:
        ensure_legacy_bookings(event)
    grouped = {}
    for booking in _active_bookings(event):
        if booking.phone and booking.contact_state == 'valid':
            grouped.setdefault(booking.phone, []).append(booking)
    registrations = EventRegistration.query.filter_by(organization_id=event.organization_id, event_id=event.id).populate_existing().all()
    by_phone = {row.phone: row for row in registrations}
    for row in registrations:
        if row.phone not in grouped:
            # Preserve the ledger independently before removing its current
            # messaging projection. Historical bookings retain their identity.
            for booking in EventBooking.query.filter_by(organization_id=event.organization_id,
                    projection_registration_id=row.id).all():
                booking.projection_registration_id = None
            db.session.delete(row)
    db.session.flush()
    identities = {}
    for phone, bookings in grouped.items():
        representative = bookings[0]
        source = representative.source[:50] if len(bookings) == 1 and representative.source not in LOCAL_SOURCES else None
        booking_id = ((f'ledger:{representative.id}' if representative.source_scope or len(representative.external_booking_id) > 80
            else representative.external_booking_id) if source else None)
        identities[phone] = (source, booking_id)
        row = by_phone.get(phone)
        if row is not None and (row.external_source, row.external_booking_id) != (source, booking_id):
            # A complete snapshot can exchange destinations between bookings.
            # Release every changed compatibility identity before assigning any
            # of them to another retained contact row in the same transaction.
            row.external_source = None
            row.external_booking_id = None
    db.session.flush()
    for phone, bookings in grouped.items():
        row = by_phone.get(phone)
        if row is None:
            row = EventRegistration(organization_id=event.organization_id, event_id=event.id, phone=phone)
            db.session.add(row)
        representative = bookings[0]
        # The destination display follows the latest supplied contact name;
        # each transaction still retains its own original name in the ledger.
        latest = sorted(bookings, key=lambda item: (_naive(item.last_received_at), item.id or 0), reverse=True)
        row.name = next((item.name for item in latest if item.name), None)
        # Preserve literal source identity on a singleton for compatibility;
        # combined contacts deliberately carry no fabricated booking identity.
        row.external_source, row.external_booking_id = identities[phone]
        row.external_person_id = representative.external_person_id if len(bookings) == 1 else None
        row.external_booking_status = 'pending' if all(item.status == 'pending' for item in bookings) else representative.status
        row.booking_spaces = sum(item.party_size for item in bookings)
        row.selections_json = representative.selections_json if len(bookings) == 1 else None
        row.booking_comment = representative.comment if len(bookings) == 1 else None
        row.external_updated_at = max((item.source_updated_at for item in bookings if item.source_updated_at), default=None)
        row.synced_at = max(_naive(item.last_received_at) for item in bookings)
        db.session.flush()
        for booking in bookings:
            booking.projection_registration_id = row.id
    db.session.flush()
    return {'contacts': len(grouped)}


def create_local_booking(event, name, phone, party_size=1, source='manual', external_booking_id=None):
    if source not in LOCAL_SOURCES:
        raise ValueError('Local registration source is not supported.')
    booking = upsert_booking(event, {'booking_id': external_booking_id or secrets.token_hex(16),
        'name': name, 'phone': phone, 'spaces': party_size, 'status': 'confirmed',
        'updated_at': utc_now().isoformat()}, source, utc_now())
    from app.services.aoc_event_sync_service import upsert_community_member_contact
    if booking.phone:
        upsert_community_member_contact(booking.name, booking.phone)
    return booking


def cancel_registration_bookings(registration):
    event = registration.event
    _lock_event(event)
    ensure_legacy_bookings(event)
    contributions = EventBooking.query.filter_by(organization_id=event.organization_id,
        event_id=event.id, phone=registration.phone, is_active=True).all()
    if any(item.source not in LOCAL_SOURCES and not item.legacy_evidence for item in contributions):
        raise ValueError('This contact has source-managed bookings; cancel those bookings at their source.')
    for booking in contributions:
        cancel_booking(booking, utc_now(), reason='operator_removed')


def project_legacy_registration(registration, source_row_id):
    """Retain an imported legacy row without claiming full source coverage."""
    event = registration.event or Event.query.filter_by(id=registration.event_id,
        organization_id=registration.organization_id).first()
    _lock_event(event)
    db.session.flush()
    key = f'event:{event.id}:row:{source_row_id}'
    booking = EventBooking.query.filter_by(organization_id=event.organization_id,
        source='legacy_import', source_scope='', external_booking_id=key).first()
    if booking is None:
        booking = EventBooking(organization_id=event.organization_id,event_id=event.id,
            source='legacy_import',source_scope='',external_booking_id=key)
        db.session.add(booking)
    booking.name = registration.name
    booking.phone = registration.phone
    booking.contact_state = 'valid'
    booking.status = registration.external_booking_status or 'legacy'
    booking.is_active = True
    booking.party_size = registration.capacity_spaces
    booking.projection_registration_id = registration.id
    booking.legacy_evidence = True
    booking.last_received_at = _naive(utc_now())
    db.session.flush()
    reconcile_event_projection(event)
    return booking


def link_duplicate_booking(booking, primary):
    """Explicitly link two representations; phones never imply a duplicate."""
    if booking.id == primary.id or booking.event_id != primary.event_id or booking.organization_id != primary.organization_id:
        raise ValueError('Duplicate bookings must be different records in the same event and organization.')
    event = booking.event
    _lock_event(event)
    booking = EventBooking.query.filter_by(id=booking.id, organization_id=event.organization_id,
        event_id=event.id).populate_existing().one()
    primary = EventBooking.query.filter_by(id=primary.id, organization_id=event.organization_id,
        event_id=event.id).populate_existing().one()
    if primary.duplicate_of_id is not None:
        raise ValueError('Select a primary booking, not another duplicate.')
    if EventBooking.query.filter_by(organization_id=event.organization_id, event_id=event.id,
            duplicate_of_id=booking.id).first() is not None:
        raise ValueError('This booking already represents linked records. Unlink those records first.')
    booking.duplicate_of_id = primary.id
    db.session.flush()
    reconcile_event_projection(event)


def unlink_duplicate_booking(booking):
    """Restore a separately counted transaction and its original evidence state."""
    event = booking.event
    _lock_event(event)
    booking = EventBooking.query.filter_by(id=booking.id,organization_id=event.organization_id,
        event_id=event.id).populate_existing().one()
    booking.duplicate_of_id = None
    db.session.flush()
    reconcile_event_projection(event)
    return booking


def roster_summary(event):
    active = _active_bookings(event)
    legacy_rows = EventRegistration.query.filter_by(organization_id=event.organization_id, event_id=event.id).count()
    all_bookings = EventBooking.query.filter_by(organization_id=event.organization_id, event_id=event.id).all()
    by_id = {item.id:item for item in all_bookings}
    unresolved_legacy = [item for item in all_bookings if item.legacy_evidence and not (
        item.duplicate_of_id in by_id and not by_id[item.duplicate_of_id].legacy_evidence)]
    unlinked_legacy = max(0, legacy_rows - len({item.projection_registration_id for item in all_bookings if item.projection_registration_id}))
    pending = [item for item in active if item.status in {'pending', 'submitted'}]
    from app.services.audience_service import event_registrations_query
    contact_rows = event_registrations_query(organization_id=event.organization_id, current_only=False).filter_by(event_id=event.id).all()
    from app.services.recipient_permission_service import filter_permission_recipients
    reachable = 0
    for start in range(0, len(contact_rows), 500):
        eligible, _, _ = filter_permission_recipients([
            {'phone': row.phone, 'name': row.name} for row in contact_rows[start:start+500]
        ], organization_id=event.organization_id, purpose='event', event_id=event.id)
        reachable += len(eligible)
    try:
        from app.services.booking_inventory_service import event_roster_coverage
        coverage = event_roster_coverage(event)
    except ImportError:
        coverage = {'trustworthy': False, 'status': 'unverified', 'reason': 'A complete source inventory has not been reviewed.'}
    return {'bookings': len(active) + unlinked_legacy,
        'reserved_places': sum(item.party_size for item in active) + sum(row.capacity_spaces for row in EventRegistration.query.filter_by(organization_id=event.organization_id,event_id=event.id).all() if row.id not in {item.projection_registration_id for item in all_bookings}),
        'pending_bookings': len(pending), 'confirmed_bookings': len(active) - len(pending),
        'reachable_contacts': reachable, 'missing_contact_details': sum(item.contact_state == 'missing' for item in active),
        'supplied_contacts': legacy_rows, 'permission_restricted_contacts': len(contact_rows) - reachable,
        'invalid_contact_details': sum(item.contact_state == 'invalid' for item in active),
        'legacy_records': len(unresolved_legacy) + unlinked_legacy,
        'coverage_verified': bool(coverage.get('trustworthy')), 'coverage_status': coverage.get('status', 'unverified'),
        'coverage_reason': coverage.get('reason'), 'last_complete_sync': coverage.get('last_complete_at'), 'coverage': coverage}

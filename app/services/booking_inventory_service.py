"""Version 2 inventories stage pages until their signed completion proof is whole.

A legacy ``reconcile_complete`` is only a heartbeat. It cannot authorize removal
or make a roster trustworthy. Source authority is a separate reviewed choice.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone, timedelta

from app import db
from app.booking_inventory_models import BookingInventory, BookingInventoryPage, EventRegistrationSource
from app.models import Event, EventRegistration, utc_now
from app.tenant import organization_context, tenant_scope_is_active, get_current_organization_id


class BookingInventoryError(ValueError):
    pass


def _naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return _naive(result)
    except (ValueError, TypeError) as exc:
        raise BookingInventoryError('Inventory watermark must include a timezone.') from exc


def _identity(value, label, maximum=160):
    value = str(value or '').strip()
    if not value or len(value) > maximum or not re.fullmatch(r'[A-Za-z0-9._:-]+', value):
        raise BookingInventoryError(f'Invalid inventory {label}.')
    return value


def _bounded_int(value, label, maximum):
    if isinstance(value, bool):
        raise BookingInventoryError(f'Invalid inventory {label}.')
    try:
        result = int(value)
        if str(result) != str(value) or not 0 <= result <= maximum:
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise BookingInventoryError(f'Invalid inventory {label}.') from exc


def configure_event_source(event, source, source_scope, actor_user_id=None, verified=False):
    """Record an intentionally selected source; verification requires a complete inventory."""
    if tenant_scope_is_active() and get_current_organization_id() != event.organization_id:
        raise BookingInventoryError('Registration source belongs to another organization.')
    source = _identity(source, 'source', 80)
    from app.services.booking_projection_service import LOCAL_SOURCES
    source_scope = str(source_scope or '').strip()
    if source_scope or source not in LOCAL_SOURCES:
        source_scope = _identity(source_scope, 'scope')
    row = EventRegistrationSource.query.filter_by(
        organization_id=event.organization_id, event_id=event.id,
        source=source, source_scope=source_scope,
    ).first()
    if row is None:
        row = EventRegistrationSource(organization_id=event.organization_id, event_id=event.id,
                                      source=source, source_scope=source_scope)
        db.session.add(row)
    if verified and (not row.last_complete_at or row.coverage_status != 'complete'):
        raise BookingInventoryError('Compare a complete source inventory before enabling roster authority.')
    row.authority_enabled = bool(verified)
    row.verified_by_user_id = actor_user_id if verified else None
    row.verified_at = _naive(utc_now()) if verified else None
    db.session.flush()
    return row


def review_local_source(event, source, source_scope='', actor_user_id=None):
    """Establish locally retained coverage only after an explicit operator comparison.

    Manual, imported and registration-survey transactions are already durable
    local writes. This review never invents missing legacy source identities,
    cancels other sources or treats a local import as an external inventory.
    """
    from app.booking_models import EventBooking
    from app.services.booking_projection_service import LOCAL_SOURCES, ensure_legacy_bookings
    if source not in LOCAL_SOURCES - {'legacy', 'legacy_import'}:
        raise BookingInventoryError('This source requires a complete external inventory.')
    if tenant_scope_is_active() and get_current_organization_id() != event.organization_id:
        raise BookingInventoryError('Registration source belongs to another organization.')
    locked_event = Event.query.filter_by(id=event.id, organization_id=event.organization_id).with_for_update().first()
    if locked_event is None:
        raise BookingInventoryError('Registration event was not found.')
    ensure_legacy_bookings(event)
    unresolved = event_roster_coverage(event)['legacy_evidence_count']
    if unresolved:
        raise BookingInventoryError('Resolve historical booking identities before reviewing a local roster.')
    local_count = EventBooking.query.filter_by(organization_id=event.organization_id, event_id=event.id,
        source=source, source_scope=source_scope).count()
    if not local_count:
        raise BookingInventoryError('This local source has no retained booking records to review.')
    row = configure_event_source(event, source, source_scope)
    now = _naive(utc_now())
    row.last_complete_at = now
    row.last_watermark = now
    row.last_inventory_id = 'local-review:' + now.isoformat()
    row.pending_inventory_id = None
    row.pending_watermark = None
    row.coverage_status = 'complete'
    db.session.flush()
    return configure_event_source(event, source, source_scope, actor_user_id=actor_user_id, verified=True)


def event_roster_coverage(event):
    from app.booking_models import EventBooking
    sources = EventRegistrationSource.query.filter_by(
        organization_id=event.organization_id, event_id=event.id,
    ).order_by(EventRegistrationSource.id).all()
    ledger_rows = EventBooking.query.filter_by(organization_id=event.organization_id, event_id=event.id).all()
    ledger_by_id = {row.id: row for row in ledger_rows}
    def resolved_legacy(row):
        target = ledger_by_id.get(row.duplicate_of_id)
        return bool(target is not None and not target.legacy_evidence and target.duplicate_of_id is None)
    legacy_count = sum(1 for row in ledger_rows if row.legacy_evidence and not resolved_legacy(row))
    linked_ids = db.session.query(EventBooking.projection_registration_id).filter(
        EventBooking.organization_id == event.organization_id,
        EventBooking.event_id == event.id,
        EventBooking.projection_registration_id.isnot(None),
    )
    untracked = EventRegistration.query.filter(
        EventRegistration.organization_id == event.organization_id,
        EventRegistration.event_id == event.id,
        ~EventRegistration.id.in_(linked_ids),
    ).count()
    configured_scopes = {(s.source, s.source_scope) for s in sources}
    ledger_scopes = {(row.source, row.source_scope) for row in ledger_rows if row.is_active and row.duplicate_of_id is None}
    missing_scopes = ledger_scopes - configured_scopes
    trustworthy = bool(sources) and all(s.authority_enabled and s.coverage_status == 'complete' for s in sources) and not legacy_count and not untracked and not missing_scopes
    reason = None if trustworthy else (
        'Historical registration identities still need comparison with the source.' if legacy_count or untracked
        else 'Active bookings include an unconfigured registration source that needs a complete inventory and review.' if missing_scopes
        else 'Source snapshots share a watermark; capture a strictly newer complete inventory.' if any(s.coverage_status == 'conflict' for s in sources)
        else 'A booking changed at the inventory watermark; capture a newer complete inventory to resolve its status.' if any(s.coverage_status == 'ambiguous' for s in sources)
        else 'Compare a complete inventory and explicitly approve every registration source.'
    )
    complete_times = [s.last_complete_at for s in sources if s.last_complete_at]
    return {'trustworthy': trustworthy, 'status': 'verified' if trustworthy else 'unverified',
            'last_complete_at': min(complete_times) if complete_times and len(complete_times) == len(sources) else None,
            'reason': reason, 'legacy_evidence_count': legacy_count + untracked, 'unconfigured_source_count': len(missing_scopes),
            'unconfigured_sources': [{'source': key[0], 'source_scope': key[1]} for key in sorted(missing_scopes)],
            'sources': sources}


def process_booking_inventory(payload, organization_id, received_at, *, source_namespace=None):
    """Called only after the existing organization-specific HMAC authentication."""
    from app.services.aoc_event_sync_service import (
        WORDPRESS_EVENT_SYNC_SOURCES, _event_source_for_booking_source, _upsert_event,
    )
    action = str(payload.get('action') or '')
    if action not in {'inventory_page', 'inventory_complete'}:
        raise BookingInventoryError('Unsupported inventory action.')
    if payload.get('protocol_version') != 2:
        raise BookingInventoryError('Booking inventory requires protocol version 2.')
    source = _identity(payload.get('source'), 'source', 80)
    if source_namespace is not None:
        if source != source_namespace:
            raise BookingInventoryError('Inventory source is outside the authenticated app namespace.')
    elif source not in WORDPRESS_EVENT_SYNC_SOURCES:
        raise BookingInventoryError('Unsupported inventory source.')
    manifest = payload.get('inventory')
    event_payload = payload.get('event')
    if not isinstance(manifest, dict) or not isinstance(event_payload, dict):
        raise BookingInventoryError('Inventory and event context are required.')
    inventory_key = _identity(manifest.get('id'), 'id', 100)
    source_scope = _identity(manifest.get('source_scope'), 'scope')
    watermark = _timestamp(manifest.get('watermark'))
    if watermark > _naive(received_at) + timedelta(minutes=5):
        raise BookingInventoryError('Inventory watermark is in the future; capture a current source snapshot.')
    pages = _bounded_int(manifest.get('pages'), 'page count', 10000)
    count = _bounded_int(manifest.get('records'), 'record count', 1000000)
    if pages < 1:
        raise BookingInventoryError('Inventory needs at least one page, including an empty roster.')
    if count > pages * 500:
        raise BookingInventoryError('Inventory record total exceeds its bounded page capacity.')
    event_external_id = str(event_payload.get('event_id') or '').strip()
    if not event_external_id:
        raise BookingInventoryError('Inventory requires an event identity.')
    normalized_manifest = {'id': inventory_key, 'source': source, 'source_scope': source_scope,
                           'event_id': event_external_id, 'watermark': watermark.isoformat(),
                           'pages': pages, 'records': count}
    manifest_digest = hashlib.sha256(json.dumps(normalized_manifest, sort_keys=True).encode()).hexdigest()
    with organization_context(organization_id):
        target_event_id = event_payload.get('target_event_id')
        if target_event_id is not None:
            target_event_id = _bounded_int(target_event_id, 'target event', 2147483647)
            binding = EventRegistrationSource.query.filter_by(organization_id=organization_id, event_id=target_event_id, source=source, source_scope=source_scope).first()
            if binding is None:
                raise BookingInventoryError('Application source is not configured for the target event.')
            event = Event.query.filter_by(id=binding.event_id, organization_id=organization_id).with_for_update().first()
            if event is None:
                raise BookingInventoryError('Configured target event was not found.')
        else:
            event = Event.query.filter_by(external_source=_event_source_for_booking_source(source), external_event_id=event_external_id).with_for_update().first()
        if event is None:
            # Inventory is intentionally separate from the event/reminder upsert path.
            event = _upsert_event(event_payload, _event_source_for_booking_source(source), _naive(received_at))
        inventory = BookingInventory.query.filter_by(organization_id=organization_id, inventory_id=inventory_key).with_for_update().first()
        if inventory is None:
            inventory = BookingInventory(organization_id=organization_id, event_id=event.id,
                inventory_id=inventory_key, source=source, source_scope=source_scope,
                watermark=watermark, page_count=pages, record_count=count,
                manifest_digest=manifest_digest, started_at=_naive(received_at))
            db.session.add(inventory)
            db.session.flush()
            source_record = EventRegistrationSource.query.filter_by(
                organization_id=organization_id, event_id=event.id, source=source, source_scope=source_scope,
            ).first()
            if source_record is None:
                source_record = configure_event_source(event, source, source_scope)
            latest_watermark = source_record.pending_watermark or source_record.last_watermark
            if latest_watermark and latest_watermark > watermark:
                inventory.status = 'superseded'
            elif latest_watermark == watermark and (source_record.pending_inventory_id or source_record.last_inventory_id) != inventory_key:
                # A timestamp tie cannot order two independently captured snapshots.
                # Neither snapshot may remove records until a newer capture resolves it.
                inventory.status = 'conflict'
                source_record.pending_inventory_id = inventory_key
                source_record.pending_watermark = watermark
                source_record.coverage_status = 'conflict'
            else:
                source_record.pending_inventory_id = inventory_key
                source_record.pending_watermark = watermark
                source_record.coverage_status = 'receiving'
        if inventory.manifest_digest != manifest_digest or inventory.event_id != event.id:
            raise BookingInventoryError('Inventory identity was reused with different scope or manifest.')
        if inventory.status in {'complete', 'superseded', 'conflict'}:
            return {'action': action, 'inventory_id': inventory_key, 'event_id': event.id, 'status': inventory.status, 'duplicate': True}
        if action == 'inventory_page':
            page_number = _bounded_int(payload.get('page'), 'page number', pages)
            if page_number < 1:
                raise BookingInventoryError('Inventory pages are one based.')
            records = payload.get('bookings')
            if not isinstance(records, list) or len(records) > 500 or any(not isinstance(r, dict) for r in records):
                raise BookingInventoryError('Inventory page must contain at most 500 booking objects.')
            # Preserve the exact JSON encoding sent by PHP for cross-runtime proof.
            encoded = payload.get('records_json')
            if not isinstance(encoded, str):
                raise BookingInventoryError('Inventory page requires its exact records_json proof bytes.')
            try:
                if json.loads(encoded) != records:
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                raise BookingInventoryError('Inventory page proof does not match booking records.') from exc
            digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            page = BookingInventoryPage.query.filter_by(inventory_id=inventory.id, page_number=page_number).first()
            if page is not None and page.payload_digest != digest:
                raise BookingInventoryError('Inventory page identity was reused with different records.')
            if page is None:
                db.session.add(BookingInventoryPage(organization_id=organization_id, inventory_id=inventory.id,
                    page_number=page_number, payload_digest=digest, records_json=encoded))
        else:
            proof = str(payload.get('completion_proof') or '')
            if not re.fullmatch(r'[a-f0-9]{64}', proof):
                raise BookingInventoryError('Inventory completion requires a SHA256 page proof.')
            if inventory.completion_proof and inventory.completion_proof != proof:
                raise BookingInventoryError('Inventory completion proof changed.')
            inventory.completion_proof = proof
        db.session.flush()
        _finish_inventory(inventory, event, _naive(received_at))
        return {'action': action, 'inventory_id': inventory_key, 'event_id': event.id, 'status': inventory.status}


def _finish_inventory(inventory, event, received_at):
    from app.booking_models import EventBooking
    from app.services.booking_projection_service import (
        upsert_booking, cancel_booking, reconcile_event_projection, apply_booking_permission_transitions,
    )
    page_metadata = BookingInventoryPage.query.with_entities(
        BookingInventoryPage.id, BookingInventoryPage.page_number, BookingInventoryPage.payload_digest,
    ).filter_by(inventory_id=inventory.id).order_by(BookingInventoryPage.page_number).all()
    if not inventory.completion_proof or len(page_metadata) != inventory.page_count:
        return
    proof = hashlib.sha256('\n'.join(p.payload_digest for p in page_metadata).encode('ascii')).hexdigest()
    if proof != inventory.completion_proof:
        raise BookingInventoryError('Inventory completion proof did not match all pages.')
    # Parse one bounded page at a time, validating every identity before any write.
    identities = set()
    record_count = 0
    for page_meta in page_metadata:
        page = db.session.get(BookingInventoryPage, page_meta.id)
        for record in json.loads(page.records_json):
            key = str(record.get('booking_id') or '').strip()
            alias = str(record.get('external_booking_id') or key).strip()
            if not key or len(key) > 120 or key in identities or alias != key:
                raise BookingInventoryError('Inventory contains missing, conflicting or duplicate booking identities.')
            identities.add(key)
            record_count += 1
            if record.get('source_scope', inventory.source_scope) != inventory.source_scope:
                raise BookingInventoryError('Inventory booking crosses its source scope.')
            if inventory.source.startswith('app:'):
                from app.services.app_registration_sync_service import validate_app_booking_record
                validate_app_booking_record(record)
                if record.get('source_revision') is None:
                    raise BookingInventoryError('Application inventory bookings require numeric source revisions.')
            updated = _timestamp(record.get('updated_at'))
            if updated > inventory.watermark:
                raise BookingInventoryError('Inventory includes a revision after its watermark.')
    if record_count != inventory.record_count:
        raise BookingInventoryError('Inventory record total did not match its manifest.')
    source = EventRegistrationSource.query.filter_by(organization_id=event.organization_id, event_id=event.id,
        source=inventory.source, source_scope=inventory.source_scope).first()
    if source is None:
        source = configure_event_source(event, inventory.source, inventory.source_scope)
    if (source.last_watermark and source.last_watermark > inventory.watermark) or (source.pending_watermark and source.pending_watermark > inventory.watermark):
        inventory.status = 'superseded'
        return
    if source.coverage_status == 'conflict' and source.pending_watermark == inventory.watermark:
        inventory.status = 'conflict'
        return
    from app.services.booking_projection_service import ensure_legacy_bookings
    ensure_legacy_bookings(event)
    seen = set()
    permission_transitions = []
    for page_meta in page_metadata:
        page = db.session.get(BookingInventoryPage, page_meta.id)
        for record in json.loads(page.records_json):
            booking = upsert_booking(event, record, inventory.source, received_at,
                source_scope=inventory.source_scope, source_revision=record.get('source_revision'),
                project=False, seed_legacy=False, sync_permissions=False)
            seen.add(booking.id)
            if booking._ledger_applied:
                permission_transitions.append(booking._permission_transition)
    candidates = EventBooking.query.filter_by(organization_id=event.organization_id, event_id=event.id,
        source=inventory.source, source_scope=inventory.source_scope, is_active=True, legacy_evidence=False).all()
    ambiguous_absence = False
    for booking in candidates:
        if booking.id in seen:
            continue
        if booking.last_received_at and _naive(booking.last_received_at) > inventory.started_at:
            continue
        if booking.source_updated_at and _naive(booking.source_updated_at) == inventory.watermark:
            # Per-booking numeric revisions cannot establish order against a
            # source-wide timestamp captured in the same clock tick.
            ambiguous_absence = True
            continue
        if cancel_booking(booking, received_at, source_updated_at=inventory.watermark,
                          reason='complete_inventory_missing', project=False, sync_permissions=False):
            permission_transitions.append(booking._permission_transition)
    # Contact corrections and cancellations see every source transaction's final
    # state. A same-timestamp destination swap must not create a transient denial.
    apply_booking_permission_transitions(event, permission_transitions)
    reconcile_event_projection(event, seed_legacy=False)
    inventory.status = 'complete'
    inventory.completed_at = received_at
    source.last_inventory_id = inventory.inventory_id
    source.last_complete_at = received_at
    source.last_watermark = inventory.watermark
    if source.pending_inventory_id == inventory.inventory_id:
        source.pending_inventory_id = None
        source.pending_watermark = None
        source.coverage_status = 'ambiguous' if ambiguous_absence else 'complete'
    db.session.flush()

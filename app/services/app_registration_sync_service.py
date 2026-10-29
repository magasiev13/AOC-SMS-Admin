"""Provider-neutral registration adapter for an organization's connected applications.

Authentication binds the integration and tenant before this module runs. External
source labels are always namespaced under that credential, never used as trusted
internal source names. Imports, commerce apps and registration systems share the
same ledger and complete-inventory contract as the WordPress adapter.
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone

from app import db
from app.models import Event, EventRegistrationSource
from app.tenant import organization_context


def app_source_namespace(integration, label):
    from app.services.aoc_event_sync_service import AocEventSyncPayloadError
    label = str(label or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,39}', label):
        raise AocEventSyncPayloadError('Application source must be a slug of at most 40 characters.')
    source = f'app:{integration.id}:{label}'
    # Event and contact compatibility columns retain their 50-character limit.
    if len(source) > 50:
        raise AocEventSyncPayloadError('Choose a shorter application source slug for this connection namespace.')
    return source



def validate_app_booking_record(record):
    """Reject ambiguous JSON types before they can alter audience eligibility."""
    from app.services.aoc_event_sync_service import AocEventSyncPayloadError
    if not isinstance(record, dict):
        raise AocEventSyncPayloadError('Booking context is required.')
    key = record.get('booking_id')
    if isinstance(key, bool) or not isinstance(key, (str, int)) or not str(key).strip():
        raise AocEventSyncPayloadError('Application booking identity must be a nonempty string or integer.')
    if str(record.get('external_booking_id') or key).strip() != str(key).strip():
        raise AocEventSyncPayloadError('Application booking identity aliases must agree.')
    for field in ('active', 'sms_consent', 'community_sms_consent'):
        if field in record and record[field] is not None and not isinstance(record[field], bool):
            raise AocEventSyncPayloadError('Application booking eligibility and consent fields must be JSON booleans.')
    for field in ('name', 'phone', 'status', 'comment'):
        if field in record and record[field] is not None and not isinstance(record[field], str):
            raise AocEventSyncPayloadError('Application booking contact and status fields must be strings.')
    if record.get('status') is not None and len(record['status']) > 30:
        raise AocEventSyncPayloadError('Application booking status must contain at most 30 characters.')


def process_app_registration_payload(payload, integration, received_at):
    from app.services.aoc_event_sync_service import AocEventSyncPayloadError, _upsert_event
    from app.services.booking_inventory_service import process_booking_inventory
    from app.services.booking_projection_service import upsert_booking
    if payload.get('protocol_version') != 2:
        raise AocEventSyncPayloadError('Application registration sync requires protocol version 2.')
    source = app_source_namespace(integration, payload.get('source'))
    action = str(payload.get('action') or '')
    data = deepcopy(payload)
    data['source'] = source
    try:
        if action in {'inventory_page', 'inventory_complete'}:
            return process_booking_inventory(data, integration.organization_id, received_at, source_namespace=source)
        if action not in {'event_upsert', 'event_deleted', 'booking_upsert', 'booking_deleted'}:
            raise AocEventSyncPayloadError('Unsupported application registration action.')
        event_payload = data.get('event')
        if not isinstance(event_payload, dict):
            raise AocEventSyncPayloadError('Event context is required.')
        event_key = str(event_payload.get('event_id') or '').strip()
        if not event_key:
            raise AocEventSyncPayloadError('External event identity is required.')
        with organization_context(integration.organization_id):
            target_event_id = event_payload.get('target_event_id')
            if target_event_id is not None:
                if isinstance(target_event_id, bool) or not str(target_event_id).isdigit():
                    raise AocEventSyncPayloadError('Target event must be a positive integer.')
                target_event_id = int(target_event_id)
                booking_context = data.get('booking')
                if not isinstance(booking_context, dict):
                    raise AocEventSyncPayloadError('Booking context is required.')
                scope = str(booking_context.get('source_scope') or '').strip()
                binding = EventRegistrationSource.query.filter_by(organization_id=integration.organization_id, event_id=target_event_id, source=source, source_scope=scope).first()
                if binding is None:
                    raise AocEventSyncPayloadError('Application source is not configured for the target event.')
                event = Event.query.filter_by(id=binding.event_id, organization_id=integration.organization_id).with_for_update().first()
                if event is None:
                    raise AocEventSyncPayloadError('Configured target event was not found.')
                if action in {'event_upsert', 'event_deleted'}:
                    raise AocEventSyncPayloadError('Linked registration sources cannot replace event details.')
            else:
                event = Event.query.filter_by(external_source=source, external_event_id=event_key).with_for_update().first()
            if action in {'event_upsert', 'event_deleted'}:
                # Preserve newer event details when an older source revision replays.
                incoming_modified = event_payload.get('modified_at')
                if not incoming_modified:
                    raise AocEventSyncPayloadError('Application event mutations require a source revision timestamp.')
                incoming = datetime.fromisoformat(str(incoming_modified).replace('Z', '+00:00'))
                if incoming.tzinfo is None:
                    raise AocEventSyncPayloadError('Event revision must include a timezone.')
                incoming = incoming.astimezone(timezone.utc).replace(tzinfo=None)
                if event:
                    if event.external_modified_at and incoming <= event.external_modified_at:
                        return {'action': action, 'event_id': event.id, 'ignored': True, 'reason': 'stale_source_revision'}
                event = _upsert_event(event_payload, source, received_at)
                if action == 'event_deleted':
                    event.external_status = 'deleted'
                # Connecting an app never silently creates community reminders.
                return {'action': action, 'event_id': event.id, 'scheduled_created': 0}
            if event is None:
                event = _upsert_event(event_payload, source, received_at)
            record = data.get('booking')
            validate_app_booking_record(record)
            scope = str(record.get('source_scope') or '').strip()
            if not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', scope):
                raise AocEventSyncPayloadError('Application bookings require an explicit source scope.')
            if record.get('source_revision') is None:
                raise AocEventSyncPayloadError('Application bookings require a numeric source revision.')
            booking = upsert_booking(event, record, source, received_at,
                source_scope=scope, source_revision=record['source_revision'], deleted=action == 'booking_deleted')
            db.session.flush()
            return {'action': action, 'event_id': event.id, 'booking_id': booking.id,
                    'registration_id': booking.projection_registration_id,
                    'ignored': not bool(getattr(booking, '_ledger_applied', False)), 'scheduled_created': 0}
    except (ValueError, TypeError) as exc:
        if isinstance(exc, AocEventSyncPayloadError):
            raise
        raise AocEventSyncPayloadError(str(exc)) from exc

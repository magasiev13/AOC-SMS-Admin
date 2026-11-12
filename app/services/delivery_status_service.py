"""Monotonic delivery evidence and independently complete usage facts per SID.

Callers hold the usage row lock and commit the observation, inbox projection and
failure suppression together. Submission attempts and log success counts never
change when a later delivery receipt arrives.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from flask import current_app

from app import db
from app.models import InboxMessage, MessagingUsageRecord, Organization, OrganizationProviderAuditLog, utc_now
from app.utils import as_utc_datetime


DELIVERY_TERMINAL = {'delivered', 'read', 'undelivered', 'failed', 'canceled'}
DELIVERY_CLOSED = DELIVERY_TERMINAL | {'unknown', 'conflict'}
DELIVERY_PROGRESS = {'pending': 0, 'scheduled': 1, 'accepted': 2, 'queued': 3, 'sending': 4, 'sent': 5}
DELIVERY_LABELS = {
    'pending': 'Awaiting delivery receipt', 'scheduled': 'Scheduled by provider',
    'accepted': 'Awaiting delivery receipt', 'queued': 'Queued by provider', 'sending': 'Sending to carrier',
    'sent': 'Sent to carrier; receipt pending', 'delivered': 'Delivered', 'read': 'Read receipt',
    'undelivered': 'Undelivered', 'failed': 'Delivery failed', 'canceled': 'Provider canceled',
    'unknown': 'Delivery unconfirmed; receipt window expired', 'conflict': 'Conflicting delivery receipts',
}


def utc_naive(value=None):
    return as_utc_datetime(value or utc_now()).replace(tzinfo=None)


def delivery_label(status):
    return DELIVERY_LABELS.get(status, 'Delivery not confirmed')


def _initialize_windows(record, now):
    record.delivery_status = record.delivery_status or 'pending'
    record.cost_status = record.cost_status or 'pending'
    record.units_status = record.units_status or 'pending'
    record.reconcile_attempt_count = int(record.reconcile_attempt_count or 0)
    if record.delivery_expires_at is None:
        record.delivery_expires_at = now + timedelta(hours=max(1, int(current_app.config.get('DELIVERY_RECEIPT_TIMEOUT_HOURS', 72))))
    if record.cost_expires_at is None:
        record.cost_expires_at = now + timedelta(days=max(1, int(current_app.config.get('USAGE_FACTS_TIMEOUT_DAYS', 7))))


def _retain_observation(record, *, status, code, source, now):
    evidence = json.loads(record.delivery_evidence_json) if record.delivery_evidence_json else []
    if not isinstance(evidence, list):
        raise ValueError('Stored delivery evidence is malformed; manual review is required.')
    match = next((row for row in evidence if row.get('status') == status and row.get('error_code') == code), None)
    if match is None:
        evidence.append({'status': status, 'error_code': code, 'first_observed_at': now.isoformat(), 'sources': [source]})
    elif source not in match.get('sources', []):
        match['sources'] = [*match.get('sources', []), source]
    record.delivery_evidence_json = json.dumps(evidence, sort_keys=True)


def finish_observation(record, *, now=None):
    """Age missing facts out explicitly; never infer delivery or a zero charge."""
    now = utc_naive(now)
    _initialize_windows(record, now)
    before = (record.delivery_status, record.delivery_error_code, record.delivery_error_message)
    if record.delivery_status not in DELIVERY_CLOSED and now >= record.delivery_expires_at:
        record.delivery_status = 'unknown'
        record.delivery_finalized_at = now
    if now >= record.cost_expires_at:
        if record.cost_status == 'pending':
            record.cost_status = 'unknown'
        if record.units_status == 'pending':
            record.units_status = 'unknown'
    facts_complete = record.cost_status == record.units_status == 'known' and record.billing_period_start is not None and record.billing_period_end is not None
    if record.delivery_status in DELIVERY_CLOSED and facts_complete:
        record.reconciliation_status = 'finalized'
        record.next_reconcile_at = None
    elif record.cost_status == 'unknown' or record.units_status == 'unknown' or (not record.billing_period_start and now >= record.cost_expires_at):
        record.reconciliation_status = 'unknown'
        record.next_reconcile_at = (
            now + timedelta(hours=1) if record.delivery_status not in DELIVERY_CLOSED else None
        )
    else:
        record.reconciliation_status = 'pending'
        delay = min(3600, 60 * (2 ** min(6, max(0, int(record.reconcile_attempt_count or 0) - 1))))
        record.next_reconcile_at = now + timedelta(seconds=delay)
    record.reconciled_at = now
    if before != (record.delivery_status, record.delivery_error_code, record.delivery_error_message):
        record.delivery_updated_at = now
    InboxMessage.query.filter_by(
        organization_id=record.organization_id, message_sid=record.message_sid, direction='outbound',
    ).update({InboxMessage.delivery_status: record.delivery_status,
              InboxMessage.delivery_error: record.delivery_error_message or record.delivery_error_code}, synchronize_session=False)


def observe_usage_record(record, message, *, source, include_cost=True, now=None):
    """Merge one verified observation into a locked existing SID ledger row."""
    from app.services import twilio_service

    now = utc_naive(now)
    _initialize_windows(record, now)
    before = (record.delivery_status, record.delivery_error_code, record.delivery_error_message)
    provider_created = getattr(message, 'date_created', None)
    if isinstance(provider_created, datetime):
        created = utc_naive(provider_created)
        record.delivery_expires_at = min(record.delivery_expires_at, created + timedelta(hours=max(1, int(current_app.config.get('DELIVERY_RECEIPT_TIMEOUT_HOURS', 72)))))
        record.cost_expires_at = min(record.cost_expires_at, created + timedelta(days=max(1, int(current_app.config.get('USAGE_FACTS_TIMEOUT_DAYS', 7)))))
    status = str(getattr(message, 'status', '') or '').strip().lower()
    if status == 'cancelled':
        status = 'canceled'
    if status not in DELIVERY_TERMINAL and status not in DELIVERY_PROGRESS:
        raise ValueError('Unsupported outbound provider status.')
    code = str(getattr(message, 'error_code', '') or '').strip() or None
    error = str(getattr(message, 'error_message', '') or '').strip() or None
    if record.historical_observation_only is not False or record.billing_review_required:
        existing_review = OrganizationProviderAuditLog.query.filter_by(
            organization_id=record.organization_id, action='usage_historical_review',
        ).filter(OrganizationProviderAuditLog.metadata_json.contains(json.dumps(record.message_sid))).first()
        if existing_review is None:
            db.session.add(OrganizationProviderAuditLog(
                organization_id=record.organization_id, action='usage_historical_review', status='pending',
                message='Recovered historical provider facts require manual billing review; no automatic historical settlement is authorized.',
                metadata_json=json.dumps({
                    'message_sid': record.message_sid, 'usage_record_id': record.id,
                    'prior_provider_cost': str(record.provider_cost or 0), 'prior_billable_units': int(record.billable_units or 0),
                    'prior_sell_amount': str(record.sell_amount or 0),
                    'billing_period_start': record.billing_period_start.isoformat() if record.billing_period_start else None,
                    'billing_period_end': record.billing_period_end.isoformat() if record.billing_period_end else None,
                }, sort_keys=True), created_at=now,
            ))
    _retain_observation(record, status=status, code=code, source=source, now=now)
    previous = record.delivery_status
    if previous in DELIVERY_TERMINAL:
        # A stale queued/sent callback cannot undo a terminal receipt. Conflicting
        # terminal receipts retain both facts and request explicit investigation.
        equivalent = previous == status or {previous, status} <= {'delivered', 'read'}
        if status in DELIVERY_TERMINAL and not equivalent:
            record.delivery_status = 'conflict'
            record.delivery_error_message = 'Conflicting authenticated terminal receipts; delivery requires review.'
        elif status == 'read' and previous == 'delivered':
            record.delivery_status = 'read'
            record.twilio_message_status = 'read'
    elif previous == 'conflict':
        pass
    elif status in DELIVERY_TERMINAL:
        record.delivery_status = status
        record.twilio_message_status = status
        record.delivery_finalized_at = now
    elif previous != 'unknown' and DELIVERY_PROGRESS.get(status, 0) >= DELIVERY_PROGRESS.get(previous, 0):
        record.delivery_status = status
        record.twilio_message_status = status
    if status in {'undelivered', 'failed', 'canceled'} and record.delivery_status != 'conflict':
        record.delivery_error_code = code or record.delivery_error_code
        record.delivery_error_message = error or record.delivery_error_message or (f'Provider error {code}' if code else None)

    if include_cost:
        raw_price = getattr(message, 'price', None)
        currency = str(getattr(message, 'price_unit', '') or '').strip().lower()
        if record.cost_status != 'known' and raw_price not in {None, ''} and currency:
            try:
                cost = Decimal(str(raw_price))
                if not cost.is_finite() or len(currency) > 8:
                    raise ValueError('Invalid provider cost facts.')
            except InvalidOperation as exc:
                raise ValueError('Invalid provider price.') from exc
            record.provider_cost = abs(cost)
            record.provider_currency = currency
            record.cost_status = 'known'
            record.cost_finalized_at = now
        raw_segments = getattr(message, 'num_segments', None)
        if record.units_status != 'known' and raw_segments not in {None, ''}:
            try:
                segments = int(raw_segments)
            except (TypeError, ValueError) as exc:
                raise ValueError('Invalid provider segment count.') from exc
            if segments < 0:
                raise ValueError('Invalid provider segment count.')
            if segments > 0 or (segments == 0 and record.cost_status == 'known' and status in {'failed', 'undelivered', 'canceled'}):
                record.billable_units = segments
                record.units_status = 'known'

    units = int(record.billable_units or 0)
    cost = Decimal(str(record.provider_cost or 0))
    billable = units > 0 and (cost > 0 or record.twilio_message_status in {'sent', 'delivered', 'read'})
    organization = db.session.get(Organization, record.organization_id)
    complimentary = twilio_service._organization_has_complimentary_billing(organization)
    record.billable = billable
    record.sell_rate = Decimal('0') if complimentary else twilio_service._currency_rate()
    record.sell_amount = Decimal('0') if complimentary or not billable else Decimal(units) * record.sell_rate
    record.margin = record.sell_amount - cost
    created_at = getattr(message, 'date_created', None)
    if isinstance(created_at, datetime) and not record.billing_period_start:
        # Once assigned, period identity is historical billing evidence. A later
        # read must not silently move it into another settlement window.
        start, end = twilio_service._period_window(created_at)
        record.billing_period_start, record.billing_period_end = utc_naive(start), utc_naive(end)
    record.last_error = None
    suppression = {'applied': False}
    if (record.historical_observation_only is False
            and record.delivery_status in {'failed', 'undelivered'}
            and record.suppression_applied_at is None):
        suppression = twilio_service.apply_usage_record_failure_suppression(
            record, twilio_message=SimpleNamespace(status=record.delivery_status, error_code=record.delivery_error_code,
                                                  error_message=record.delivery_error_message,
                                                  date_created=provider_created),
        )
        if suppression.get('applied'):
            record.suppression_applied_at = now
    finish_observation(record, now=now)
    if record.delivery_updated_at is None or before != (record.delivery_status, record.delivery_error_code, record.delivery_error_message):
        record.delivery_updated_at = now
    return suppression


def process_delivery_callback(organization_id, payload):
    """Apply an authenticated caller's already tenant/account-bound callback."""
    from app.services.twilio_service import _usage_record_expected_account
    from app.tenant import organization_context

    sid = str(payload.get('MessageSid') or payload.get('SmsSid') or '').strip()
    account = str(payload.get('AccountSid') or '').strip()
    if not sid or not account:
        raise ValueError('A provider SID and account are required.')
    with organization_context(organization_id):
        record = MessagingUsageRecord.query.filter_by(
            organization_id=organization_id, message_sid=sid,
        ).populate_existing().with_for_update().first()
        if record is None:
            # The provider can callback before submission persistence. Do not
            # invent billable app traffic; the normal SID poll remains fallback.
            db.session.commit()
            return {'status': 'unknown_sid'}
        if _usage_record_expected_account(record) != account:
            raise ValueError('The callback account does not own this usage SID.')
        suppression = observe_usage_record(record, SimpleNamespace(
            status=payload.get('MessageStatus') or payload.get('SmsStatus'),
            error_code=payload.get('ErrorCode'), error_message=payload.get('ErrorMessage') or payload.get('ChannelStatusMessage'),
        ), source='callback', include_cost=False)
        db.session.commit()
        return {'status': 'recorded', 'delivery_status': record.delivery_status, 'suppression_applied': bool(suppression.get('applied'))}


def _delivery_records(organization_id, details):
    sids = {str(row.get('sid') or '') for row in details if isinstance(row, dict) and row.get('sid')}
    return {
        record.message_sid: record for record in MessagingUsageRecord.query.filter(
            MessagingUsageRecord.organization_id == organization_id, MessagingUsageRecord.message_sid.in_(sids),
        ).all()
    } if sids else {}


def _canonical_delivery_error(message, code=None):
    """A carrier code has one display wording, independent of receipt source."""
    code = str(code or '').strip()
    message = str(message or '').strip()
    if not code and re.search(r'\b30003\b', message):
        code = '30003'
    if code == '30003':
        return 'Unreachable destination handset (30003)'
    if not code:
        return message or None
    # Unknown codes still need one wording across polling/callback sources.
    # Retain the provider's raw description in its audit row, not as a second
    # competing label for the same code in the recipient table.
    return f'Provider error ({code})'


def project_log_delivery(organization_id, details, *, records_by_sid=None):
    """Overlay current delivery facts without rewriting submission audit JSON."""
    records = _delivery_records(organization_id, details) if records_by_sid is None else records_by_sid
    projected = []
    for row in details:
        detail = dict(row)
        record = records.get(str(detail.get('sid') or ''))
        if record is not None and record.organization_id != organization_id:
            record = None
        status = record.delivery_status if record else ('pending' if detail.get('sid') or detail.get('success') is True else None)
        detail['delivery_status'] = status
        detail['delivery_label'] = delivery_label(status) if status else 'Not submitted'
        detail['delivery_tone'] = (
            'success' if status in {'delivered', 'read'} else
            'danger' if status in {'undelivered', 'failed', 'canceled'} else
            'warning text-dark' if status in {'unknown', 'conflict'} else 'secondary'
        )
        detail['delivery_error'] = _canonical_delivery_error(record.delivery_error_message, record.delivery_error_code) if record else None
        projected.append(detail)
    return projected


def _message_details(log):
    try:
        details = json.loads(log.details or '[]')
    except (TypeError, ValueError):
        return []
    if isinstance(details, dict):
        details = details.get('details') or details.get('results') or []
    return [dict(row) for row in details if isinstance(row, dict)] if isinstance(details, list) else []


def message_result_summary(log, projected_details=None):
    """Keep saved submission counts separate from current receipt evidence.

    An accepted message with no final receipt is unconfirmed, never delivered.
    Uncertain provider acceptance is tracked separately and is not a receipt.
    """
    if log is None:
        return None
    details = project_log_delivery(log.organization_id, _message_details(log)) if projected_details is None else projected_details
    receipts = {row['sid']: row for row in details if row.get('sid')}
    delivered = sum(row.get('delivery_status') in {'delivered', 'read'} for row in receipts.values())
    not_delivered = sum(row.get('delivery_status') in {'undelivered', 'failed', 'canceled'} for row in receipts.values())
    accepted = int(log.success_count or 0)
    return {
        'total': int(log.total_recipients or 0),
        'accepted': accepted,
        'not_submitted': int(log.failure_count or 0),
        'delivered': delivered,
        'not_delivered': not_delivered,
        'no_receipt': max(0, accepted - delivered - not_delivered),
        'acceptance_unresolved': sum(row.get('status') in {'sending', 'ambiguous', 'queued'} and row.get('success') is not True for row in details),
    }


def batch_message_results(logs):
    """Summarize a bounded history page with one receipt query per tenant."""
    groups = {}
    for log in logs:
        groups.setdefault(log.organization_id, []).append((log, _message_details(log)))
    result = {}
    for organization_id, entries in groups.items():
        records = _delivery_records(organization_id, [row for _, details in entries for row in details])
        for log, details in entries:
            result[log.id] = message_result_summary(log, project_log_delivery(organization_id, details, records_by_sid=records))
    return result

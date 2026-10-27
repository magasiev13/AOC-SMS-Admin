"""Explicit provider-evidence review; never submit or retry a provider message."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    AppUser,
    MessageDispatchAttempt,
    MessageLog,
    OrganizationMessagingProfile,
    OrganizationProviderAuditLog,
    OutboundRequest,
    utc_now,
)
from app.services.test_recipient_service import mask_phone_for_audit
from app.tenant import get_current_organization_id, organization_context, tenant_scope_is_active, without_tenant_scope
from app.utils import as_utc_datetime, normalize_phone, render_message_template, validate_phone


class OutboundReviewError(ValueError):
    """The supplied evidence cannot safely resolve this saved attempt."""


_ACCEPTED_PROVIDER_STATES = {'accepted', 'queued', 'sending', 'sent', 'delivered', 'read'}
_FAILED_PROVIDER_STATES = {'failed', 'undelivered', 'canceled', 'cancelled'}


@contextmanager
def _review_scope(organization_id: int):
    if not current_app.config.get('SAAS_MODE'):
        raise OutboundReviewError('Outbound review requires SAAS_MODE=1.')
    if int(organization_id) <= 0:
        raise OutboundReviewError('An explicit positive organization id is required.')
    if tenant_scope_is_active() and get_current_organization_id() != int(organization_id):
        raise OutboundReviewError('The requested organization is outside the active tenant scope.')
    with organization_context(int(organization_id)):
        yield


def _request(organization_id: int, request_id: int, *, locked=False):
    query = OutboundRequest.query.filter_by(id=int(request_id), organization_id=organization_id)
    if locked:
        query = query.populate_existing().with_for_update()
    request = query.first()
    if request is None:
        raise OutboundReviewError('The saved request does not belong to the specified organization.')
    return request


def _attempt_query(request):
    query = MessageDispatchAttempt.query.filter_by(organization_id=request.organization_id)
    if request.kind == 'blast' and request.message_log_id:
        return query.filter_by(message_log_id=request.message_log_id, scheduled_message_id=None)
    if request.kind == 'reply':
        return query.filter_by(logical_send_key=f'reply-request:{request.id}', scheduled_message_id=None)
    raise OutboundReviewError('This saved request has no supported durable attempt linkage.')


def _actor(actor_user_id: int):
    actor = AppUser.query.filter_by(id=int(actor_user_id)).populate_existing().first()
    if actor is None or not actor.is_platform_admin:
        raise OutboundReviewError('An existing platform administrator actor is required.')
    return actor


def _expected_account(organization_id: int) -> str:
    profile = OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).populate_existing().first()
    account = None
    if profile is not None:
        if profile.provider_mode == 'platform_managed':
            account = profile.twilio_subaccount_sid
        elif profile.provider_mode == 'customer_managed':
            account = profile.twilio_account_sid
    if not account:
        raise OutboundReviewError('The organization has no verifiable current provider account.')
    return str(account)


def _attempt_summary(attempt):
    return {
        'attempt_id': attempt.id,
        'status': attempt.status,
        'recipient': mask_phone_for_audit(attempt.recipient_phone),
        'body_digest': attempt.message_body_digest,
        'provider_message_sid': attempt.provider_message_sid,
        'sending_at': attempt.sending_at.isoformat() if attempt.sending_at else None,
        'last_error': attempt.last_error,
    }


def inspect_outbound_request(organization_id: int, request_id: int) -> dict:
    """Read local evidence only; no provider, queue, audit write, or commit."""
    with _review_scope(organization_id), db.session.no_autoflush:
        request = _request(organization_id, request_id)
        return {
            'organization_id': request.organization_id,
            'request_id': request.id,
            'kind': request.kind,
            'status': request.status,
            'message_log_id': request.message_log_id,
            'thread_id': request.thread_id,
            'attempts': [_attempt_summary(attempt) for attempt in _attempt_query(request).order_by(MessageDispatchAttempt.id).all()],
            'next_action': (
                'If every frozen recipient has a known outcome, use explicit --finalize-known. Otherwise match each unresolved attempt to a verified provider SID; never create a replacement send.'
                if request.status == 'ambiguous' else 'Inspect the saved request and its existing attempts.'
            ),
        }


def _frozen_attempt_identities(request) -> dict[str, tuple[str, str, str]]:
    """Reconstruct the original provider keys without expanding the audience."""
    from app.services.outbound_request_service import intent_digest

    try:
        payload = json.loads(request.payload_json)
        intent = payload['intent']
        recipients = payload['recipients']
        body = payload['message_body']
    except (TypeError, ValueError, KeyError) as exc:
        raise OutboundReviewError('The immutable request payload is incomplete or invalid.') from exc
    if (
        not isinstance(intent, dict) or not isinstance(recipients, list) or not recipients
        or not isinstance(body, str) or not body
        or intent.get('message_body') != body
        or request.payload_digest != intent_digest(request.kind, intent)
    ):
        raise OutboundReviewError('The saved audience/body do not match a valid immutable request intent.')
    identities = {}
    seen_phones = set()
    for recipient in recipients:
        if (
            not isinstance(recipient, dict) or not isinstance(recipient.get('phone'), str)
            or (recipient.get('name') is not None and not isinstance(recipient.get('name'), str))
        ):
            raise OutboundReviewError('The saved recipient snapshot is invalid.')
        phone = normalize_phone(recipient.get('phone'))
        if not validate_phone(phone) or phone in seen_phones:
            raise OutboundReviewError('The saved recipient snapshot contains an invalid or duplicate phone.')
        seen_phones.add(phone)
        if request.kind == 'blast':
            prefix = f'blast-log:{request.message_log_id}'
            personalized_body = render_message_template(body, recipient)
            key = prefix + ':' + hashlib.sha256(f'{prefix}|{phone}|{personalized_body}'.encode('utf-8')).hexdigest()
            send_kind = 'manual_live_test' if intent.get('test_mode') else 'blast'
        elif request.kind == 'reply':
            if (
                len(recipients) != 1 or not request.thread_id or intent.get('thread_id') != request.thread_id
                or normalize_phone(intent.get('phone')) != phone
            ):
                raise OutboundReviewError('The reply snapshot does not match its immutable conversation identity.')
            key = f'reply-request:{request.id}'
            personalized_body = body
            send_kind = 'manual_reply'
        else:
            raise OutboundReviewError('This request kind cannot be finalized from known attempt evidence.')
        identities[key] = (phone, hashlib.sha256(personalized_body.encode('utf-8')).hexdigest(), send_kind)
    return identities


def finalize_known_outbound_request(
    organization_id: int,
    request_id: int,
    *,
    actor_user_id: int,
    evidence_reference: str,
) -> dict:
    """Recover a stale aggregate only when every frozen outcome is already known.

    No provider/queue access occurs. Missing or unresolved recipient evidence
    rejects the whole operation, so this action cannot authorize new submissions.
    """
    reference = str(evidence_reference or '').strip()
    if not reference or len(reference) > 1024:
        raise OutboundReviewError('A retained evidence reference of 1–1024 characters is required.')
    with _review_scope(organization_id), db.session.no_autoflush:
        _actor(actor_user_id)
        request = _request(organization_id, request_id)
        original_kind, original_log_id = request.kind, request.message_log_id
        if original_kind == 'blast':
            log = MessageLog.query.filter_by(
                id=original_log_id, organization_id=organization_id,
            ).populate_existing().with_for_update().first()
            if log is None:
                raise OutboundReviewError('The saved request log is unavailable.')
        request = _request(organization_id, request_id, locked=True)
        if request.kind != original_kind or request.message_log_id != original_log_id:
            raise OutboundReviewError('Request linkage changed during the known-outcome review.')
        if request.status in {'accepted', 'failed'}:
            raise OutboundReviewError('The request already has a terminal known outcome; no recovery change was made.')
        _actor(actor_user_id)
        expected = _frozen_attempt_identities(request)
        attempts = _attempt_query(request).order_by(MessageDispatchAttempt.id).populate_existing().with_for_update().all()
        if {attempt.logical_send_key for attempt in attempts} != set(expected) or len(attempts) != len(expected):
            raise OutboundReviewError('Every frozen recipient must have exactly its original known attempt; missing or extra evidence requires manual review.')
        for attempt in attempts:
            if attempt.status not in {'sent', 'failed'}:
                raise OutboundReviewError('The request still has queued, sending or ambiguous outcomes; no recovery change was made.')
            if (attempt.recipient_phone, attempt.message_body_digest, attempt.send_kind) != expected[attempt.logical_send_key]:
                raise OutboundReviewError('An attempt phone, personalized body digest or send kind does not match the immutable request.')
            if attempt.status == 'sent' and not re.fullmatch(r'SM[0-9a-fA-F]{32}', str(attempt.provider_message_sid or '')):
                raise OutboundReviewError('A known accepted attempt must retain its provider SID; no acceptance was inferred.')
        try:
            previous_result = json.loads(request.result_json) if request.result_json else {}
        except (TypeError, ValueError) as exc:
            raise OutboundReviewError('The previous request result requires repair before an audit entry can be appended.') from exc
        if not isinstance(previous_result, dict) or not isinstance(previous_result.get('operator_reviews', []), list):
            raise OutboundReviewError('The previous request audit result has an unsupported shape.')
        now = utc_now().replace(tzinfo=None)
        accepted_count = sum(attempt.status == 'sent' for attempt in attempts)
        review = {
            'action': 'finalize_known', 'request_id': request.id, 'actor_user_id': int(actor_user_id),
            'evidence_reference': reference, 'recorded_at': now.isoformat(),
            'previous_request_status': request.status, 'frozen_recipient_count': len(expected),
            'accepted_count': accepted_count, 'failed_count': len(attempts) - accepted_count,
            'attempts': [
                {'attempt_id': attempt.id, 'logical_send_key': attempt.logical_send_key,
                 'status': attempt.status, 'provider_message_sid': attempt.provider_message_sid,
                 'body_digest': attempt.message_body_digest}
                for attempt in attempts
            ],
        }
        audit = OrganizationProviderAuditLog(
            organization_id=organization_id, actor_user_id=int(actor_user_id), action='outbound_known_finalize',
            status='success', message='Reviewed complete known attempt evidence for the original request; no provider or queue operation occurred.',
            metadata_json=json.dumps(review, sort_keys=True), created_at=now,
        )
        db.session.add(audit)
        db.session.flush()
        review['audit_id'] = audit.id
        previous_result['operator_reviews'] = [*previous_result.get('operator_reviews', []), review]
        request.result_json = json.dumps(previous_result, sort_keys=True)
        request.status = 'pending'
        request.last_error = None
        request.enqueue_lease = None
        request.enqueue_lease_until = None
        request.next_enqueue_at = now
        db.session.commit()
        return {
            'organization_id': organization_id, 'request_id': request_id, 'request_status': 'pending',
            'known_attempts': len(attempts), 'accepted_count': accepted_count, 'failed_count': len(attempts) - accepted_count,
            'audit_id': audit.id, 'provider_read_calls': 0, 'provider_create_calls': 0, 'enqueued': False,
        }


def resolve_outbound_attempt(
    organization_id: int,
    request_id: int,
    *,
    attempt_id: int,
    message_sid: str,
    actor_user_id: int,
    evidence_reference: str,
) -> dict:
    """Resolve one unknown outcome from an existing provider record.

    The provider lookup is read-only. A transaction then locks log -> request ->
    attempt, revalidates ownership and immutable identity, records audit proof,
    and returns the original request to deterministic recovery. It never resets
    an unresolved attempt to queued or starts a queue/provider action itself.
    """
    sid = str(message_sid or '').strip()
    if not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid):
        raise OutboundReviewError('Supply an explicit valid Twilio message SID (SM followed by 32 hexadecimal characters).')
    reference = str(evidence_reference or '').strip()
    if not reference or len(reference) > 1024:
        raise OutboundReviewError('A retained evidence reference of 1–1024 characters is required.')

    with _review_scope(organization_id), db.session.no_autoflush:
        _actor(actor_user_id)
        request = _request(organization_id, request_id)
        if request.status in {'accepted', 'failed'}:
            raise OutboundReviewError('The request already has a terminal known outcome; inspect it without changing it.')
        attempt = _attempt_query(request).filter_by(id=int(attempt_id)).first()
        if attempt is None or attempt.status not in {'sending', 'ambiguous'}:
            raise OutboundReviewError('Select a sending or ambiguous attempt belonging to this exact request.')
        expected = {
            'account_sid': _expected_account(organization_id),
            'phone': attempt.recipient_phone,
            'digest': attempt.message_body_digest,
            'log_id': request.message_log_id,
            'kind': request.kind,
            'sending_at': as_utc_datetime(attempt.sending_at),
            'resolved_at': as_utc_datetime(attempt.resolved_at),
        }
        if expected['sending_at'] is None:
            raise OutboundReviewError('The attempt has no submission timestamp; retained evidence requires manual review.')

        from app.services.twilio_service import _client_for_usage_reconciliation

        try:
            provider_record = _client_for_usage_reconciliation(organization_id).messages(sid).fetch()
        except Exception as exc:
            raise OutboundReviewError('The provider record could not be fetched; no outcome was changed.') from exc
        provider_status = str(getattr(provider_record, 'status', '') or '').lower()
        if str(getattr(provider_record, 'sid', '') or '') != sid:
            raise OutboundReviewError('The returned provider SID does not match the requested evidence.')
        if str(getattr(provider_record, 'account_sid', '') or '') != expected['account_sid']:
            raise OutboundReviewError('The provider record belongs to a different account.')
        if str(getattr(provider_record, 'direction', '') or '') != 'outbound-api':
            raise OutboundReviewError('The provider record is not an outbound API message.')
        if normalize_phone(getattr(provider_record, 'to', '')) != expected['phone']:
            raise OutboundReviewError('The provider recipient does not match the saved attempt.')
        body = getattr(provider_record, 'body', None)
        if not isinstance(body, str) or hashlib.sha256(body.encode('utf-8')).hexdigest() != expected['digest']:
            raise OutboundReviewError('The exact provider message body does not match the saved personalized digest.')
        if provider_status not in _ACCEPTED_PROVIDER_STATES | _FAILED_PROVIDER_STATES:
            raise OutboundReviewError('The provider status does not establish a supported known outcome.')
        provider_created_at = getattr(provider_record, 'date_created', None)
        if not isinstance(provider_created_at, datetime):
            raise OutboundReviewError('The provider record has no verifiable creation timestamp; manual review is required.')
        provider_created_at = as_utc_datetime(provider_created_at)
        if abs((provider_created_at - expected['sending_at']).total_seconds()) > 120:
            raise OutboundReviewError('The provider record was created outside this submission timestamp window.')
        if expected['resolved_at'] is not None and (provider_created_at - expected['resolved_at']).total_seconds() > 120:
            raise OutboundReviewError('The provider record was created after the saved attempt outcome window.')

        if expected['kind'] == 'blast':
            log = MessageLog.query.filter_by(
                id=expected['log_id'], organization_id=organization_id,
            ).populate_existing().with_for_update().first()
            if log is None:
                raise OutboundReviewError('The saved request log is unavailable.')
        request = _request(organization_id, request_id, locked=True)
        if request.kind != expected['kind'] or request.message_log_id != expected['log_id']:
            raise OutboundReviewError('Request linkage changed during evidence verification.')
        if request.status in {'accepted', 'failed'}:
            raise OutboundReviewError('The request was finalized while evidence was being verified; no change was made.')
        _actor(actor_user_id)
        if _expected_account(organization_id) != expected['account_sid']:
            raise OutboundReviewError('The provider account changed during evidence verification.')
        attempt = _attempt_query(request).filter_by(id=int(attempt_id)).populate_existing().with_for_update().first()
        if attempt is None or attempt.status not in {'sending', 'ambiguous'}:
            raise OutboundReviewError('The attempt was resolved while evidence was being verified; inspect the current outcome.')
        if (
            attempt.recipient_phone != expected['phone'] or attempt.message_body_digest != expected['digest']
            or as_utc_datetime(attempt.sending_at) != expected['sending_at']
            or as_utc_datetime(attempt.resolved_at) != expected['resolved_at']
        ):
            raise OutboundReviewError('Attempt identity changed during evidence verification.')
        if attempt.provider_message_sid and attempt.provider_message_sid != sid:
            raise OutboundReviewError('The attempt already records a different provider SID.')
        # Intentional platform boundary: an authenticated operator checks only
        # global SID uniqueness; no other tenant's record is returned or shown.
        with without_tenant_scope():
            used_sid = MessageDispatchAttempt.query.with_entities(MessageDispatchAttempt.id).filter(
                MessageDispatchAttempt.provider_message_sid == sid,
                MessageDispatchAttempt.id != attempt.id,
            ).first()
        if used_sid is not None:
            raise OutboundReviewError('This provider SID is already linked to another attempt.')
        try:
            previous_result = json.loads(request.result_json) if request.result_json else {}
        except (TypeError, ValueError) as exc:
            raise OutboundReviewError('The previous request result requires repair before an audit entry can be appended.') from exc
        if not isinstance(previous_result, dict) or not isinstance(previous_result.get('operator_reviews', []), list):
            raise OutboundReviewError('The previous request audit result has an unsupported shape.')

        now = utc_now()
        previous_status = attempt.status
        outcome = 'failed' if provider_status in _FAILED_PROVIDER_STATES else 'sent'
        attempt.provider_message_sid = sid
        attempt.status = outcome
        attempt.resolved_at = now.replace(tzinfo=None)
        attempt.last_error = (
            f'Verified provider terminal status: {provider_status}; code={getattr(provider_record, "error_code", None)}'
            if outcome == 'failed' else None
        )
        review = {
            'request_id': request.id, 'attempt_id': attempt.id, 'provider_message_sid': sid,
            'provider_account_sid': expected['account_sid'], 'provider_status': provider_status,
            'previous_attempt_status': previous_status, 'resolved_attempt_status': outcome,
            'actor_user_id': int(actor_user_id), 'evidence_reference': reference,
            'recorded_at': now.isoformat(), 'body_digest': expected['digest'],
            'provider_created_at': provider_created_at.isoformat(), 'attempt_sending_at': expected['sending_at'].isoformat(),
        }
        audit = OrganizationProviderAuditLog(
            organization_id=organization_id, actor_user_id=int(actor_user_id),
            action='outbound_attempt_review', status='success',
            message='Verified an existing provider message; no replacement message was submitted.',
            metadata_json=json.dumps(review, sort_keys=True),
            created_at=now.replace(tzinfo=None),
        )
        db.session.add(audit)
        try:
            db.session.flush()
        except IntegrityError as exc:
            db.session.rollback()
            raise OutboundReviewError('The evidence conflicts with concurrently saved dispatch history; inspect the current outcome.') from exc
        review['audit_id'] = audit.id
        previous_result['operator_reviews'] = [*previous_result.get('operator_reviews', []), review]
        request.result_json = json.dumps(previous_result, sort_keys=True)
        request.status = 'pending'
        request.last_error = None
        request.enqueue_lease = None
        request.enqueue_lease_until = None
        request.next_enqueue_at = now.replace(tzinfo=None)
        db.session.commit()
        return {'organization_id': organization_id, 'request_id': request_id, 'attempt_id': attempt_id,
                'attempt_status': outcome, 'request_status': 'pending', 'provider_message_sid': sid,
                'audit_id': audit.id, 'provider_create_calls': 0}

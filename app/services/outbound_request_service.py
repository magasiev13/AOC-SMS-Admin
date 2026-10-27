"""Persist intent before transport; retries reuse the same operation and attempts."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import MessageLog, Organization, OutboundRequest, utc_now
from app.services.outbound_idempotency_service import BLAST_JOB_TIMEOUT_SECONDS
from app.tenant import organization_context, without_tenant_scope


TERMINAL_STATES = {'accepted', 'failed', 'ambiguous'}
ACTIVE_QUEUE_STATES = {'queued', 'started', 'deferred', 'scheduled'}


def get_queue():
    # Keep the same injectable queue boundary used by routes and tests.
    from app.queue import get_queue as configured_queue
    return configured_queue()


class OutboundRequestConflict(ValueError):
    pass


def _now():
    return utc_now().replace(tzinfo=None)


def _json(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def intent_digest(kind: str, intent: dict) -> str:
    return hashlib.sha256(_json({'kind': kind, 'intent': intent}).encode()).hexdigest()


def normalize_request_key(raw: str | None, *, kind: str, intent: dict) -> str:
    key = str(raw or '').strip()
    if not key:
        # Compatibility for already-open pre-upgrade forms. A fresh rendered
        # form always issues a token; keyless retries cannot create new intent.
        return 'compat-' + intent_digest(kind, intent)[:56]
    if len(key) > 64 or not re.fullmatch(r'[A-Za-z0-9._:-]+', key):
        raise ValueError('Invalid send request key. Reload the form before starting a new send.')
    return key


def actor_identity(actor) -> tuple[str, int | None]:
    user_id = getattr(actor, 'id', None)
    if isinstance(user_id, int) and user_id > 0:
        return f'user:{user_id}', user_id
    name = str(getattr(actor, 'username', '') or '').strip()
    if not name:
        raise ValueError('An authenticated sender is required.')
    return 'user-name:' + hashlib.sha256(name.encode()).hexdigest(), None


def existing_outbound_request(organization_id: int, actor, key: str, kind: str, intent: dict):
    actor_key, _ = actor_identity(actor)
    request = OutboundRequest.query.filter_by(
        organization_id=organization_id, actor_key=actor_key, request_key=key,
    ).first()
    if request is not None and (request.kind != kind or request.payload_digest != intent_digest(kind, intent)):
        raise OutboundRequestConflict('This request key already belongs to different content. Start a new send from a fresh form.')
    if request is None:
        # A fresh page/token must not accidentally bypass unresolved acceptance.
        request = OutboundRequest.query.filter_by(
            organization_id=organization_id, actor_key=actor_key, kind=kind,
            payload_digest=intent_digest(kind, intent),
        ).filter(OutboundRequest.status.in_(['pending', 'queue_unknown', 'queued', 'processing', 'ambiguous'])).first()
    return request


def reserve_outbound_request(
    *, organization_id: int, actor, key: str, kind: str, intent: dict,
    recipient_data: list[dict], message_body: str, thread_id: int | None = None,
) -> tuple[OutboundRequest, bool]:
    if kind not in {'blast', 'reply'}:
        raise ValueError('Unknown outbound request kind.')
    # Serialize organization quotas/creation and recheck after acquiring lock.
    Organization.query.filter_by(id=organization_id).with_for_update().one()
    existing = existing_outbound_request(organization_id, actor, key, kind, intent)
    if existing is not None:
        db.session.commit()
        return existing, False
    actor_key, user_id = actor_identity(actor)
    payload = {'intent': intent, 'recipients': recipient_data, 'message_body': message_body}
    request = OutboundRequest(
        organization_id=organization_id, actor_user_id=user_id, actor_key=actor_key,
        request_key=key, kind=kind, payload_digest=intent_digest(kind, intent),
        payload_json=_json(payload), thread_id=thread_id, status='pending', next_enqueue_at=_now(),
    )
    try:
        with db.session.begin_nested():
            if kind == 'blast':
                log = MessageLog(
                    organization_id=organization_id, message_body=message_body,
                    target=intent['target'], event_id=intent.get('event_id'),
                    test_mode=bool(intent.get('test_mode')), status='processing',
                    total_recipients=len(recipient_data), success_count=0, failure_count=0, details='[]',
                    created_at=_now(),
                )
                db.session.add(log)
                db.session.flush()
                request.message_log_id = log.id
            db.session.add(request)
            db.session.flush()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = existing_outbound_request(organization_id, actor, key, kind, intent)
        if existing is None:
            raise
        return existing, False
    return request, True


def request_job_id(request: OutboundRequest) -> str:
    return f'twinevia-send-{request.organization_id}-{request.id}'


def result_state(result: dict) -> str:
    details = result.get('details') or [result]
    if any(row.get('status') in {'sending', 'ambiguous'} for row in details if isinstance(row, dict)):
        return 'ambiguous'
    if result.get('success') is False or int(result.get('failure_count') or 0):
        return 'failed'
    return 'accepted'


def complete_request(request: OutboundRequest, result: dict) -> None:
    if request.result_json:
        previous = json.loads(request.result_json)
        if previous.get('operator_reviews'):
            result = dict(result, operator_reviews=previous['operator_reviews'])
    request.status = result_state(result)
    request.result_json = _json(result)
    request.last_error = str(result.get('error') or '')[:2000] or None
    request.enqueue_lease = None
    request.enqueue_lease_until = None
    request.next_enqueue_at = None


def claim_reply_request(request: OutboundRequest) -> bool:
    now = _now()
    changed = OutboundRequest.query.filter(
        OutboundRequest.id == request.id,
        OutboundRequest.status.in_(['pending', 'queue_unknown', 'queued']),
    ).update({
        OutboundRequest.status: 'processing', OutboundRequest.updated_at: now,
        OutboundRequest.next_enqueue_at: now + timedelta(minutes=2),
    }, synchronize_session=False)
    db.session.commit()
    db.session.refresh(request)
    return bool(changed)


def start_request_execution(request: OutboundRequest) -> bool:
    """A delayed worker cannot move a concurrently completed request backwards."""
    now = _now()
    changed = OutboundRequest.query.filter(
        OutboundRequest.id == request.id, ~OutboundRequest.status.in_(TERMINAL_STATES),
    ).update({
        OutboundRequest.status: 'processing', OutboundRequest.updated_at: now,
        OutboundRequest.next_enqueue_at: now + timedelta(minutes=2),
    }, synchronize_session=False)
    db.session.commit()
    db.session.refresh(request)
    return bool(changed)


def enqueue_outbound_request(request: OutboundRequest, *, queue=None) -> bool:
    """An acknowledgement loss leaves durable pending work, never a new log."""
    now = _now()
    lease = uuid4().hex
    changed = OutboundRequest.query.filter(
        OutboundRequest.id == request.id,
        ~OutboundRequest.status.in_(TERMINAL_STATES),
        or_(OutboundRequest.enqueue_lease_until.is_(None), OutboundRequest.enqueue_lease_until <= now),
    ).update({
        OutboundRequest.enqueue_lease: lease,
        OutboundRequest.enqueue_lease_until: now + timedelta(seconds=60),
        OutboundRequest.enqueue_attempts: OutboundRequest.enqueue_attempts + 1,
    }, synchronize_session=False)
    db.session.commit()
    if not changed:
        return False
    db.session.refresh(request)
    enqueue_started = False
    try:
        queue = get_queue() if queue is None else queue
        ping = getattr(getattr(queue, 'connection', None), 'ping', None)
        if callable(ping):
            ping()
        from rq import Retry
        payload = json.loads(request.payload_json)
        kwargs = dict(job_id=request_job_id(request), job_timeout=BLAST_JOB_TIMEOUT_SECONDS)
        enqueue_started = True
        if request.kind == 'blast':
            if not request.message_log_id:
                raise ValueError('The saved send log is unavailable; operator review is required.')
            queue.enqueue(
                'app.tasks.send_bulk_job', request.message_log_id, request.organization_id,
                payload['recipients'], payload['message_body'],
                retry=Retry(max=3, interval=[30, 120, 300]), **kwargs,
            )
        else:
            queue.enqueue('app.tasks.send_reply_request_job', request.organization_id, request.id, **kwargs)
    except Exception as exc:
        # The queue may have accepted the job. Preserve identity and payload.
        db.session.rollback()
        OutboundRequest.query.filter(
            OutboundRequest.id == request.id, OutboundRequest.enqueue_lease == lease,
            OutboundRequest.status.in_(['pending', 'queue_unknown', 'queued']),
        ).update({
            OutboundRequest.status: 'queue_unknown' if enqueue_started else 'pending',
            OutboundRequest.last_error: str(exc)[:2000],
            OutboundRequest.enqueue_lease: None, OutboundRequest.enqueue_lease_until: None,
            OutboundRequest.next_enqueue_at: now + timedelta(seconds=30),
        }, synchronize_session=False)
        db.session.commit()
        db.session.refresh(request)
        return False
    OutboundRequest.query.filter(
        OutboundRequest.id == request.id, OutboundRequest.enqueue_lease == lease,
        OutboundRequest.status.in_(['pending', 'queue_unknown', 'queued']),
    ).update({
        OutboundRequest.status: 'queued', OutboundRequest.last_error: None,
        OutboundRequest.enqueue_lease: None, OutboundRequest.enqueue_lease_until: None,
        OutboundRequest.next_enqueue_at: now + timedelta(minutes=2),
    }, synchronize_session=False)
    db.session.commit()
    db.session.refresh(request)
    return True


def request_status_message(request: OutboundRequest) -> str:
    if request.status == 'ambiguous':
        return (f'Request #{request.id}: provider acceptance is unresolved. Automatic resend is blocked. '
                'Contact platform support to reconcile this request before starting a replacement.')
    if request.status in {'pending', 'queue_unknown'}:
        return (f'Request #{request.id} is saved. Queue confirmation is pending; recovery will reuse this request. '
                'Do not start a replacement send.')
    if request.status in {'queued', 'processing'}:
        return f'Request #{request.id} is saved and submission is in progress.'
    if request.status == 'accepted':
        return f'Request #{request.id} was submitted to the provider. This is not a delivery receipt.'
    return f'Request #{request.id} did not complete. Review its recorded outcome before starting a new send.'


def reconcile_outbound_requests(*, limit: int = 50) -> dict[str, int]:
    """Explicit timer/CLI path. Provider calls remain in RQ, never in a thread."""
    now = _now()
    with without_tenant_scope():
        candidates = db.session.query(OutboundRequest.id, OutboundRequest.organization_id).filter(
            ~OutboundRequest.status.in_(TERMINAL_STATES),
            or_(OutboundRequest.next_enqueue_at.is_(None), OutboundRequest.next_enqueue_at <= now),
        ).order_by(OutboundRequest.id).limit(max(1, min(limit, 500))).all()
    seen = queued = 0
    for request_id, organization_id in candidates:
        with organization_context(organization_id):
            request = OutboundRequest.query.filter_by(id=request_id).first()
            if request is None or request.status in TERMINAL_STATES:
                continue
            seen += 1
            try:
                queue = get_queue()
                job = queue.fetch_job(request_job_id(request))
                if job is not None and job.get_status(refresh=True) in ACTIVE_QUEUE_STATES:
                    request.next_enqueue_at = now + timedelta(minutes=2)
                    db.session.commit()
                    continue
                if enqueue_outbound_request(request, queue=queue):
                    queued += 1
            except Exception:
                db.session.rollback()
                # Remains due for the next explicit timer; never abandon intent.
                from flask import current_app
                current_app.logger.exception('Outbound recovery unavailable request_id=%s', request_id)
    return {'requests_seen': seen, 'requests_queued': queued}

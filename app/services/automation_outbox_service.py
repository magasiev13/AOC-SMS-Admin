"""New automatic replies commit with inbound state and recover by the same key."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import timedelta
from uuid import uuid4

from flask import current_app
from sqlalchemy import or_

from app import db
from app.models import (
    AutomationReplyOutbox, InboxMessage, InboxThread, MessageDispatchAttempt,
    Organization, OrganizationMessagingProfile, SurveySession, utc_now,
)
from app.services.billing_service import organization_transmit_block_reason
from app.services.twilio_service import get_twilio_service, record_usage_candidates
from app.tenant import organization_context, without_tenant_scope
from app.utils import normalize_phone, normalize_sms_body


TERMINAL = {'accepted', 'failed', 'cancelled', 'expired'}
ACTIVE = {'pending', 'queued', 'processing'}


def _now():
    return utc_now().replace(tzinfo=None)


def get_queue():
    from app.queue import get_queue as configured_queue
    return configured_queue()


def _provider_account(organization_id):
    profile = OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).populate_existing().first()
    if profile is None:
        return None
    return profile.twilio_subaccount_sid if profile.provider_mode == 'platform_managed' else profile.twilio_account_sid


def save_automation_replies(inbound, thread, replies, *, survey_session=None, question_delay=0):
    """No commit/enqueue: these rows belong to the inbound/survey transaction."""
    db.session.flush()
    now = _now()
    age = max(1, int(current_app.config.get('AUTOMATION_REPLY_MAX_AGE_MINUTES', 30)))
    survey_count, rows = 0, []
    for ordinal, reply in enumerate(replies):
        source, source_id = str(reply['source']), reply.get('source_id')
        source_id = source_id if isinstance(source_id, int) else None
        body = normalize_sms_body(str(reply.get('body') or '').strip())
        key = f'automation:{inbound.id}:{ordinal}:{source}:{source_id or 0}'
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        existing = AutomationReplyOutbox.query.filter_by(organization_id=thread.organization_id, logical_send_key=key).first()
        if existing is not None:
            if existing.phone != thread.phone or existing.body_digest != digest:
                raise ValueError('An automatic reply key already belongs to another immutable response.')
            rows.append(existing)
            continue
        delay = max(0.0, float(question_delay)) if source == 'survey' and survey_count else 0.0
        if source == 'survey':
            survey_count += 1
        row = AutomationReplyOutbox(
            organization_id=thread.organization_id, inbound_message_id=inbound.id, inbound_message_sid=inbound.message_sid, thread_id=thread.id,
            survey_session_id=survey_session.id if survey_session is not None and source == 'survey' else None,
            survey_session_state=survey_session.status if survey_session is not None and source == 'survey' else None,
            phone=thread.phone, body=body, body_digest=digest, provider_account_sid=_provider_account(thread.organization_id),
            logical_send_key=key, ordinal=ordinal, source=source, source_id=source_id,
            delay_after_previous_seconds=delay, ready_at=now, next_enqueue_at=now,
            expires_at=now + timedelta(minutes=age), status='pending', created_at=now, updated_at=now,
        )
        db.session.add(row)
        rows.append(row)
    db.session.flush()
    return rows


def session_has_unsubmitted_reply(session_id):
    return AutomationReplyOutbox.query.filter(
        AutomationReplyOutbox.survey_session_id == session_id,
        AutomationReplyOutbox.status.in_(ACTIVE | {'ambiguous'}),
    ).first() is not None


def _lock_row(organization_id, outbox_id):
    snapshot = AutomationReplyOutbox.query.filter_by(id=outbox_id, organization_id=organization_id).first()
    if snapshot is None:
        return None, None
    thread = InboxThread.query.filter_by(
        id=snapshot.thread_id, organization_id=organization_id,
    ).populate_existing().with_for_update().first() if snapshot.thread_id else None
    row = AutomationReplyOutbox.query.filter_by(
        id=outbox_id, organization_id=organization_id,
    ).populate_existing().with_for_update().first()
    return row, thread


def _attempt(row):
    if hashlib.sha256(row.body.encode('utf-8')).hexdigest() != row.body_digest:
        raise ValueError('The saved automatic reply body no longer matches its immutable digest.')
    attempt = MessageDispatchAttempt.query.filter_by(
        organization_id=row.organization_id, logical_send_key=row.logical_send_key,
    ).populate_existing().with_for_update().first()
    if attempt is not None and (
        attempt.recipient_phone != row.phone or attempt.message_body_digest != row.body_digest
        or attempt.send_kind != 'automation_reply' or attempt.scheduled_message_id is not None or attempt.message_log_id is not None
    ):
        raise ValueError('The durable automatic reply attempt does not match its saved response.')
    return attempt


def _known_result(row):
    attempt = _attempt(row)
    if attempt is None or attempt.status == 'queued':
        return None
    return {
        'success': attempt.status == 'sent', 'sid': attempt.provider_message_sid,
        # Attempt sent establishes API acceptance, not a carrier receipt.
        'status': 'accepted' if attempt.status == 'sent' else attempt.status,
        'error': attempt.last_error, 'account_sid': row.provider_account_sid,
        'logical_send_key': row.logical_send_key, 'idempotent_replay': True,
    }


def _cancel_bound_session(row):
    if row.survey_session_id and row.survey_session_state == 'active':
        session = SurveySession.query.filter_by(id=row.survey_session_id, organization_id=row.organization_id).first()
        if session is not None and session.status == 'active':
            session.status, session.completed_at, session.last_activity_at = 'cancelled', _now(), _now()


def _finish(row, thread, result, *, status=None):
    if row.status in TERMINAL:
        db.session.commit()
        return json.loads(row.result_json or '{}')
    status = status or ('accepted' if result.get('success') else ('ambiguous' if result.get('status') in {'sending', 'ambiguous'} else 'failed'))
    message = InboxMessage.query.filter_by(
        id=row.inbox_message_id, organization_id=row.organization_id,
    ).first() if row.inbox_message_id else None
    if message is None and row.completed_at is None and thread is not None:
        from app.services.inbox_service import DuplicateMessageSidError, _append_inbox_message
        try:
            message = _append_inbox_message(
                thread, row.phone, 'outbound', row.body, message_sid=result.get('sid'),
                automation_source=row.source, automation_source_id=row.source_id,
                delivery_status=result.get('status'), delivery_error=result.get('error'),
                raw_payload={'automation_outbox_id':row.id}, duplicate_sid_mode='reject',
            )
        except DuplicateMessageSidError as exc:
            candidate = exc.existing_message
            if candidate.organization_id != row.organization_id or candidate.thread_id != row.thread_id or candidate.body != row.body:
                raise ValueError('The provider SID already belongs to another conversation message.')
            message = candidate
        db.session.flush()
        row.inbox_message_id = message.id
    elif message is not None:
        message.message_sid, message.delivery_status, message.delivery_error = result.get('sid'), result.get('status'), result.get('error')
    result = dict(result, automation_outbox_id=row.id, inbox_message_id=row.inbox_message_id)
    row.result_json, row.status = json.dumps(result, sort_keys=True), status
    row.last_error, row.completed_at = str(result.get('error') or '')[:2000] or None, _now()
    row.worker_lease, row.worker_lease_until = None, None
    row.enqueue_lease, row.enqueue_lease_until, row.next_enqueue_at = None, None, None
    if status in {'failed', 'cancelled', 'expired', 'ambiguous'}:
        _cancel_bound_session(row)
    if result.get('sid'):
        # Commit history, terminal outbox result and the usage ledger together.
        # If persistence fails, the independently committed provider attempt is
        # still replayable and cannot cause another submission.
        record_usage_candidates(row.organization_id, [result], source=row.source, commit=False)
    db.session.commit()
    return result


def _not_submitted(row, thread, reason, status):
    return _finish(row, thread, {'success':False,'sid':None,'status':status,'error':reason,'not_submitted':True},status=status)


def _submission_gate(row, thread, now):
    if thread is None or thread.id != row.thread_id or normalize_phone(thread.phone) != row.phone:
        return 'conversation_unavailable', 'failed'
    if not row.body or hashlib.sha256(row.body.encode('utf-8')).hexdigest() != row.body_digest:
        return 'automatic_reply_payload_invalid', 'failed'
    if row.expires_at <= now:
        return 'Automatic reply expired before submission; no message was sent.', 'expired'
    if not current_app.config.get('INBOUND_AUTO_REPLY_ENABLED',True):
        return 'auto_reply_disabled', 'failed'
    if row.survey_session_state == 'active':
        # Session deletion clears its FK, but the saved active-survey context
        # still requires a live session before this question may be submitted.
        session = SurveySession.query.filter_by(id=row.survey_session_id,organization_id=row.organization_id).first() if row.survey_session_id is not None else None
        if session is None or session.status != 'active':
            return 'The original survey session ended before this reply was submitted.', 'cancelled'
    from app.services.twilio_service import _consent_send_block_reason
    consent_reason = _consent_send_block_reason(row.organization_id,row.phone,send_kind='automation_reply')
    if consent_reason:
        return consent_reason, 'cancelled'
    if not row.provider_account_sid or _provider_account(row.organization_id) != row.provider_account_sid:
        return 'provider_identity_changed', 'failed'
    organization = db.session.get(Organization,row.organization_id)
    reason = organization_transmit_block_reason(organization)
    return (reason,'deferred') if reason else (None,None)


def lock_automation_submission(organization_id, outbox_id, *, phone, body_digest):
    """After attempt claim commit, hold conversation lock through provider HTTP."""
    row, thread = _lock_row(organization_id,outbox_id)
    if row is None or row.phone != phone or row.body_digest != body_digest:
        return {'success':False,'sid':None,'status':'cancelled','error':'Saved automatic reply is unavailable.','not_submitted':True}
    if row.status in TERMINAL or row.status == 'ambiguous':
        return {'success':False,'sid':None,'status':'cancelled','error':'Automatic reply is no longer dispatchable.','not_submitted':True}
    reason, status = _submission_gate(row,thread,_now())
    if reason:
        return {'success':False,'sid':None,'status':status,'error':reason,'not_submitted':True,'deferred':status=='deferred'}
    return None


def dispatch_automation_reply(organization_id,outbox_id):
    """One guarded dispatch or history-only replay; use from an RQ job."""
    if not current_app.config.get('SAAS_MODE'):
        raise RuntimeError('Automatic reply outbox dispatch requires SAAS_MODE=1.')
    with organization_context(organization_id):
        row, thread = _lock_row(organization_id,outbox_id)
        if row is None:
            db.session.commit()
            return {'status':'missing'}
        if row.status in TERMINAL:
            db.session.commit()
            return json.loads(row.result_json or '{}')
        known = _known_result(row)
        now = _now()
        if known is not None and known.get('status') in {'sending','ambiguous'}:
            if row.status == 'ambiguous':
                db.session.commit()
                return json.loads(row.result_json or '{}')
            if row.worker_lease_until and row.worker_lease_until > now:
                db.session.commit()
                return {'status':'busy'}
        if known is not None:
            return _finish(row,thread,known)
        if row.worker_lease_until and row.worker_lease_until > now:
            db.session.commit()
            return {'status':'busy'}
        prior = AutomationReplyOutbox.query.filter(
            AutomationReplyOutbox.organization_id==organization_id,AutomationReplyOutbox.thread_id==row.thread_id,
            AutomationReplyOutbox.id<row.id,AutomationReplyOutbox.status.in_(ACTIVE),
        ).order_by(AutomationReplyOutbox.id).first()
        if prior is not None:
            row.status,row.next_enqueue_at='pending',now+timedelta(seconds=30)
            db.session.commit()
            return {'status':'waiting_for_previous'}
        sequence_prefix = ':'.join(row.logical_send_key.split(':',2)[:2]) + ':'
        previous = AutomationReplyOutbox.query.filter(
            AutomationReplyOutbox.organization_id==organization_id,AutomationReplyOutbox.logical_send_key.startswith(sequence_prefix),
            AutomationReplyOutbox.ordinal<row.ordinal,
        ).order_by(AutomationReplyOutbox.ordinal.desc()).first()
        if previous is not None:
            if previous.status!='accepted':
                return _not_submitted(row,thread,'An earlier response in this sequence was not accepted; this follow-up was not submitted.','cancelled')
            ready_at=max(row.ready_at,previous.completed_at+timedelta(seconds=row.delay_after_previous_seconds or 0))
        else:
            ready_at=row.ready_at
        if ready_at>now:
            row.ready_at,row.next_enqueue_at,row.status=ready_at,ready_at,'pending'
            db.session.commit()
            return {'status':'waiting','wait_seconds':(ready_at-now).total_seconds()}
        reason,status=_submission_gate(row,thread,now)
        if reason:
            if status=='deferred':
                row.status,row.next_enqueue_at,row.last_error='pending',now+timedelta(seconds=30),reason
                row.ready_at=row.next_enqueue_at
                db.session.commit()
                return {'status':'deferred','error':reason}
            return _not_submitted(row,thread,reason,status)
        token=uuid4().hex
        row.worker_lease,row.worker_lease_until=token,now+timedelta(seconds=max(60,int(current_app.config.get('AUTOMATION_REPLY_LEASE_SECONDS',120))))
        row.status,row.attempt_count='processing',int(row.attempt_count or 0)+1
        row.next_enqueue_at=row.worker_lease_until
        phone,body,key=row.phone,row.body,row.logical_send_key
        db.session.commit()
        try:
            provider=get_twilio_service(organization_id)
        except Exception as exc:
            row,thread=_lock_row(organization_id,outbox_id)
            row.status,row.worker_lease,row.worker_lease_until='pending',None,None
            row.next_enqueue_at,row.last_error=_now()+timedelta(seconds=30),str(exc)[:2000]
            db.session.commit()
            return {'status':'deferred'}
        result=provider.send_message_with_attempt(phone,body,key,None,None,'automation_reply',automation_reply_id=outbox_id)
        row,thread=_lock_row(organization_id,outbox_id)
        if row is None:
            db.session.commit()
            return {'status':'missing'}
        if row.status in TERMINAL:
            db.session.commit()
            return json.loads(row.result_json or '{}')
        known=_known_result(row)
        if known is not None:
            # Fresh committed attempts outrank a delayed worker's local result.
            keep_not_submitted = known.get('status') == 'failed' and result.get('status') in {'cancelled','expired'} and result.get('not_submitted')
            if not keep_not_submitted and (known.get('status') not in {'sending','ambiguous'} or result.get('status') in {'sending','ambiguous'}):
                result=known
        if result.get('retryable') or result.get('deferred'):
            row.status,row.worker_lease,row.worker_lease_until='pending',None,None
            row.next_enqueue_at=_now()+timedelta(seconds=min(300,30*(2**min(3,int(row.attempt_count or 1)-1))))
            row.ready_at=row.next_enqueue_at
            row.last_error=str(result.get('error') or '')[:2000]
            db.session.commit()
            return result
        return _finish(row,thread,result,status=result.get('status') if result.get('status') in {'cancelled','expired'} else None)


def enqueue_automation_reply(row, *, queue=None):
    now,lease=_now(),uuid4().hex
    changed=AutomationReplyOutbox.query.filter(
        AutomationReplyOutbox.id==row.id,AutomationReplyOutbox.organization_id==row.organization_id,
        AutomationReplyOutbox.status.in_(ACTIVE),
        or_(AutomationReplyOutbox.next_enqueue_at.is_(None),AutomationReplyOutbox.next_enqueue_at<=now),
        or_(AutomationReplyOutbox.enqueue_lease_until.is_(None),AutomationReplyOutbox.enqueue_lease_until<=now),
    ).update({AutomationReplyOutbox.enqueue_lease:lease,AutomationReplyOutbox.enqueue_lease_until:now+timedelta(seconds=60)},synchronize_session=False)
    db.session.commit()
    if not changed:
        return False
    try:
        queue=get_queue() if queue is None else queue
        queue.enqueue('app.services.automation_outbox_service.deliver_automation_job',row.organization_id,row.id,
                      job_id=f'twinevia-automation-{row.organization_id}-{row.id}',job_timeout=1800)
    except Exception as exc:
        db.session.rollback()
        AutomationReplyOutbox.query.filter_by(id=row.id,enqueue_lease=lease).update({
            AutomationReplyOutbox.enqueue_lease:None,AutomationReplyOutbox.enqueue_lease_until:None,
            AutomationReplyOutbox.next_enqueue_at:now+timedelta(seconds=30),AutomationReplyOutbox.last_error:str(exc)[:2000],
        },synchronize_session=False)
        db.session.commit()
        return False
    AutomationReplyOutbox.query.filter(
        AutomationReplyOutbox.id==row.id,AutomationReplyOutbox.enqueue_lease==lease,
        AutomationReplyOutbox.status.in_(['pending','queued']),
    ).update({AutomationReplyOutbox.status:'queued',AutomationReplyOutbox.enqueue_lease:None,
              AutomationReplyOutbox.enqueue_lease_until:None,AutomationReplyOutbox.next_enqueue_at:now+timedelta(minutes=2)},synchronize_session=False)
    db.session.commit()
    return True


def enqueue_for_inbound(organization_id,inbound_id):
    with organization_context(organization_id):
        rows=AutomationReplyOutbox.query.filter_by(organization_id=organization_id,inbound_message_id=inbound_id).order_by(AutomationReplyOutbox.ordinal).all()
        queued=0
        for row in rows:
            if row.status in ACTIVE:
                queued+=int(enqueue_automation_reply(row))
        return {'saved_replies':len(rows),'queued_replies':queued}


def resume_saved_inbound_sid(organization_id,message_sid):
    """Retained intent still deduplicates a SID after inbox history is deleted."""
    rows=AutomationReplyOutbox.query.filter_by(
        organization_id=organization_id,inbound_message_sid=message_sid,
    ).order_by(AutomationReplyOutbox.ordinal).all()
    if not rows:
        return None
    thread_id=rows[0].thread_id
    queued=0
    for row in rows:
        if row.status in ACTIVE:
            queued+=int(enqueue_automation_reply(row))
    return {'status':'duplicate','thread_id':thread_id,'automation_outbox':{'saved_replies':len(rows),'queued_replies':queued}}


def deliver_automation_job(organization_id,outbox_id):
    from app import create_app

    app=create_app(run_startup_tasks=False,start_scheduler=False)
    with app.app_context(),organization_context(organization_id):
        current_id=outbox_id
        for _ in range(20):
            result=dispatch_automation_reply(organization_id,current_id)
            if result.get('status')=='waiting' and 0<result.get('wait_seconds',0)<=30:
                time.sleep(result['wait_seconds'])
                continue
            row=AutomationReplyOutbox.query.filter_by(id=current_id,organization_id=organization_id).first()
            if row is None or row.status not in TERMINAL:
                return result
            next_row=AutomationReplyOutbox.query.filter(
                AutomationReplyOutbox.organization_id==organization_id,AutomationReplyOutbox.thread_id==row.thread_id,
                AutomationReplyOutbox.id>row.id,AutomationReplyOutbox.status.in_(ACTIVE),
            ).order_by(AutomationReplyOutbox.id).first()
            if next_row is None:
                return result
            current_id=next_row.id
        return {'status':'bounded_job_complete'}


def recover_automation_replies(*,limit=100):
    """Explicit oneshot/RQ path: only this new table, no historical backfill."""
    if not current_app.config.get('SAAS_MODE'):
        raise RuntimeError('Automatic reply outbox recovery requires SAAS_MODE=1.')
    now=_now()
    summary={'seen':0,'queued':0,'finalized':0}
    with without_tenant_scope():
        resolved_attempt=db.session.query(MessageDispatchAttempt.id).filter(
            MessageDispatchAttempt.organization_id==AutomationReplyOutbox.organization_id,
            MessageDispatchAttempt.logical_send_key==AutomationReplyOutbox.logical_send_key,
            MessageDispatchAttempt.status.in_(['sent','failed']),
        ).exists()
        candidates=db.session.query(AutomationReplyOutbox.id,AutomationReplyOutbox.organization_id).filter(
            or_(AutomationReplyOutbox.status.in_(ACTIVE),
                db.and_(AutomationReplyOutbox.status=='ambiguous',resolved_attempt)),
            or_(AutomationReplyOutbox.next_enqueue_at.is_(None),AutomationReplyOutbox.next_enqueue_at<=now),
        ).order_by(AutomationReplyOutbox.id).limit(max(1,min(500,limit))).all()
    for outbox_id,organization_id in candidates:
        with organization_context(organization_id):
            row=AutomationReplyOutbox.query.filter_by(id=outbox_id,organization_id=organization_id).first()
            if row is None:
                continue
            summary['seen']+=1
            known=_known_result(row)
            db.session.commit()
            if known is not None or row.expires_at<=now:
                dispatch_automation_reply(organization_id,outbox_id)
                summary['finalized']+=1
                continue
            if row.status=='ambiguous':
                continue
            try:
                queue=get_queue()
                job=queue.fetch_job(f'twinevia-automation-{organization_id}-{outbox_id}')
                if job is not None and job.get_status(refresh=True) in {'queued','started','deferred','scheduled'}:
                    continue
                summary['queued']+=int(enqueue_automation_reply(row,queue=queue))
            except Exception:
                db.session.rollback()
                current_app.logger.exception('Automatic reply recovery queue unavailable outbox_id=%s',outbox_id)
    return summary

"""Explicit invitation approval, late roster resolution and immutable execution snapshots.

Browser reviews expire after fifteen minutes. Persistent invitation approval is a
separate row and is never reconstructed from a browser token by the scheduler.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy import func

from app import db
from app.models import (CommunityMember, EventRegistration, FeedbackInvitation,
                        MessageDispatchAttempt, Organization, ScheduledMessage, utc_now)
from app.services.send_review_service import SendReviewInputError
from app.tenant import organization_context
from app.utils import normalize_sms_body, find_invalid_template_tokens

SOURCE = 'feedback_invitation'


def _now():
    return utc_now().replace(tzinfo=None)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def form_version(flow):
    return _digest([flow.id, flow.purpose, flow.feedback_event_id, flow.name,
                    flow.trigger_keyword, flow.intro_message, flow.questions,
                    flow.completion_message, bool(flow.is_active)])


def context_digest(event, flow, schedule):
    return _digest([event.id, event.title, event.date, event.external_start_at,
                    event.external_end_at, event.external_timezone, event.external_status,
                    form_version(flow), schedule.message_body, schedule.scheduled_at,
                    schedule.target, schedule.event_id, bool(schedule.test_mode)])


def invitation_intent(event, flow, values):
    """Normalize only explicit operator choices; no default invitation time."""
    audience = values.get('audience_rule', '')
    if audience not in {'registered_contacts', 'community'}:
        raise SendReviewInputError('Choose registered contacts or contactable community.')
    timing = values.get('timing', '')
    if timing not in {'now', 'later'}:
        raise SendReviewInputError('Choose Send now or an explicit date and time.')
    body = normalize_sms_body(str(values.get('message_body') or '').strip())
    if not body or len(body) > 1600:
        raise SendReviewInputError('Write an invitation of at most 1,600 characters.')
    if find_invalid_template_tokens(body):
        raise SendReviewInputError('Correct unsupported personalization tokens before reviewing.')
    if not flow.is_active:
        raise SendReviewInputError('Activate this feedback form before preparing an invitation.')
    if not flow.is_event_feedback or flow.feedback_event_id != event.id or flow.organization_id != event.organization_id:
        raise SendReviewInputError('This feedback form does not belong to this event.')
    zone_name = str(values.get('timezone') or '').strip()
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise SendReviewInputError('Choose a valid timezone, such as America/Denver or UTC.') from exc
    day = str(values.get('schedule_date') or '').strip() if timing == 'later' else ''
    time = str(values.get('schedule_time') or '').strip() if timing == 'later' else ''
    if timing == 'later':
        try:
            naive = datetime.strptime(f'{day} {time}', '%Y-%m-%d %H:%M')
            local = naive.replace(tzinfo=zone)
            if (local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive
                    or local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()):
                raise ValueError('ambiguous or nonexistent local time')
        except ValueError as exc:
            raise SendReviewInputError('Choose an unambiguous valid local date and time; use UTC near a daylight saving change.') from exc
        if local.astimezone(timezone.utc) <= utc_now():
            raise SendReviewInputError('Scheduled time must be in the future.')
    return {'event_id':event.id, 'survey_id':flow.id, 'form_version':form_version(flow),
            'audience_rule':audience, 'timing':timing, 'timezone':zone_name,
            'schedule_date':day, 'schedule_time':time, 'message_body':body}


def roster_hold_reason(event):
    from app.services.booking_projection_service import roster_summary
    if not roster_summary(event).get('coverage_verified'):
        return ('The event roster is not verified. Complete an event-specific source reconciliation and compare '
                'its coverage before reviewing this registrant invitation. Community requires a separate explicit review.')
    return None


def resolve_recipients(event, audience_rule):
    from app.services.audience_service import community_query, event_registrations_query
    from app.services.recipient_service import dedupe_recipients_by_phone
    from app.services.recipient_permission_service import filter_permission_recipients
    limit = int(current_app.config.get('SEND_MAX_RECIPIENTS', 5000) or 5000)
    if audience_rule == 'registered_contacts':
        query = event_registrations_query(current_only=False, organization_id=event.organization_id).filter_by(event_id=event.id)
    else:
        query = community_query(organization_id=event.organization_id)
    records = query.order_by(query.column_descriptions[0]['entity'].id).limit(limit+1).all()
    if len(records) > limit:
        raise SendReviewInputError('The invitation audience exceeds the configured recipient limit.')
    rows = [{'phone':r.phone, 'name':r.name} for r in records]
    rows, skipped, _ = filter_permission_recipients(rows, organization_id=event.organization_id,
        purpose='event' if audience_rule == 'registered_contacts' else 'community',
        event_id=event.id if audience_rule == 'registered_contacts' else None)
    rows, duplicates, _ = dedupe_recipients_by_phone(rows)
    return rows, len(skipped) + len(duplicates)


def build_review(event, flow, values, actor, approval_key):
    from app.services.billing_service import organization_transmit_block_reason
    from app.services.send_review_service import freeze_review
    from app.utils import analyze_personalized_sms_blast, render_message_template
    intent = invitation_intent(event, flow, values)
    reason = organization_transmit_block_reason(db.session.get(Organization, event.organization_id))
    if reason:
        raise SendReviewInputError(reason)
    if intent['audience_rule'] == 'registered_contacts':
        reason = roster_hold_reason(event)
        if reason:
            raise SendReviewInputError(reason)
    recipients, excluded = resolve_recipients(event, intent['audience_rule'])
    if not recipients and intent['timing'] == 'now':
        raise SendReviewInputError('No eligible contacts are available for this invitation yet.')
    estimate = analyze_personalized_sms_blast(intent['message_body'], recipients)
    from app.routes import _blast_limit_error
    limit_error = _blast_limit_error(estimate)
    if limit_error:
        raise SendReviewInputError(limit_error)
    profile = event.organization.messaging_profile
    timing = 'Send now' if intent['timing'] == 'now' else f"{intent['schedule_date']} {intent['schedule_time']} ({intent['timezone']})"
    return {'review_token':freeze_review(actor, event.organization_id, approval_key, intent, recipients),
            'intent':intent, 'late_bound':True, 'audience':'Registered contacts' if intent['audience_rule']=='registered_contacts' else 'Contactable community',
            'eligible':len(recipients), 'excluded':excluded, 'sender':profile.from_number if profile else 'Configured messaging service',
            'segments':estimate['total_segments'], 'sample':render_message_template(intent['message_body'], recipients[0] if recipients else {'name':''}),
            'timing':timing, 'confirmation_label':'Confirm invitation' if intent['timing']=='now' else 'Confirm invitation schedule',
            'audience_policy':'Eligible contacts are resolved when this invitation becomes due. Its recipient snapshot then freezes for all retries. Current opt-outs and cancellations are checked before each submission.'}


def approve_invitation(event, flow, values, actor, approval_key, token):
    from app.services.send_review_service import reviewed_recipients
    intent = invitation_intent(event, flow, values)
    if not approval_key or len(approval_key)>64 or not all(c.isalnum() or c in '._:-' for c in approval_key):
        raise SendReviewInputError('Reload the invitation form before reviewing again.')
    # Validates session identity and exact form/version/content/audience/timing.
    reviewed_recipients(token, actor, event.organization_id, approval_key, intent, allow_empty=True)
    Organization.query.filter_by(id=event.organization_id).with_for_update().one()
    existing = FeedbackInvitation.query.filter_by(organization_id=event.organization_id,
        author_user_id=actor.id, approval_key=approval_key).first()
    if existing:
        if existing.approval_digest != _digest(intent):
            raise SendReviewInputError('This approval key already belongs to another invitation.')
        return existing
    if intent['audience_rule']=='registered_contacts':
        reason = roster_hold_reason(event)
        if reason:
            raise SendReviewInputError(reason)
    limit = int(current_app.config.get('SCHEDULED_MAX_PENDING_PER_ORGANIZATION', 25) or 25)
    if ScheduledMessage.query.filter_by(organization_id=event.organization_id).filter(
            ScheduledMessage.status.in_(['pending','processing','held'])).count() >= limit:
        raise SendReviewInputError('The workspace has reached its pending scheduled-message limit.')
    when = _now() if intent['timing']=='now' else datetime.strptime(
        f"{intent['schedule_date']} {intent['schedule_time']}", '%Y-%m-%d %H:%M').replace(
            tzinfo=ZoneInfo(intent['timezone'])).astimezone(timezone.utc).replace(tzinfo=None)
    schedule_identity = _digest([event.organization_id, actor.id, approval_key])
    schedule = ScheduledMessage(organization_id=event.organization_id, scheduled_at=when,
        message_body=intent['message_body'], target='event' if intent['audience_rule']=='registered_contacts' else 'community',
        event_id=event.id if intent['audience_rule']=='registered_contacts' else None, status='pending',
        test_mode=False, automation_source=SOURCE, automation_key='feedback:'+schedule_identity,
        automation_kind='feedback_invitation', request_idempotency_key='feedback:'+schedule_identity[:55])
    db.session.add(schedule); db.session.flush()
    invitation = FeedbackInvitation(organization_id=event.organization_id, event_id=event.id, survey_id=flow.id,
        author_user_id=actor.id, approved_by_user_id=actor.id, approval_key=approval_key,
        approved_at=_now(), approval_digest=_digest(intent), message_digest=_digest(intent['message_body']), form_version=form_version(flow),
        audience_rule=intent['audience_rule'], timezone=intent['timezone'], scheduled_message_id=schedule.id,
        context_digest=context_digest(event, flow, schedule), status='approved')
    invitation.approval_history_json = _json([{
        'kind':'initial', 'actor_user_id':actor.id, 'approved_at':invitation.approved_at.isoformat(),
        'form_version':invitation.form_version, 'context_digest':invitation.context_digest,
        'intent_digest':invitation.approval_digest, 'message_digest':invitation.message_digest,
        'audience_rule':invitation.audience_rule, 'timezone':invitation.timezone,
        'scheduled_at':schedule.scheduled_at.isoformat(),
    }])
    db.session.add(invitation); db.session.flush()
    return invitation


def _invitation_for_schedule(schedule, *, lock=False):
    if schedule.automation_source != SOURCE:
        return None
    query = FeedbackInvitation.query.filter_by(organization_id=schedule.organization_id,
        scheduled_message_id=schedule.id).populate_existing()
    return query.with_for_update().first() if lock else query.first()


def hold_invitation(invitation, schedule, reason):
    invitation.status, invitation.hold_reason = 'held', reason
    schedule.status, schedule.error_message = 'held', reason
    schedule.processing_started_at, schedule.next_retry_at = None, None


def validate_authority(invitation, schedule):
    event, flow = invitation.event, invitation.survey
    if event is not None:
        db.session.refresh(event)
    if flow is not None:
        db.session.refresh(flow)
    if event is None or flow is None or not flow.is_active or not flow.is_event_feedback or flow.feedback_event_id != event.id:
        return 'The feedback form was paused or its event context changed. Review this invitation again.'
    if invitation.context_digest != context_digest(event, flow, schedule):
        return 'Event timing, invitation content or feedback settings changed after approval. Review this invitation again.'
    if invitation.audience_rule=='registered_contacts':
        return roster_hold_reason(event)
    return None


def prepare_due_invitation(schedule):
    """Caller holds schedule lock. Freeze once before the first execution claim."""
    invitation = _invitation_for_schedule(schedule, lock=True)
    if schedule.automation_source != SOURCE:
        return True
    if invitation is None:
        schedule.status, schedule.error_message = 'held', 'The durable feedback invitation approval is missing. Review required.'
        return False
    reason = validate_authority(invitation, schedule)
    if reason:
        hold_invitation(invitation, schedule, reason); return False
    if invitation.snapshot_frozen_at is None:
        try:
            rows, _ = resolve_recipients(invitation.event, invitation.audience_rule)
        except SendReviewInputError as exc:
            # A legitimate late signup can put this explicitly selected audience
            # over a configured limit. Hold only this invitation for review.
            hold_invitation(invitation, schedule, str(exc))
            return False
        if not rows:
            hold_invitation(invitation, schedule, 'No eligible contacts remain. Review the event audience before resuming.')
            return False
        # Late contacts and personalized Unicode names can change the segment
        # estimate since approval, even when the recipient count still fits.
        # Check the exact due audience before freezing execution authority.
        from app.routes import _blast_limit_error
        from app.utils import analyze_personalized_sms_blast
        limit_error = _blast_limit_error(analyze_personalized_sms_blast(schedule.message_body, rows))
        if limit_error:
            hold_invitation(invitation, schedule, limit_error)
            return False
        snapshot = _json(rows)
        max_bytes = int(current_app.config.get('RECIPIENT_SNAPSHOT_MAX_BYTES', 1024*1024) or 1024*1024)
        if len(snapshot.encode()) > max_bytes:
            hold_invitation(invitation, schedule, 'The recipient snapshot exceeds the configured size limit. Review the audience.')
            return False
        schedule.test_recipient_snapshot_json = snapshot
        invitation.snapshot_frozen_at, invitation.recipient_count = _now(), len(rows)
        invitation.snapshot_digest, invitation.status = _digest(rows), 'frozen'
    else:
        try:
            rows = json.loads(schedule.test_recipient_snapshot_json or '')
        except ValueError:
            rows = None
        if not isinstance(rows, list) or invitation.snapshot_digest != _digest(rows):
            hold_invitation(invitation, schedule, 'The frozen invitation audience changed. Restore its original snapshot before review.')
            return False
    return True


def submission_guard(schedule, phone, body=None):
    """Run under the schedule lock immediately before each provider submission."""
    invitation = _invitation_for_schedule(schedule, lock=True)
    if schedule.automation_source != SOURCE:
        return None
    if invitation is None:
        schedule.status, schedule.error_message = 'held', 'Feedback invitation approval is missing.'
        return {'success':False, 'status':'held', 'error':schedule.error_message, 'dispatch_halted':True}
    reason = validate_authority(invitation, schedule)
    if reason:
        hold_invitation(invitation, schedule, reason)
        return {'success':False, 'status':'held', 'error':reason, 'dispatch_halted':True}
    if invitation.snapshot_frozen_at is None:
        hold_invitation(invitation, schedule, 'The invitation recipient snapshot has not been frozen.')
        return {'success':False, 'status':'held', 'error':invitation.hold_reason, 'dispatch_halted':True}
    try:
        frozen_rows = json.loads(schedule.test_recipient_snapshot_json or '')
    except (TypeError, ValueError):
        frozen_rows = None
    if not isinstance(frozen_rows, list) or invitation.snapshot_digest != _digest(frozen_rows):
        hold_invitation(invitation, schedule, 'The original frozen invitation audience must be restored before resuming.')
        return {'success':False, 'status':'held', 'error':invitation.hold_reason, 'dispatch_halted':True}
    frozen_recipient = next((row for row in frozen_rows if row.get('phone') == phone), None)
    if frozen_recipient is None:
        return {'success':False, 'status':'skipped', 'error':'Recipient is outside the approved frozen invitation audience.'}
    if body is not None:
        from app.utils import render_message_template
        if body != render_message_template(schedule.message_body, frozen_recipient):
            hold_invitation(invitation, schedule, 'Queued invitation content differs from its approved message. Review required.')
            return {'success':False, 'status':'held', 'error':invitation.hold_reason, 'dispatch_halted':True}
    if invitation.audience_rule == 'registered_contacts' and EventRegistration.query.filter_by(
            organization_id=invitation.organization_id, event_id=invitation.event_id, phone=phone).populate_existing().with_for_update().first() is None:
        return {'success':False, 'status':'skipped', 'error':'Registration was cancelled or its destination changed before submission.'}
    return None


def invitation_history(event, flow=None):
    query = FeedbackInvitation.query.filter_by(organization_id=event.organization_id, event_id=event.id)
    if flow is not None:
        query = query.filter_by(survey_id=flow.id)
    rows=[]
    for invitation in query.order_by(FeedbackInvitation.id.desc()).limit(50).all():
        schedule=invitation.scheduled_message
        outcomes=dict(db.session.query(MessageDispatchAttempt.status, func.count(MessageDispatchAttempt.id)).filter_by(
            organization_id=event.organization_id, scheduled_message_id=schedule.id).group_by(MessageDispatchAttempt.status).all())
        try:
            local=schedule.scheduled_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(invitation.timezone))
            local_label=local.strftime('%Y-%m-%d %H:%M')
        except (ValueError, ZoneInfoNotFoundError):
            local_label=schedule.scheduled_at.strftime('%Y-%m-%d %H:%M')+' UTC'
        rows.append({'id':invitation.id, 'status':schedule.status, 'audience_rule':invitation.audience_rule,
            'scheduled_at':schedule.scheduled_at, 'scheduled_local':local_label,
            'timezone':invitation.timezone, 'hold_reason':invitation.hold_reason or schedule.error_message,
            'recipient_count':invitation.recipient_count, 'accepted':outcomes.get('sent',0),
            'unresolved':outcomes.get('sending',0)+outcomes.get('ambiguous',0),
            'failed':outcomes.get('failed',0), 'message_log_id':schedule.message_log_id,
            'snapshot_frozen_at':invitation.snapshot_frozen_at})
    return rows


def held_review(event, flow, invitation, actor):
    from app.services.send_review_service import freeze_review
    from app.utils import analyze_personalized_sms_blast, render_message_template
    schedule = invitation.scheduled_message
    if schedule.status != 'held':
        raise SendReviewInputError('This invitation is no longer held. Refresh its status.')
    if not flow.is_active:
        raise SendReviewInputError('Activate the feedback form before reviewing its invitation.')
    if invitation.audience_rule == 'registered_contacts':
        reason = roster_hold_reason(event)
        if reason:
            raise SendReviewInputError(reason)
    # A review after execution began preserves its complete original denominator.
    if invitation.snapshot_frozen_at:
        try:
            rows = json.loads(schedule.test_recipient_snapshot_json)
        except (TypeError, ValueError) as exc:
            raise SendReviewInputError('The original frozen audience must be restored before resuming.') from exc
        if not isinstance(rows, list) or invitation.snapshot_digest != _digest(rows):
            raise SendReviewInputError('The original frozen audience must be restored before resuming.')
    else:
        rows = resolve_recipients(event, invitation.audience_rule)[0]
    if not rows:
        raise SendReviewInputError('No eligible invitation audience is available yet.')
    intent = {'resume_invitation':invitation.id,
              'context_digest':context_digest(event, flow, schedule),
              'snapshot_digest':invitation.snapshot_digest,
              'audience_rule':invitation.audience_rule}
    estimate = analyze_personalized_sms_blast(schedule.message_body, rows)
    return {'review_token':freeze_review(actor, event.organization_id, f'invitation-resume:{invitation.id}', intent, rows),
        'intent':intent, 'late_bound':invitation.snapshot_frozen_at is None,
        'audience':'Registered contacts' if invitation.audience_rule == 'registered_contacts' else 'Contactable community',
        'eligible':len(rows), 'excluded':0, 'sender':'Configured messaging service',
        'segments':estimate['total_segments'], 'timing':'Resume when due after this review',
        'sample':render_message_template(schedule.message_body, rows[0]),
        'confirmation_label':'Confirm reviewed invitation',
        'audience_policy':'The audience rule stays unchanged. Accepted and unresolved provider evidence is retained. An existing frozen snapshot cannot gain recipients.'}


def resume_held(event, flow, invitation, actor, token):
    from app.services.scheduled_cancellation_service import lock_scheduled_message
    from app.services.send_review_service import reviewed_recipients
    schedule = lock_scheduled_message(invitation.scheduled_message_id, event.organization_id)
    invitation = FeedbackInvitation.query.filter_by(id=invitation.id, organization_id=event.organization_id).populate_existing().with_for_update().one()
    review = held_review(event, flow, invitation, actor)
    reviewed_recipients(token, actor, event.organization_id, f'invitation-resume:{invitation.id}', review['intent'])
    if invitation.snapshot_frozen_at:
        try:
            rows=json.loads(schedule.test_recipient_snapshot_json)
        except (TypeError, ValueError) as exc:
            raise SendReviewInputError('The original frozen audience must be restored before resuming.') from exc
        if invitation.snapshot_digest != _digest(rows):
            raise SendReviewInputError('The original frozen audience must be restored before resuming.')
    # Changing content after execution began would create different logical
    # per-recipient keys, so resuming must retain the original approved body.
    if invitation.snapshot_frozen_at and invitation.message_digest != _digest(schedule.message_body):
        raise SendReviewInputError('Invitation content changed after execution began. Restore its original message before resuming.')
    invitation.message_digest = _digest(schedule.message_body)
    try:
        history=json.loads(invitation.approval_history_json or '[]')
        if not isinstance(history, list):
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise SendReviewInputError('The retained approval history must be restored before resuming.') from exc
    invitation.context_digest=context_digest(event, flow, schedule)
    invitation.form_version=form_version(flow)
    invitation.approved_by_user_id, invitation.approved_at=actor.id, _now()
    history.append({'kind':'resume', 'actor_user_id':actor.id, 'approved_at':invitation.approved_at.isoformat(),
        'form_version':invitation.form_version, 'context_digest':invitation.context_digest,
        'intent_digest':_digest(review['intent']), 'message_digest':invitation.message_digest,
        'audience_rule':invitation.audience_rule, 'snapshot_digest':invitation.snapshot_digest,
        'timezone':invitation.timezone, 'scheduled_at':schedule.scheduled_at.isoformat()})
    invitation.approval_history_json=_json(history)
    invitation.status='frozen' if invitation.snapshot_frozen_at else 'approved'
    invitation.hold_reason=None
    schedule.status, schedule.error_message='pending', None
    schedule.processing_started_at, schedule.next_retry_at=None, None
    return invitation

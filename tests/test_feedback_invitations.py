"""Explicit invitation authority, late binding, holds and submission recovery."""
import importlib
import json
import logging
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.test_event_feedback import feedback, create_flow


def contact(f, phone='+17205550145', *, event=True):
    cls=f.m.EventRegistration if event else f.m.CommunityMember
    row=cls(organization_id=f.org.id, phone=phone, name='Fixture guest')
    if event:row.event_id=f.event.id
    f.db.session.add(row); f.db.session.commit()
    return row


def setup(f, *, audience='community', timing='now', message_body=None):
    from app.services import feedback_invitation_service as invitations
    f.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS']=[f.org.slug]
    flow=create_flow(f)
    row=contact(f,event=audience=='registered_contacts')
    later=f.m.utc_now()+timedelta(days=2)
    values={'audience_rule':audience, 'timing':timing, 'timezone':'UTC',
        'schedule_date':later.strftime('%Y-%m-%d'), 'schedule_time':later.strftime('%H:%M'),
        'message_body':message_body if message_body is not None else f'If you joined us, reply {flow.trigger_keyword} to share feedback.'}
    key=uuid4().hex
    with f.organization_context(f.org.id):
        with patch.object(invitations,'roster_hold_reason',return_value=None):
            review=invitations.build_review(f.event,flow,values,f.owner,key)
            invitation=invitations.approve_invitation(f.event,flow,values,f.owner,key,review['review_token'])
            f.db.session.commit()
    return invitations,flow,row,values,key,review,invitation


def test_form_creation_and_review_create_no_invitation_or_schedule(feedback):
    f=feedback;flow=create_flow(f)
    assert f.m.FeedbackInvitation.query.count()==0
    assert f.m.ScheduledMessage.query.count()==0
    page=f.staff_client.get(f'/events/{f.event.id}/feedback/{flow.id}/invite')
    assert page.status_code==200
    assert b'name="timing"' in page.data
    assert f.m.FeedbackInvitation.query.count()==0
    assert f.m.ScheduledMessage.query.count()==0


def test_approval_links_author_form_schedule_and_is_idempotent(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f,timing='later')
    with f.organization_context(f.org.id):
        same=s.approve_invitation(f.event,flow,values,f.owner,key,review['review_token'])
        f.db.session.commit()
    assert same.id==invitation.id
    assert f.m.ScheduledMessage.query.count()==1
    assert invitation.author_user_id==f.owner.id
    assert invitation.approved_by_user_id==f.owner.id
    assert invitation.form_version==s.form_version(flow)
    assert invitation.scheduled_message.test_recipient_snapshot_json is None
    assert invitation.recipient_count is None
    assert invitation.approval_key==key
    assert invitation.scheduled_message.scheduled_at.strftime('%Y-%m-%d %H:%M')==values['schedule_date']+' '+values['schedule_time']
    assert 'review_token' not in {c.name for c in invitation.__table__.columns}


def test_review_binds_audience_content_timing_form_and_actor(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    with f.organization_context(f.org.id):
        for changed in [dict(values,audience_rule='registered_contacts'), dict(values,message_body='Changed'), dict(values,timing='later')]:
            with pytest.raises(ValueError):s.approve_invitation(f.event,flow,changed,f.owner,key,review['review_token'])
        with pytest.raises(ValueError):s.approve_invitation(f.event,flow,values,f.staff,key,review['review_token'])
        flow.set_questions(['Changed question?']);f.db.session.commit()
        with pytest.raises(ValueError):s.approve_invitation(f.event,flow,values,f.owner,key,review['review_token'])
    assert f.m.FeedbackInvitation.query.count()==1


def test_late_registrants_included_once_then_snapshot_never_expands(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f,audience='registered_contacts')
    late=contact(f,'+17205550146')
    with f.organization_context(f.org.id), patch.object(s,'roster_hold_reason',return_value=None):
        assert s.prepare_due_invitation(invitation.scheduled_message)
        f.db.session.commit()
        frozen=invitation.scheduled_message.test_recipient_snapshot_json
        assert {r['phone'] for r in json.loads(frozen)}=={row.phone,late.phone}
        contact(f,'+17205550147')
        assert s.prepare_due_invitation(invitation.scheduled_message)
        f.db.session.commit()
        assert invitation.scheduled_message.test_recipient_snapshot_json==frozen
        assert invitation.recipient_count==2


def test_incomplete_roster_blocks_review_and_holds_due_invitation(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f,audience='registered_contacts')
    with f.organization_context(f.org.id), patch.object(s,'roster_hold_reason',return_value='Complete the source inventory first.'):
        with pytest.raises(ValueError,match='source inventory'):s.build_review(f.event,flow,values,f.owner,uuid4().hex)
        assert not s.prepare_due_invitation(invitation.scheduled_message)
        f.db.session.commit()
    assert invitation.scheduled_message.status=='held'
    assert invitation.snapshot_frozen_at is None
    assert invitation.audience_rule=='registered_contacts'


@pytest.mark.parametrize('change',['event_time','form_questions','form_paused','message'])
def test_material_changes_hold_without_discarding_outcome_evidence(feedback,change):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    schedule=invitation.scheduled_message
    with f.organization_context(f.org.id):
        assert s.prepare_due_invitation(schedule);f.db.session.commit()
        if change=='event_time':f.event.external_start_at=f.m.utc_now().replace(tzinfo=None)+timedelta(days=5)
        elif change=='form_questions':flow.set_questions(['A different question?'])
        elif change=='form_paused':flow.is_active=False
        else:schedule.message_body='Changed invitation body'
        f.db.session.commit()
        assert not s.prepare_due_invitation(schedule)
        f.db.session.commit()
    assert schedule.status=='held'
    assert invitation.recipient_count==1
    assert invitation.snapshot_frozen_at is not None


def test_cancelled_registration_removed_at_last_provider_boundary(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f,audience='registered_contacts')
    with f.organization_context(f.org.id), patch.object(s,'roster_hold_reason',return_value=None):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule)
        schedule.status='processing';f.db.session.commit()
        phone=row.phone;f.db.session.delete(row);f.db.session.commit()
        with patch('app.services.twilio_service.time.sleep'):
            result=f.provider.send_message_with_attempt(phone,schedule.message_body,'invitation-cancelled',None,schedule.id,'blast')
    assert not result['success'] and result['status']=='skipped'
    assert f.provider.client.messages.create.call_count==0
    assert invitation.recipient_count==1


def test_scheduler_resolves_late_contacts_and_does_not_duplicate_confirmed_invitation(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    late=contact(f,'+17205550146',event=False)
    from app.services.scheduler_service import send_scheduled_messages
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider), patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app);send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert f.provider.client.messages.create.call_count==2
    assert invitation.recipient_count==2
    assert invitation.scheduled_message.status=='sent'
    assert f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=invitation.scheduled_message_id).count()==2


def test_paused_form_final_boundary_holds_and_review_resumes_same_snapshot(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);schedule.status='processing';f.db.session.commit()
        snapshot=schedule.test_recipient_snapshot_json
        flow.is_active=False;f.db.session.commit()
        result=f.provider.send_message_with_attempt(row.phone,schedule.message_body,'held-invitation',None,schedule.id,'blast')
        assert result['dispatch_halted']
        assert schedule.status=='held'
        assert f.provider.client.messages.create.call_count==0
        flow.is_active=True;f.db.session.commit()
        review=s.held_review(f.event,flow,invitation,f.owner)
        s.resume_held(f.event,flow,invitation,f.owner,review['review_token']);f.db.session.commit()
        assert schedule.status=='pending'
        assert schedule.test_recipient_snapshot_json==snapshot
        assert f.m.MessageDispatchAttempt.query.filter_by(logical_send_key='held-invitation').one().status=='queued'


def test_invitation_routes_are_tenant_bound_and_staff_output_opaque(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    foreign=create_flow(f,event=f.other_event)
    assert f.staff_client.get(f'/events/{f.other_event.id}/feedback/{foreign.id}/invite').status_code==404
    assert f.staff_client.get(f'/events/{f.event.id}/feedback/{flow.id}/invitations/{invitation.id}/review').status_code==302
    page=f.staff_client.get(f'/events/{f.event.id}/feedback/{flow.id}')
    assert row.phone.encode() not in page.data
    assert b'Invitation #1' in page.data


def test_explicit_timezone_required_and_dst_gaps_rejected(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    for changed in [dict(values,timing=''),dict(values,timezone='Not/AZone'),
                    dict(values,timing='later',timezone='America/Denver',schedule_date='2030-03-10',schedule_time='02:30')]:
        with pytest.raises(ValueError):s.invitation_intent(f.event,flow,changed)


def test_stop_after_freeze_is_rechecked_before_provider_submission(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);schedule.status='processing';f.db.session.commit()
        f.db.session.add(f.m.UnsubscribedContact(organization_id=f.org.id,phone=row.phone));f.db.session.commit()
        result=f.provider.send_message_with_attempt(row.phone,schedule.message_body,'invitation-stop',None,schedule.id,'blast')
    assert not result['success']
    assert f.provider.client.messages.create.call_count==0
    assert invitation.recipient_count==1


def test_ambiguous_submission_preserves_evidence_and_never_repeats_provider_call(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.provider.client.messages.create.side_effect=ConnectionError('provider acknowledgement lost')
    from app.services.scheduler_service import send_scheduled_messages
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app);send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert f.provider.client.messages.create.call_count==1
    attempt=f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=invitation.scheduled_message_id).one()
    assert attempt.status=='ambiguous'
    assert s.invitation_history(f.event)[0]['unresolved']==1
    assert invitation.scheduled_message.status=='failed'


def test_interrupted_execution_recovers_original_snapshot_without_expansion_or_resend(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    from app.services.scheduler_service import send_scheduled_messages
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule)
        schedule.status='processing';schedule.processing_started_at=f.m.utc_now().replace(tzinfo=None)-timedelta(hours=1)
        f.db.session.commit()
        # An interruption before the first provider call still retains the
        # initial audience; a new community contact cannot enter on recovery.
        frozen=schedule.test_recipient_snapshot_json
        contact(f,'+17205550149',event=False)
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert f.provider.client.messages.create.call_count==1
    assert invitation.scheduled_message.test_recipient_snapshot_json==frozen
    assert invitation.recipient_count==1
    assert invitation.scheduled_message.status=='sent'


def test_post_freeze_message_change_cannot_be_approved_as_same_execution(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);f.db.session.commit()
        schedule.message_body='A changed body after snapshot freeze';f.db.session.commit()
        assert not s.prepare_due_invitation(schedule);f.db.session.commit()
        review=s.held_review(f.event,flow,invitation,f.owner)
        with pytest.raises(ValueError,match='Restore its original message'):
            s.resume_held(f.event,flow,invitation,f.owner,review['review_token'])
    assert f.provider.client.messages.create.call_count==0


def test_invitation_provider_guard_rejects_outside_snapshot_and_wrong_content(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);schedule.status='processing';f.db.session.commit()
        outside=f.provider.send_message_with_attempt('+17205550199',schedule.message_body,'outside-snapshot',None,schedule.id,'blast')
        assert outside['status']=='skipped'
        mismatch=f.provider.send_message_with_attempt(row.phone,'Unapproved body','wrong-content',None,schedule.id,'blast')
        assert mismatch['dispatch_halted'] and schedule.status=='held'
    assert f.provider.client.messages.create.call_count==0


def test_feedback_invitation_migration_is_additive_and_idempotent(feedback):
    f=feedback
    migration=importlib.import_module('app.saas_migrations.031_feedback_invitations')
    with f.db.engine.begin() as connection:
        migration.apply(connection,logging.getLogger(__name__))
        migration.apply(connection,logging.getLogger(__name__))
    assert f.m.FeedbackInvitation.query.count()==0
    assert f.m.ScheduledMessage.query.count()==0
    assert f.m.Event.query.count()==2


def test_concurrent_scheduler_workers_claim_one_invitation(feedback):
    f=feedback
    if not f.root:
        pytest.skip('True row-lock concurrency requires disposable PostgreSQL.')
    s,flow,row,values,key,review,invitation=setup(f)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.scheduler_service import send_scheduled_messages
    gate=Barrier(2)
    def worker():
        gate.wait(timeout=10)
        send_scheduled_messages(f.app)
        with f.app.app_context():f.db.session.remove()
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=[pool.submit(worker) for _ in range(2)]
            for future in results:future.result(timeout=30)
    f.db.session.expire_all()
    assert f.provider.client.messages.create.call_count==1
    assert invitation.scheduled_message.status=='sent'
    assert f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=invitation.scheduled_message_id).count()==1


def test_verified_empty_event_can_schedule_for_legitimate_later_registrations(feedback):
    f=feedback
    from app.services import feedback_invitation_service as s
    f.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS']=[f.org.slug]
    flow=create_flow(f);future=f.m.utc_now()+timedelta(days=2)
    values={'audience_rule':'registered_contacts','timing':'later','timezone':'UTC',
        'schedule_date':future.strftime('%Y-%m-%d'),'schedule_time':future.strftime('%H:%M'),
        'message_body':f'If you joined us, reply {flow.trigger_keyword}.'}
    with f.organization_context(f.org.id),patch.object(s,'roster_hold_reason',return_value=None):
        key=uuid4().hex;review=s.build_review(f.event,flow,values,f.owner,key)
        assert review['eligible']==0
        invitation=s.approve_invitation(f.event,flow,values,f.owner,key,review['review_token'])
        f.db.session.commit()
        assert invitation.snapshot_frozen_at is None
        late=contact(f,'+17205550148')
        assert s.prepare_due_invitation(invitation.scheduled_message)
        f.db.session.commit()
        assert invitation.recipient_count==1
        assert json.loads(invitation.scheduled_message.test_recipient_snapshot_json)[0]['phone']==late.phone


def test_full_approval_identity_keeps_distinct_keys_and_authors_separate(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    keys=['a'*55+'suffix001','a'*55+'suffix002']
    schedules=[]
    with f.organization_context(f.org.id):
        for actor,approval in [(f.owner,keys[0]),(f.owner,keys[1]),(f.staff,keys[0])]:
            review=s.build_review(f.event,flow,values,actor,approval)
            invite=s.approve_invitation(f.event,flow,values,actor,approval,review['review_token'])
            f.db.session.commit();schedules.append(invite.scheduled_message)
    assert len({r.automation_key for r in schedules})==3
    assert len({r.request_idempotency_key for r in schedules})==3
    assert all(len(r.request_idempotency_key)<=64 for r in schedules)


@pytest.mark.parametrize('corrupt',['not json','{}','[]','[{"phone":"+17205550199"}]'])
def test_corrupt_frozen_snapshot_holds_and_review_gives_actionable_explanation(feedback,corrupt):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);f.db.session.commit()
        schedule.test_recipient_snapshot_json=corrupt;f.db.session.commit()
        assert not s.prepare_due_invitation(schedule);f.db.session.commit()
    response=f.client.get(f'/events/{f.event.id}/feedback/{flow.id}/invitations/{invitation.id}/review')
    assert response.status_code==302
    assert 'original frozen audience' in f.client.get(response.location).get_data(as_text=True)
    assert f.provider.client.messages.create.call_count==0


def test_reapproval_retains_original_and_latest_form_version_authority(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    original_version=invitation.form_version
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);f.db.session.commit()
        flow.set_questions(['Updated wording after approval?']);f.db.session.commit()
        assert not s.prepare_due_invitation(schedule);f.db.session.commit()
        review=s.held_review(f.event,flow,invitation,f.staff)
        s.resume_held(f.event,flow,invitation,f.staff,review['review_token']);f.db.session.commit()
        history=json.loads(invitation.approval_history_json)
    assert len(history)==2
    assert history[0]['form_version']==original_version
    assert history[0]['actor_user_id']==f.owner.id
    assert history[1]['form_version']==s.form_version(flow)
    assert history[1]['actor_user_id']==f.staff.id
    assert invitation.author_user_id==f.owner.id
    assert invitation.approved_by_user_id==f.staff.id
    assert row.phone not in invitation.approval_history_json


def test_late_audience_over_limit_holds_invitation_and_other_due_schedule_executes(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.app.config['SEND_MAX_RECIPIENTS']=1
    contact(f,'+17205550146',event=False)
    following=f.m.ScheduledMessage(organization_id=f.org.id,scheduled_at=f.m.utc_now().replace(tzinfo=None),
        message_body='Another explicitly reviewed message',target='community',status='pending',
        test_recipient_snapshot_json=json.dumps([{'phone':row.phone,'name':'Fixture guest'}]))
    f.db.session.add(following);f.db.session.commit()
    from app.services.scheduler_service import send_scheduled_messages
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert invitation.scheduled_message.status=='held'
    assert 'recipient limit' in invitation.hold_reason
    assert invitation.snapshot_frozen_at is None
    assert invitation.scheduled_message.test_recipient_snapshot_json is None
    assert following.status=='sent'
    assert f.provider.client.messages.create.call_count==1
    assert f.provider.client.messages.create.call_args.kwargs['body']==following.message_body
    assert f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=invitation.scheduled_message_id).count()==0


def test_unexpected_preparation_error_rolls_back_only_current_schedule_and_continues(feedback):
    f=feedback
    # Insert an earlier ordinary schedule, then the broken invitation, then
    # another ordinary schedule. Their independently committed evidence stays.
    earlier=f.m.ScheduledMessage(organization_id=f.org.id,scheduled_at=f.m.utc_now().replace(tzinfo=None),
        message_body='Independent earlier reviewed send',target='community',status='pending',
        test_recipient_snapshot_json=json.dumps([{'phone':'+17205550145','name':'Fixture guest'}]))
    f.db.session.add(earlier);f.db.session.commit()
    s,flow,row,values,key,review,invitation=setup(f)
    later=f.m.ScheduledMessage(organization_id=f.org.id,scheduled_at=f.m.utc_now().replace(tzinfo=None),
        message_body='Independent later reviewed send',target='community',status='pending',
        test_recipient_snapshot_json=json.dumps([{'phone':row.phone,'name':'Fixture guest'}]))
    f.db.session.add(later);f.db.session.commit()
    others=[earlier,later]
    original=s.prepare_due_invitation
    def broken_prepare(schedule):
        if schedule.id==invitation.scheduled_message_id:
            schedule.error_message='uncommitted preparation mutation'
            raise RuntimeError('owned preparation failure')
        return original(schedule)
    from app.services.scheduler_service import send_scheduled_messages
    with patch.object(s,'prepare_due_invitation',side_effect=broken_prepare),patch(
            'app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert invitation.scheduled_message.status=='pending'
    assert invitation.scheduled_message.error_message is None
    assert invitation.snapshot_frozen_at is None
    assert all(item.status=='sent' and item.message_log_id for item in others)
    assert f.provider.client.messages.create.call_count==2
    assert f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=invitation.scheduled_message_id).count()==0



def test_late_unicode_personalization_over_segment_limit_holds_before_freezing_and_other_send_executes(feedback):
    from app.services.scheduler_service import send_scheduled_messages
    from app.utils import analyze_personalized_sms_blast
    f=feedback
    f.app.config['SEND_MAX_SEGMENTS']=2
    body='Hi {name}: '+('A'*100)
    s,flow,row,values,key,review,invitation=setup(f,message_body=body)
    assert review['eligible']==1 and review['segments']==1
    late=contact(f,'+17205550146',event=False)
    late.name='山田';f.db.session.commit()
    estimate=analyze_personalized_sms_blast(body,[{'phone':row.phone,'name':row.name},
                                               {'phone':late.phone,'name':late.name}])
    assert estimate['unique_recipients']==2 < f.app.config['SEND_MAX_RECIPIENTS']
    assert estimate['total_segments']==3 > f.app.config['SEND_MAX_SEGMENTS']
    following=f.m.ScheduledMessage(organization_id=f.org.id,scheduled_at=f.m.utc_now().replace(tzinfo=None),
        message_body='Another reviewed send',target='community',status='pending',
        test_recipient_snapshot_json=json.dumps([{'phone':row.phone,'name':row.name}]))
    f.db.session.add(following);f.db.session.commit()
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert invitation.scheduled_message.status=='held'
    assert '3 SMS segments' in invitation.hold_reason and 'limit is 2' in invitation.hold_reason
    assert invitation.snapshot_frozen_at is None and invitation.recipient_count is None
    assert invitation.scheduled_message.test_recipient_snapshot_json is None
    assert f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=invitation.scheduled_message_id).count()==0
    assert following.status=='sent'
    assert f.provider.client.messages.create.call_count==1
    assert f.provider.client.messages.create.call_args.kwargs['body']==following.message_body
    # It remains recoverable through the existing explicit held-review path.
    # Once the configured limit permits the reviewed three-segment audience,
    # the same invitation freezes and executes once rather than needing a copy.
    f.app.config['SEND_MAX_SEGMENTS']=3
    with f.organization_context(f.org.id):
        held=s.held_review(f.event,flow,invitation,f.owner)
        assert held['segments']==3
        s.resume_held(f.event,flow,invitation,f.owner,held['review_token'])
        f.db.session.commit()
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app);send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert invitation.scheduled_message.status=='sent'
    assert invitation.recipient_count==2 and invitation.snapshot_frozen_at is not None
    assert f.m.FeedbackInvitation.query.count()==1
    assert f.provider.client.messages.create.call_count==3


def _held_at(f, service, invitation, scheduled_at):
    schedule=invitation.scheduled_message
    schedule.scheduled_at=scheduled_at
    schedule.status='held'
    invitation.hold_reason='Awaiting explicit operator review.'
    invitation.context_digest=service.context_digest(f.event,invitation.survey,schedule)
    history=json.loads(invitation.approval_history_json)
    history[0]['scheduled_at']=scheduled_at.isoformat()
    invitation.approval_history_json=json.dumps(history)
    f.db.session.commit()
    return schedule


def test_expired_held_invitation_requires_deliberate_new_timing_choice(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.app.config['SCHEDULED_MESSAGE_MAX_LAG']=60
    old=f.m.utc_now().replace(tzinfo=None)-timedelta(hours=2)
    schedule=_held_at(f,s,invitation,old)
    with f.organization_context(f.org.id):
        with pytest.raises(ValueError,match='original send window has expired'):
            s.held_review(f.event,flow,invitation,f.owner)
        with pytest.raises(ValueError,match='original send window has expired'):
            s.resume_held(f.event,flow,invitation,f.owner,'old-token',resume_timing='original')
    assert schedule.status=='held' and schedule.scheduled_at==old
    assert f.provider.client.messages.create.call_count==0


def test_original_review_cannot_silently_become_send_now_or_resume_after_lag_expires(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.app.config['SCHEDULED_MESSAGE_MAX_LAG']=60
    original=f.m.utc_now().replace(tzinfo=None)
    schedule=_held_at(f,s,invitation,original)
    with f.organization_context(f.org.id),patch.object(s,'_now',return_value=original+timedelta(minutes=30)):
        held=s.held_review(f.event,flow,invitation,f.owner)
        with pytest.raises(ValueError,match='changed'):
            s.resume_held(f.event,flow,invitation,f.owner,held['review_token'],resume_timing='now')
    with f.organization_context(f.org.id),patch.object(s,'_now',return_value=original+timedelta(minutes=61)):
        with pytest.raises(ValueError,match='original send window has expired'):
            s.resume_held(f.event,flow,invitation,f.owner,held['review_token'])
    assert schedule.status=='held' and schedule.scheduled_at==original
    assert len(json.loads(invitation.approval_history_json))==1


def test_explicit_send_now_recovers_expired_hold_and_retains_both_approval_times(feedback):
    from app.services.scheduler_service import send_scheduled_messages
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.app.config['SCHEDULED_MESSAGE_MAX_LAG']=60
    old=f.m.utc_now().replace(tzinfo=None)-timedelta(hours=2)
    schedule=_held_at(f,s,invitation,old)
    before=f.m.utc_now().replace(tzinfo=None)
    with f.organization_context(f.org.id):
        reviewed=s.held_review(f.event,flow,invitation,f.owner,resume_timing='now')
        assert reviewed['confirmation_label']=='Confirm send now'
        assert 'Send now' in reviewed['timing']
        s.resume_held(f.event,flow,invitation,f.owner,reviewed['review_token'],resume_timing='now')
        f.db.session.commit()
        assert schedule.status=='pending' and schedule.scheduled_at>=before
        history=json.loads(invitation.approval_history_json)
        assert history[0]['scheduled_at']==old.isoformat()
        assert history[-1]['previous_scheduled_at']==old.isoformat()
        assert history[-1]['scheduled_at']==schedule.scheduled_at.isoformat()
        assert history[-1]['resume_timing']=='now'
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app);send_scheduled_messages(f.app)
    f.db.session.expire_all()
    assert schedule.status=='sent' and invitation.recipient_count==1
    assert f.provider.client.messages.create.call_count==1
    assert f.m.FeedbackInvitation.query.count()==1


def test_expired_partial_frozen_invitation_preserves_attempts_and_excludes_late_contacts(feedback):
    from app.services.scheduler_service import send_scheduled_messages
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    second=contact(f,'+17205550146',event=False)
    third=contact(f,'+17205550147',event=False)
    schedule=invitation.scheduled_message
    original_provider=f.provider.client.messages.create.side_effect
    call_number=0
    def partial_provider(**params):
        nonlocal call_number
        call_number+=1
        if call_number==2:raise ConnectionError('owned lost provider acknowledgement')
        return original_provider(**params)
    with f.organization_context(f.org.id):
        assert s.prepare_due_invitation(schedule)
        schedule.status='processing';f.db.session.commit()
        frozen=schedule.test_recipient_snapshot_json
        frozen_at=invitation.snapshot_frozen_at
        rows=json.loads(frozen)
        f.provider.client.messages.create.side_effect=partial_provider
        partial=f.provider.send_bulk_with_attempts(rows[:2],schedule.message_body,f'scheduled:{schedule.id}',None,schedule.id,0,'blast')
        assert partial['success_count']==1 and partial['failure_count']==1
        original_attempts={a.logical_send_key:(a.status,a.provider_message_sid) for a in f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=schedule.id)}
        assert sorted(state[0] for state in original_attempts.values())==['ambiguous','sent']
        f.app.config['SCHEDULED_MESSAGE_MAX_LAG']=60
        old=f.m.utc_now().replace(tzinfo=None)-timedelta(hours=2)
        _held_at(f,s,invitation,old)
        late=contact(f,'+17205550148',event=False)
        reviewed=s.held_review(f.event,flow,invitation,f.owner,resume_timing='now')
        s.resume_held(f.event,flow,invitation,f.owner,reviewed['review_token'],resume_timing='now')
        f.db.session.commit()
        assert schedule.test_recipient_snapshot_json==frozen
        assert invitation.snapshot_frozen_at==frozen_at and invitation.recipient_count==3
    f.provider.client.messages.create.side_effect=original_provider
    with patch('app.services.scheduler_service.get_twilio_service',return_value=f.provider),patch('app.services.twilio_service.time.sleep'):
        send_scheduled_messages(f.app);send_scheduled_messages(f.app)
    f.db.session.expire_all()
    # Two original provider calls (one uncertain), then only the untouched third
    # recipient. Retiming cannot change the schedule/phone/body dispatch keys.
    assert f.provider.client.messages.create.call_count==3
    assert late.phone not in [call.kwargs['to'] for call in f.provider.client.messages.create.call_args_list]
    for key,state in original_attempts.items():
        attempt=f.m.MessageDispatchAttempt.query.filter_by(logical_send_key=key).one()
        assert (attempt.status,attempt.provider_message_sid)==state
    assert f.m.MessageDispatchAttempt.query.filter_by(scheduled_message_id=schedule.id).count()==3
    assert schedule.test_recipient_snapshot_json==frozen and invitation.recipient_count==3
    assert schedule.status=='failed'  # The original ambiguous evidence remains unresolved.


def test_retiming_a_frozen_invitation_rejects_changed_body_before_time_mutation(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.app.config['SCHEDULED_MESSAGE_MAX_LAG']=60
    with f.organization_context(f.org.id):
        schedule=invitation.scheduled_message
        assert s.prepare_due_invitation(schedule);f.db.session.commit()
        old=f.m.utc_now().replace(tzinfo=None)-timedelta(hours=2)
        _held_at(f,s,invitation,old)
        schedule.message_body='Changed frozen message';f.db.session.commit()
        reviewed=s.held_review(f.event,flow,invitation,f.owner,resume_timing='now')
        with pytest.raises(ValueError,match='Restore its original message'):
            s.resume_held(f.event,flow,invitation,f.owner,reviewed['review_token'],resume_timing='now')
        assert schedule.scheduled_at==old and schedule.status=='held'


@pytest.mark.parametrize('actor',['owner','staff'])
def test_expired_held_route_requires_explicit_review_now_then_confirmation(feedback,actor):
    import re
    from html import unescape
    f=feedback;s,flow,row,values,key,review,invitation=setup(f)
    f.app.config['SCHEDULED_MESSAGE_MAX_LAG']=60
    old=f.m.utc_now().replace(tzinfo=None)-timedelta(hours=2)
    schedule=_held_at(f,s,invitation,old)
    client=f.client if actor=='owner' else f.staff_client
    path=f'/events/{f.event.id}/feedback/{flow.id}/invitations/{invitation.id}/review'
    choice=client.get(path)
    assert choice.status_code==200 and b'Review sending now' in choice.data
    assert b'name="review_token"' not in choice.data
    assert b'Confirm send now' not in choice.data
    assert schedule.scheduled_at==old and schedule.status=='held'
    rejected=client.post(path,data={'resume_timing':'original','review_token':'old-token'})
    assert rejected.status_code==302
    page=client.get(path+'?resume_timing=now')
    assert page.status_code==200 and b'Confirm send now' in page.data
    token=unescape(re.search(r'name="review_token" value="([^"]+)"',page.get_data(as_text=True)).group(1))
    assert row.phone.encode() not in page.data
    assert schedule.scheduled_at==old and schedule.status=='held'
    response=client.post(path,data={'resume_timing':'now','review_token':token})
    assert response.status_code==302
    f.db.session.expire_all()
    assert schedule.status=='pending' and schedule.scheduled_at>old
    assert 'sending now' in client.get(response.location).get_data(as_text=True)


def test_nonexpired_original_review_preserves_original_future_time(feedback):
    f=feedback;s,flow,row,values,key,review,invitation=setup(f,timing='later')
    original=invitation.scheduled_message.scheduled_at
    schedule=_held_at(f,s,invitation,original)
    with f.organization_context(f.org.id):
        reviewed=s.held_review(f.event,flow,invitation,f.owner)
        assert reviewed['confirmation_label']=='Confirm reviewed invitation'
        assert reviewed['intent']['resume_timing']=='original'
        s.resume_held(f.event,flow,invitation,f.owner,reviewed['review_token'])
        f.db.session.commit()
    assert schedule.status=='pending' and schedule.scheduled_at==original
    assert json.loads(invitation.approval_history_json)[-1]['resume_timing']=='original'
    assert f.provider.client.messages.create.call_count==0

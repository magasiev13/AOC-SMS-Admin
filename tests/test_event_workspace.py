"""Event operators see independent bookings and evidence-based communication."""
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from tests.test_event_feedback import feedback, create_flow


def test_group_booking_metrics_and_staff_privacy_without_a_customer_specific_rule(feedback):
    from app.services.booking_projection_service import upsert_booking, reconcile_event_projection
    f = feedback
    with f.organization_context(f.org.id):
        upsert_booking(f.event, {'booking_id': 'order-one', 'spaces': 10, 'name': 'Family group',
                                'phone': '+17205550145', 'sms_consent': True, 'source_revision': 1},
                       'app:1:b3BhcXVlLWNvbnRhY3Q', f.m.utc_now(), source_scope='cGhvbmUtZGVyaXZlZC1zY29wZQ')
        upsert_booking(f.event, {'booking_id': 'order-two', 'spaces': 2, 'name': 'Company guests',
                                'phone': '', 'source_revision': 1, 'status': 'pending'},
                       'app:1:community-crm', f.m.utc_now(), source_scope='registrations')
        reconcile_event_projection(f.event)
        f.db.session.commit()
    page = f.staff_client.get(f'/events/{f.event.id}')
    assert page.status_code == 200
    for text in [b'Bookings', b'Reserved places', b'Reachable contacts', b'Missing contact details',
                 b'Company guests', b'Family group', b'10 reserved places', b'2 reserved places', b'Unverified coverage']:
        assert text in page.data
    assert b'+17205550145' not in page.data
    assert b'order-one' not in page.data
    assert b'b3BhcXVlLWNvbnRhY3Q' not in page.data
    assert b'cGhvbmUtZGVyaXZlZC1zY29wZQ' not in page.data
    assert b'Attendance requires separate check-in evidence' in page.data
    for section in ['overview', 'registrations', 'messages', 'feedback']:
        assert f'id="{section}"'.encode() in page.data


def test_reminders_use_exact_source_identity_without_changing_community_audience(feedback):
    from app.services.aoc_event_sync_service import _automation_key
    from app.services.event_workspace_service import event_communications
    f = feedback
    f.event.external_source = 'wordpress_events_manager'
    f.event.external_event_id = 'external-event-42'
    for ordinal, kind in enumerate(['two_week', 'seven_day', 'one_day', 'day_of']):
        f.db.session.add(f.m.ScheduledMessage(organization_id=f.org.id,
            scheduled_at=f.m.utc_now().replace(tzinfo=None) + timedelta(days=ordinal+1),
            message_body='Existing reminder', target='community', event_id=None,
            automation_source='event_sync', automation_key=_automation_key(f.event, kind), automation_kind=kind))
    f.db.session.add(f.m.ScheduledMessage(organization_id=f.org.id, scheduled_at=f.m.utc_now(),
        message_body='Similar unrelated event', target='community', event_id=None,
        automation_source='other_automation', automation_key=_automation_key(f.event, 'one_day')))
    f.db.session.commit()
    with f.organization_context(f.org.id):
        rows = event_communications(f.event)
    assert len(rows['scheduled']) == 4
    assert rows['next_message']['label'] == 'Two weeks before'
    assert all(row['schedule'].event_id is None and row['schedule'].target == 'community' for row in rows['scheduled'])
    page = f.staff_client.get(f'/events/{f.event.id}')
    assert page.status_code == 200 and b'One week before' in page.data
    assert b'Similar unrelated event' not in page.data


def test_missing_receipts_never_become_delivered(feedback):
    from app.services.event_workspace_service import submission_outcomes
    f = feedback
    log = f.m.MessageLog(organization_id=f.org.id, message_body='Submission proof',
        target='event', event_id=f.event.id, total_recipients=3, success_count=2, failure_count=1,
        details=json.dumps([{'sid': 'SMpending', 'success': True}]))
    f.db.session.add(log)
    f.db.session.commit()
    with f.organization_context(f.org.id):
        result = submission_outcomes(log)
    assert result == {'total': 3, 'accepted': 2, 'delivered': 0, 'submission_failed': 1,
                      'delivery_failed': 0, 'unresolved': 2, 'acceptance_rate': 66.7}
    dashboard = f.client.get('/dashboard')
    assert b'Provider acceptance' in dashboard.data
    assert b'Delivery Rate' not in dashboard.data
    assert b'0 confirmed delivered' in dashboard.data
    assert b'>Delivered<' not in dashboard.data


def test_manual_shared_contact_and_missing_phone_keep_independent_seats(feedback):
    f = feedback
    for phone, spaces in [('+17205550145', '10'), ('+17205550145', '3'), ('', '2')]:
        response = f.client.post(f'/events/{f.event.id}/register', data={'name': 'Group', 'phone': phone, 'party_size': spaces})
        assert response.status_code == 302
    with f.organization_context(f.org.id):
        assert f.m.EventBooking.query.count() == 3
        assert f.m.EventRegistration.query.count() == 1
        assert f.event.reserved_places == 15
    response = f.client.post(f'/events/{f.event.id}/delete')
    assert response.status_code == 302
    assert f.db.session.get(f.m.Event, f.event.id) is not None


def test_source_authority_requires_owner_and_complete_inventory(feedback):
    f = feedback
    path = f'/events/{f.event.id}/registration-sources'
    data = {'source': 'app:1:company-events', 'source_scope': 'conference-2026', 'action': 'configure'}
    assert f.staff_client.post(path, data=data).status_code == 403
    assert f.client.post(path, data=data).status_code == 302
    data.update(action='verify', compared_with_source='1')
    assert f.client.post(path, data=data).status_code == 302
    row = f.m.EventRegistrationSource.query.filter_by(event_id=f.event.id).one()
    assert not row.authority_enabled
    assert f.staff_client.post(f'/events/{f.other_event.id}/registration-sources', data=data).status_code in {403,404}


def test_feedback_links_open_deliberate_invitation_setup_without_writes(feedback):
    f = feedback
    flow = create_flow(f)
    path = f'/dashboard?target=event&event_id={f.event.id}&feedback_id={flow.id}'
    page = f.staff_client.get(path, follow_redirects=True)
    assert page.status_code == 200
    assert b'name="audience_rule"' in page.data and b'name="timing"' in page.data
    assert f.m.ScheduledMessage.query.count() == 0
    assert f.m.FeedbackInvitation.query.count() == 0
    assert f.staff_client.post('/dashboard/review', data={'feedback_id': flow.id}).status_code == 400


def test_app_integration_setup_is_workspace_owner_scoped(feedback):
    f = feedback
    assert f.staff_client.get('/settings/event-sync?provider=app').status_code == 403
    page = f.client.get('/settings/event-sync?provider=app')
    assert page.status_code == 200
    assert b'Enable app integration' in page.data
    assert f'/webhooks/event-sync/{f.org.slug}/app'.encode() in page.data
    assert b'X-Twinevia-Signature' in page.data
    assert f.m.ScheduledMessage.query.count() == 0


def test_empty_configured_event_is_retained_instead_of_deletion_error(feedback):
    from app.services.booking_inventory_service import configure_event_source
    f = feedback
    with f.organization_context(f.org.id):
        configure_event_source(f.event, 'app:1:business-events', 'registrations')
        f.db.session.commit()
    assert f.client.post(f'/events/{f.event.id}/delete').status_code == 302
    assert f.client.post('/events/bulk-delete', data={'event_ids': str(f.event.id)}).status_code == 302
    assert f.db.session.get(f.m.Event, f.event.id) is not None


def test_local_bookings_can_cancel_one_and_explicitly_link_or_unlink(feedback):
    from app.services.booking_projection_service import create_local_booking, roster_summary
    f = feedback
    with f.organization_context(f.org.id):
        first = create_local_booking(f.event, 'First transaction', '+17205550145', party_size=10)
        second = create_local_booking(f.event, 'Second transaction', '+17205550145', party_size=3)
        f.db.session.commit()
    path = f'/events/{f.event.id}/bookings/{second.id}/duplicate'
    assert f.staff_client.post(path, data={'primary_booking_id': first.id, 'same_transaction': '1'}).status_code == 403
    assert f.client.post(path, data={'primary_booking_id': first.id, 'same_transaction': '1'}).status_code == 302
    assert roster_summary(f.event)['reserved_places'] == 10
    f.client.post(path, data={'action': 'unlink'})
    assert roster_summary(f.event)['reserved_places'] == 13
    assert f.client.post(f'/events/{f.event.id}/bookings/{second.id}/cancel').status_code == 302
    f.db.session.expire_all()
    assert roster_summary(f.event)['reserved_places'] == 10
    assert f.m.EventRegistration.query.filter_by(event_id=f.event.id).count() == 1
    assert f.m.EventBooking.query.count() == 2


def test_reviewed_community_announcement_keeps_event_context_and_audience(feedback):
    f = feedback
    f.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS'] = [f.org.slug]
    f.db.session.add(f.m.CommunityMember(organization_id=f.org.id, name='Member', phone='+17205550145'))
    f.db.session.commit()
    form = {'target': 'community', 'context_event_id': f.event.id, 'message_body': 'Join our community event.',
            'request_idempotency_key': uuid4().hex, 'require_review': '1'}
    review = f.client.post('/dashboard/review', data=form)
    assert review.status_code == 200 and review.json['audience'] == 'Community'
    form['review_token'] = review.json['review_token']
    with patch('app.routes.enqueue_outbound_request'):
        response = f.client.post('/dashboard', data=form)
    assert response.status_code == 302
    row = f.m.MessageLog.query.one()
    assert row.target == 'community' and row.event_id == f.event.id
    assert row.total_recipients == 1


def test_held_invitation_schedule_history_is_preserved(feedback):
    from tests.test_feedback_invitations import setup
    f = feedback
    service, flow, contact, values, key, review, invitation = setup(f)
    invitation.scheduled_message.status = 'held'
    f.db.session.commit()
    schedule_id = invitation.scheduled_message_id
    assert f.client.post(f'/scheduled/{schedule_id}/delete').status_code == 302
    assert f.db.session.get(f.m.ScheduledMessage, schedule_id) is not None
    f.client.post(f'/scheduled/{schedule_id}/cancel')
    assert f.db.session.get(f.m.ScheduledMessage, schedule_id).status == 'cancelled'
    f.client.post('/scheduled/bulk-delete', data={'scheduled_ids': str(schedule_id)})
    assert f.db.session.get(f.m.ScheduledMessage, schedule_id) is not None


def test_community_feedback_invitation_is_event_associated_and_held_review_reachable(feedback):
    from tests.test_feedback_invitations import setup
    from app.services.event_workspace_service import event_communications
    f=feedback
    service,flow,contact,values,key,review,invitation=setup(f)
    assert invitation.scheduled_message.target=='community'
    assert invitation.scheduled_message.event_id is None
    with f.organization_context(f.org.id):
        communications=event_communications(f.event)
        assert communications['next_message']['schedule'].id==invitation.scheduled_message_id
        assert communications['next_message']['label']=='Feedback invitation'
        assert len(communications['scheduled'])==1
        invitation.scheduled_message.status='held'
        invitation.hold_reason='Compare the event roster before resuming.'
        f.db.session.commit()
    page=f.staff_client.get(f'/events/{f.event.id}')
    assert page.status_code==200
    assert b'Review held invitation' in page.data
    assert f'/events/{f.event.id}/feedback/{flow.id}/invitations/{invitation.id}/review'.encode() in page.data
    assert contact.phone.encode() not in page.data
    assert f'/scheduled'.encode() in page.data


def test_reminder_key_and_kind_must_both_match_event_identity(feedback):
    from app.services.aoc_event_sync_service import _automation_key
    from app.services.event_workspace_service import event_communications
    f=feedback
    f.event.external_source='wordpress_events_manager';f.event.external_event_id='42'
    f.db.session.add(f.m.ScheduledMessage(organization_id=f.org.id,
        scheduled_at=f.m.utc_now().replace(tzinfo=None),message_body='Mismatched automation',
        target='community',event_id=None,automation_source='event_sync',
        automation_key=_automation_key(f.event,'one_day'),automation_kind='two_week'))
    f.db.session.commit()
    with f.organization_context(f.org.id):
        assert event_communications(f.event)['scheduled']==[]


def test_ambiguous_acceptance_is_unresolved_even_when_legacy_failure_aggregate_includes_it(feedback):
    from app.services.event_workspace_service import submission_outcomes
    f=feedback
    log=f.m.MessageLog(organization_id=f.org.id,message_body='Outcome evidence',target='community',
        total_recipients=3,success_count=1,failure_count=2,
        details=json.dumps([{'phone':'+17205550141','sid':'SMaccepted','success':True,'status':'sent'},
                            {'phone':'+17205550142','success':False,'status':'ambiguous'},
                            {'phone':'+17205550143','success':False,'status':'failed'}]))
    f.db.session.add(log);f.db.session.commit()
    with f.organization_context(f.org.id):result=submission_outcomes(log)
    assert result['submission_failed']==1
    assert result['unresolved']==2
    assert result['delivered']==0
    assert result['accepted']==1

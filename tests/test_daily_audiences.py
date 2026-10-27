"""Current audience projection and intentional past-event feedback, with no purge."""
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from flask import template_rendered
from sqlalchemy import text


@pytest.fixture
def daily(tmp_path, monkeypatch):
    root = os.environ.get('AUDIENCE_TEST_POSTGRES_ROOT')
    database_name = None
    if root:
        if root != 'postgresql://audit@127.0.0.1:55432/postgres':
            raise RuntimeError('Audience tests require the owned local PostgreSQL audit root.')
        import psycopg
        from psycopg import sql
        database_name = 'daily_audience_' + uuid4().hex
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'daily.db')
    for key, value in {
        'DATABASE_URL': uri, 'SAAS_MODE': '1', 'FLASK_DEBUG': '1', 'SCHEDULER_ENABLED': '0',
        'PYTHON_DOTENV_DISABLED': '1', 'SECRET_KEY': 'isolated-daily-workflow', 'AUTH_ALERTS_ENABLED': '0',
        'COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS': 'armenians-of-colorado',
        'APP_TIMEZONE': 'America/Denver', 'REDIS_URL': 'redis://127.0.0.1:1/0',
    }.items():
        monkeypatch.setenv(key, value)
    import app.config
    importlib.reload(app.config)
    from app import create_app, db, models
    from app.services import audience_service as audience
    from app.services.recipient_permission_service import permission_context
    from app.services.twilio_service import TwilioService
    from app.tenant import organization_context
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, TWILIO_ALLOW_LIVE_SENDS_IN_TESTING=True)
    try:
        with app.app_context():
            db.create_all()
            org = models.Organization(name='AOC daily workflow', slug='armenians-of-colorado')
            other = models.Organization(name='Independent workspace', slug='independent-workspace')
            user = models.AppUser(username='daily-owner', email='daily@fixture.test', phone='+17205550991', role='admin')
            user.set_password('Daily-fixture-pass1!')
            db.session.add_all([org, other, user])
            db.session.flush()
            db.session.add_all([
                models.OrganizationMembership(organization_id=org.id, user_id=user.id, role='owner'),
                models.OrganizationSubscription(organization=org, status='complimentary'),
                models.OrganizationMessagingProfile(organization=org, status='active', provider_status='active',
                    sender_review_status='approved', from_number='+17205550999', phone_number_sid='PNfixture',
                    twilio_subaccount_sid='ACfixture', messaging_service_sid='MGfixture'),
            ])
            for policy, key in [('terms','TERMS_POLICY_VERSION'),('privacy','PRIVACY_POLICY_VERSION'),('acceptable_use','ACCEPTABLE_USE_POLICY_VERSION'),('sms','SMS_POLICY_VERSION'),('billing','BILLING_POLICY_VERSION')]:
                db.session.add(models.CustomerPolicyAcceptance(organization_id=org.id, user_id=user.id, policy_name=policy, policy_version=app.config[key]))
            db.session.commit()
            provider = TwilioService.__new__(TwilioService)
            provider.organization_id, provider.account_sid = org.id, 'ACfixture'
            provider.messaging_service_sid, provider.from_number = 'MGfixture', '+17205550999'
            provider.client = MagicMock()
            provider.client.messages.create.side_effect = lambda **kwargs: SimpleNamespace(sid='SM'+uuid4().hex, status='queued', account_sid='ACfixture', num_segments='1')
            client = app.test_client()
            client.post('/login', data={'username':user.email, 'password':'Daily-fixture-pass1!'})
            yield SimpleNamespace(app=app, db=db, m=models, org=org, other=other, owner=user,
                audience=audience, provider=provider, client=client, organization_context=organization_context,
                permission_context=permission_context, now=models.utc_now().replace(tzinfo=None), root=root)
            db.session.remove()
            db.engine.dispose()
    finally:
        if database_name:
            import psycopg
            from psycopg import sql
            with psycopg.connect(root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database_name)))


def event(daily, *, title='Event', days=0, org=None, **fields):
    today = daily.now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('America/Denver')).date()
    row = daily.m.Event(organization_id=(org or daily.org).id, title=title, date=today+timedelta(days=days), **fields)
    daily.db.session.add(row)
    daily.db.session.flush()
    return row


def registration(daily, selected_event, phone='+17205550145', name='Attendee'):
    row = daily.m.EventRegistration(organization_id=selected_event.organization_id, event_id=selected_event.id, phone=phone, name=name)
    daily.db.session.add(row)
    daily.db.session.flush()
    return row


def context_for(daily, path):
    captured = []
    def receiver(sender, template, context, **extra):
        captured.append(context)
    with template_rendered.connected_to(receiver, daily.app):
        response = daily.client.get(path)
    assert response.status_code == 200
    return response, captured[-1]


def test_ninety_past_attendees_leave_current_totals_without_deleting_history(daily):
    past = event(daily, title='Past community gathering', days=-90)
    for number in range(90):
        registration(daily, past, phone=f'+1720555{number:04d}')
    daily.db.session.commit()
    with daily.organization_context(daily.org.id):
        assert daily.audience.current_audience_counts()['event_registrations'] == 0
        assert daily.audience.event_registrations_query(current_only=False).count() == 90
    response, context = context_for(daily, '/dashboard')
    assert context['event_registration_count'] == 0
    assert context['total_recipients'] == 0
    assert 'Past community gathering' not in response.get_data(as_text=True)
    assert daily.m.EventRegistration.query.count() == 90
    assert daily.m.Event.query.count() == 1


def test_future_ongoing_date_only_and_dst_boundaries_are_consistent(daily):
    fixed = datetime(2026,11,1,5,30,tzinfo=timezone.utc)  # Oct31 23:30 in Denver before DST ends.
    same_day = event(daily, title='Date-only')
    same_day.date = datetime(2026,10,31).date()
    ongoing = event(daily, title='Multi-day', days=-2, external_end_at=fixed.replace(tzinfo=None)+timedelta(hours=4))
    ended = event(daily, title='Ended', external_end_at=fixed.replace(tzinfo=None))
    future = event(daily, title='Future', days=10)
    future.date = datetime(2026,11,3).date()
    draft = event(daily, title='Draft', days=10, external_status='draft')
    daily.db.session.commit()
    with daily.organization_context(daily.org.id):
        assert {row.id for row in daily.audience.events_query(now=fixed)} == {same_day.id,ongoing.id,future.id}
        assert {row.id for row in daily.audience.events_query(now=fixed+timedelta(hours=3))} == {ongoing.id,future.id}
        assert {row.id for row in daily.audience.events_query(now=fixed+timedelta(hours=5))} == {future.id}
    assert daily.m.Event.query.count() == 5


def test_contactable_filter_precedes_pagination_and_keeps_tenant_scope(daily):
    for number in range(60):
        phone = f'+1720660{number:04d}'
        daily.db.session.add(daily.m.CommunityMember(organization_id=daily.org.id, name=f'A blocked {number:02d}', phone=phone))
        daily.db.session.add(daily.m.UnsubscribedContact(organization_id=daily.org.id, phone=phone, source='inbound'))
    for number in range(55):
        daily.db.session.add(daily.m.CommunityMember(organization_id=daily.org.id, name=f'B contactable {number:02d}', phone=f'+1720770{number:04d}'))
    daily.db.session.add(daily.m.SuppressedContact(organization_id=daily.other.id, phone='+17207700000', category='hard_fail', source='manual'))
    daily.db.session.commit()
    response, context = context_for(daily, '/community')
    assert (len(context['members']),context['total_count'],context['total_pages']) == (50,55,2)
    assert 'A blocked' not in response.get_data(as_text=True)
    assert 'B contactable 00' in response.get_data(as_text=True)
    _, page_two = context_for(daily, '/community?page=2')
    assert len(page_two['members']) == 5
    response, all_rows = context_for(daily, '/community?view=all')
    assert all_rows['total_count'] == 115
    assert 'view=all' in response.get_data(as_text=True)
    assert 'name="view" value="all"' in response.get_data(as_text=True)
    assert daily.m.CommunityMember.query.count() == 115
    assert daily.m.UnsubscribedContact.query.count() == 60


def test_contactable_search_export_and_legacy_formatted_restrictions_match(daily):
    phone = '+17205550145'
    daily.db.session.add(daily.m.CommunityMember(organization_id=daily.org.id, name='Opted out retained', phone=phone))
    negative = daily.m.UnsubscribedContact(organization_id=daily.org.id, phone=phone, source='inbound')
    daily.db.session.add(negative)
    daily.db.session.commit()
    daily.db.session.execute(text('UPDATE unsubscribed_contacts SET phone=:phone WHERE id=:id'), {'phone':'(720) 555-0145','id':negative.id})
    daily.db.session.commit()
    response, context = context_for(daily, '/community?search=Opted')
    assert context['total_count'] == 0
    assert 'Opted out retained' not in response.get_data(as_text=True)
    assert 'Opted out retained' not in daily.client.get('/community/export').get_data(as_text=True)
    assert 'Opted out retained' in daily.client.get('/community/export?view=all').get_data(as_text=True)
    assert daily.m.UnsubscribedContact.query.count() == daily.m.CommunityMember.query.count() == 1


def test_current_unique_contacts_do_not_double_count_community_and_rsvps(daily):
    future = event(daily, title='Upcoming gathering', days=2)
    registration(daily, future)
    daily.db.session.add(daily.m.CommunityMember(organization_id=daily.org.id, name='Same person', phone='+17205550145'))
    daily.db.session.commit()
    _, context = context_for(daily, '/dashboard')
    assert (context['community_count'],context['event_registration_count'],context['total_recipients']) == (1,1,1)


def test_authenticated_transport_stop_is_hidden_even_if_negative_table_row_is_missing(daily):
    phone = '+17205550145'
    daily.db.session.add(daily.m.CommunityMember(organization_id=daily.org.id, phone=phone))
    denied = daily.m.RecipientPermissionEvidence(organization_id=daily.org.id, phone=phone, purpose='all', decision='denied',
        source='inbound_stop', evidence_key='SMexplicitstop', occurred_at=daily.now)
    daily.db.session.add(denied)
    daily.db.session.commit()
    with daily.organization_context(daily.org.id):
        assert daily.audience.community_query().count() == 0
    daily.db.session.add(daily.m.RecipientPermissionEvidence(organization_id=daily.org.id, phone=phone, purpose='all', decision='unknown',
        source='inbound_start', evidence_key='SMexplicitstart', occurred_at=daily.now+timedelta(seconds=1)))
    daily.db.session.commit()
    with daily.organization_context(daily.org.id):
        assert daily.audience.community_query().count() == 1
    assert daily.m.RecipientPermissionEvidence.query.count() == 2
    assert daily.m.CommunityMember.query.count() == 1


def test_past_event_history_has_explicit_feedback_composer_without_reapproval(daily):
    past = event(daily, title='Past feedback gathering', days=-5)
    registration(daily, past)
    daily.db.session.commit()
    current = daily.client.get('/events').get_data(as_text=True)
    history = daily.client.get('/events?view=past').get_data(as_text=True)
    assert 'Past feedback gathering' not in current
    assert 'Past feedback gathering' in history
    detail = daily.client.get(f'/events/{past.id}').get_data(as_text=True)
    assert 'Send follow-up / feedback' in detail
    response, context = context_for(daily, f'/dashboard?target=event&event_id={past.id}')
    assert context['selected_past_event'].id == past.id
    assert context['selected_event_id'] == past.id
    assert f'value="{past.id}" data-recipient-count="1" selected' in response.get_data(as_text=True)
    review = daily.client.post('/dashboard/review', data={
        'target':'event', 'event_id':past.id, 'message_body':'How was the gathering? Please share feedback.',
        'request_idempotency_key':uuid4().hex,
    })
    assert review.status_code == 200
    assert review.json['eligible'] == 1
    assert review.json['audience'] == past.title
    with daily.permission_context('event',past.id):
        result = daily.provider.send_message('+17205550145','How was the gathering?',send_kind='blast')
    assert result['success'] is True
    assert daily.provider.client.messages.create.call_count == 1
    daily.db.session.add(daily.m.UnsubscribedContact(organization_id=daily.org.id, phone='+17205550145', source='inbound', reason='STOP'))
    daily.db.session.commit()
    with daily.permission_context('event',past.id):
        result = daily.provider.send_message('+17205550145','Follow-up',send_kind='blast')
    assert result['success'] is False
    assert result['reason'] == 'unsubscribed'
    assert daily.provider.client.messages.create.call_count == 1
    assert daily.m.EventRegistration.query.count() == 1


def test_missing_or_foreign_tenant_never_becomes_a_current_or_explicit_audience(daily):
    foreign = event(daily, title='Private foreign event', days=-1, org=daily.other)
    registration(daily, foreign)
    daily.db.session.commit()
    assert daily.audience.events_query().count() == 0
    assert daily.audience.community_query().count() == 0
    with daily.organization_context(daily.org.id):
        assert daily.audience.event_registrations_query(current_only=False).count() == 0
    assert daily.client.get(f'/dashboard?target=event&event_id={foreign.id}').status_code == 404
    assert daily.client.get(f'/events/{foreign.id}').status_code == 404


def test_ended_synced_pre_event_reminder_expires_without_provider_but_manual_feedback_remains(daily):
    from app.services.aoc_event_sync_service import _automation_key
    from app.services.scheduler_service import send_scheduled_messages
    past = event(daily, title='Ended reminder', days=-1, external_source='aoc-wordpress', external_event_id='45', external_status='publish')
    automatic = daily.m.ScheduledMessage(organization_id=daily.org.id, scheduled_at=daily.now-timedelta(seconds=1),
        message_body='Reminder before event', target='community', status='pending', automation_source='aoc_events',
        automation_kind='one_day', automation_key=_automation_key(past,'one_day'))
    manual = daily.m.ScheduledMessage(organization_id=daily.org.id, scheduled_at=daily.now+timedelta(hours=1),
        message_body='Your feedback matters', target='event', event_id=past.id, status='pending')
    daily.db.session.add_all([automatic,manual])
    daily.db.session.commit()
    assert daily.audience.pre_event_reminder_block_reason(manual) is None
    with patch('app.services.scheduler_service.get_twilio_service', side_effect=AssertionError('Ended reminder must never construct a provider.')):
        send_scheduled_messages(daily.app)
    daily.db.session.expire_all()
    assert daily.db.session.get(daily.m.ScheduledMessage,automatic.id).status == 'expired'
    assert daily.db.session.get(daily.m.ScheduledMessage,manual.id).status == 'pending'
    assert daily.m.Event.query.count() == 1


def test_frozen_manual_past_event_feedback_dispatches_and_rechecks_stop(daily):
    from app.services.scheduler_service import send_scheduled_messages
    selected = event(daily, title='Retained feedback audience', days=-3)
    phone, blocked_phone = '+17205550145', '+17205550146'
    registration(daily,selected,phone)
    registration(daily,selected,blocked_phone)
    daily.db.session.add(daily.m.UnsubscribedContact(organization_id=daily.org.id, phone=blocked_phone, source='inbound', reason='STOP'))
    scheduled = daily.m.ScheduledMessage(organization_id=daily.org.id, scheduled_at=daily.now-timedelta(seconds=1),
        status='pending', message_body='Please share feedback about our event.', target='event', event_id=selected.id,
        test_recipient_snapshot_json=json.dumps([{'phone':phone},{'phone':blocked_phone}]))
    daily.db.session.add(scheduled)
    daily.db.session.commit()
    with patch('app.services.scheduler_service.get_twilio_service',return_value=daily.provider):
        send_scheduled_messages(daily.app)
    daily.db.session.expire_all()
    saved = daily.db.session.get(daily.m.ScheduledMessage,scheduled.id)
    assert saved.status == 'sent'
    log = daily.db.session.get(daily.m.MessageLog,saved.message_log_id)
    assert (log.success_count,log.failure_count,log.event_id) == (1,0,selected.id)
    assert daily.provider.client.messages.create.call_count == 1
    assert daily.provider.client.messages.create.call_args.kwargs['to'] == phone
    assert daily.m.EventRegistration.query.count() == 2
    assert daily.m.UnsubscribedContact.query.count() == 1


def test_reminder_is_rechecked_between_recipients_and_preserves_first_acceptance(daily, monkeypatch):
    from app.services.aoc_event_sync_service import _automation_key
    selected = event(daily, title='Ends during dispatch', external_source='aoc-wordpress', external_event_id='46',
                     external_status='publish', external_end_at=daily.now+timedelta(seconds=1))
    phones = ['+17205550145','+17205550146']
    daily.db.session.add_all([daily.m.CommunityMember(organization_id=daily.org.id, phone=phone) for phone in phones])
    scheduled = daily.m.ScheduledMessage(organization_id=daily.org.id, scheduled_at=daily.now, status='processing',
        message_body='Current reminder', target='community', automation_source='aoc_events', automation_kind='day_of',
        automation_key=_automation_key(selected,'day_of'))
    daily.db.session.add(scheduled)
    daily.db.session.commit()
    clock = [daily.now]
    monkeypatch.setattr(daily.audience,'utc_now',lambda:clock[0])
    def accept_first(**kwargs):
        clock[0] = daily.now+timedelta(seconds=2)
        return SimpleNamespace(sid='SM'+'f'*32, status='queued', account_sid='ACfixture', num_segments='1')
    daily.provider.client.messages.create.side_effect = accept_first
    with daily.organization_context(daily.org.id), daily.permission_context('community'):
        result = daily.provider.send_bulk_with_attempts([{'phone':phone} for phone in phones], 'Current reminder',
            'daily-boundary',None,scheduled.id,0,'blast')
    assert result['success_count'] == 1
    assert result['not_submitted_count'] == 1
    assert result['halted_status'] == 'expired'
    assert daily.provider.client.messages.create.call_count == 1
    daily.db.session.expire_all()
    attempts = daily.m.MessageDispatchAttempt.query.order_by(daily.m.MessageDispatchAttempt.id).all()
    assert [(row.status,row.provider_message_sid) for row in attempts] == [('sent','SM'+'f'*32),('failed',None)]
    assert daily.db.session.get(daily.m.ScheduledMessage,scheduled.id).status == 'expired'

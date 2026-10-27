"""Lossless forms, bounded inbox, current invitations and honest readiness."""
from datetime import timedelta
import importlib
import json
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event


@pytest.fixture
def ux(tmp_path, monkeypatch):
    root = os.environ.get('AUDIT_UX_DATABASE_ROOT')
    name = None
    if root:
        if root != 'postgresql://audit@127.0.0.1:55432/postgres':
            raise RuntimeError('UX tests accept only the isolated loopback PostgreSQL audit cluster.')
        import psycopg
        from psycopg import sql
        name = 'ux_case_' + uuid4().hex
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(name)))
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'ux.db')
    for key, value in {
        'DATABASE_URL': uri, 'SAAS_MODE': '1', 'FLASK_DEBUG': '1', 'SCHEDULER_ENABLED': '0',
        'SECRET_KEY': 'isolated-ux-secret', 'AUTH_ALERTS_ENABLED': '0', 'PYTHON_DOTENV_DISABLED': '1',
        'TWILIO_BROWSER_FAKE_SENDS': '1', 'TWILIO_ACCOUNT_SID': 'ACisolatedux',
        'TWILIO_AUTH_TOKEN': 'isolated-ux-token', 'REDIS_URL': 'redis://127.0.0.1:1/0',
    }.items():
        monkeypatch.setenv(key, value)
    import app.config
    importlib.reload(app.config)
    from app import create_app, db, models
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    assert app.config['SQLALCHEMY_DATABASE_URI'] == uri
    with app.app_context():
        try:
            db.create_all()
            org = models.Organization(name='UX fixture', slug='ux-fixture', status='active')
            other = models.Organization(name='Other UX tenant', slug='ux-other', status='active')
            db.session.add_all([org, other])
            db.session.flush()
            db.session.add_all([
                models.OrganizationSubscription(organization=org, status='complimentary'),
                models.OrganizationMessagingProfile(organization=org, provider_mode='platform_managed',
                    status='active', provider_status='active', from_number='+17205550901',
                    twilio_subaccount_sid='ACisolatedux', messaging_service_sid='MGisolatedux',
                    phone_number_sid='PNisolatedux', sender_review_status='approved'),
            ])
            users = {}
            for index, role in enumerate(['owner', 'staff', 'platform']):
                user = models.AppUser(username='ux-' + role, phone=f'+1720555091{index}', role='admin',
                    is_platform_admin=role == 'platform', must_change_password=False)
                user.set_password('Isolated-ux-pass1!')
                db.session.add(user)
                db.session.flush()
                if role != 'platform':
                    db.session.add(models.OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
                users[role] = user.id
            db.session.commit()
            fixture = SimpleNamespace(app=app, db=db, models=models, org_id=org.id, other_id=other.id, users=users, client=app.test_client())
            yield fixture
        finally:
            db.session.remove()
            db.engine.dispose()
    if name:
        import psycopg
        from psycopg import sql
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def login(ux, role='owner'):
    ux.client = ux.app.test_client()
    response = ux.client.post('/platform/login' if role == 'platform' else '/login', data={'username': 'ux-' + role, 'password': 'Isolated-ux-pass1!'})
    assert response.status_code == 302


@pytest.mark.parametrize('phone', ['', 'not a phone', '+17205550801'])
def test_contact_validation_preserves_raw_fields_and_creates_nothing(ux, phone):
    ux.db.session.add(ux.models.CommunityMember(organization_id=ux.org_id, phone='+17205550801', name='Existing'))
    ux.db.session.commit()
    login(ux)
    response = ux.client.post('/community/add', data={'name': 'Keep this name', 'phone': phone})
    assert response.status_code == 200
    assert b'value="Keep this name"' in response.data
    assert f'value="{phone}"'.encode() in response.data
    assert b'aria-invalid="true"' in response.data and b'id="phone-error"' in response.data
    assert ux.models.CommunityMember.query.count() == 1


def test_contact_edit_preserves_attempted_values_without_changing_saved_record(ux):
    member = ux.models.CommunityMember(organization_id=ux.org_id, name='Original', phone='+17205550802')
    ux.db.session.add(member)
    ux.db.session.commit()
    login(ux)
    response = ux.client.post(f'/community/{member.id}/edit', data={'name': 'Changed name', 'phone': 'bad phone'})
    assert b'value="Changed name"' in response.data and b'value="bad phone"' in response.data
    ux.db.session.refresh(member)
    assert (member.name, member.phone) == ('Original', '+17205550802')


@pytest.mark.parametrize('questions', ['\n'.join(f'Question {i}' for i in range(11)), 'x' * 321])
def test_survey_rejects_unsupported_questions_without_losing_text_or_creating_event(ux, questions):
    login(ux)
    response = ux.client.post('/inbox/surveys/add', data={
        'name': 'Retained survey', 'trigger_keyword': 'RETAIN', 'questions': questions,
        'intro_message': 'Keep this intro', 'event_link_mode': 'new', 'new_event_title': 'Must not be created',
    })
    assert response.status_code == 200
    assert questions.encode() in response.data
    assert b'Keep this intro' in response.data
    assert ux.models.SurveyFlow.query.count() == 0 and ux.models.Event.query.count() == 0


def test_survey_boundary_values_round_trip_and_invalid_edit_keeps_existing_prompts(ux):
    login(ux)
    questions = ['x' * 320] + [f'Question {i}' for i in range(9)]
    response = ux.client.post('/inbox/surveys/add', data={'name': 'Boundaries', 'trigger_keyword': 'BOUND', 'questions': '\n'.join(questions), 'is_active': 'on'})
    assert response.status_code == 302
    survey = ux.models.SurveyFlow.query.one()
    assert survey.questions == questions
    response = ux.client.post(f'/inbox/surveys/{survey.id}/edit', data={'name': 'Boundaries', 'trigger_keyword': 'BOUND', 'questions': '\n'.join(questions + ['Eleventh question'])})
    assert b'Eleventh question' in response.data
    ux.db.session.refresh(survey)
    assert survey.questions == questions


def thread_with_history(ux, count=205):
    thread = ux.models.InboxThread(organization_id=ux.org_id, phone='+17205550803')
    ux.db.session.add(thread)
    ux.db.session.flush()
    stamp = ux.models.utc_now().replace(tzinfo=None)
    messages = [ux.models.InboxMessage(organization_id=ux.org_id, thread_id=thread.id, phone=thread.phone,
        direction='inbound', body=f'History message {i:04d}', created_at=stamp) for i in range(count)]
    ux.db.session.add_all(messages)
    ux.db.session.commit()
    return thread, messages


def displayed_message_ids(response):
    return [int(value) for value in re.findall(rb'name="message_ids"\s+value="(\d+)"', response.data)]


def test_inbox_history_is_bounded_and_keyset_pages_have_no_missing_or_duplicate_messages(ux):
    thread, messages = thread_with_history(ux)
    login(ux)
    latest = ux.client.get(f'/inbox?thread={thread.id}')
    assert displayed_message_ids(latest) == [message.id for message in messages[-100:]]
    older = ux.client.get(f'/inbox?thread={thread.id}&before={messages[-100].id}')
    assert displayed_message_ids(older) == [message.id for message in messages[5:105]]
    oldest = ux.client.get(f'/inbox?thread={thread.id}&before={messages[5].id}')
    assert displayed_message_ids(oldest) == [message.id for message in messages[:5]]
    newer = ux.client.get(f'/inbox?thread={thread.id}&after={messages[4].id}')
    assert displayed_message_ids(newer) == [message.id for message in messages[5:105]]
    assert b'Older messages' in latest.data and b'Newer messages' in older.data


def test_inbox_cursor_and_activity_stay_tenant_scoped_and_detect_deletion(ux):
    thread, messages = thread_with_history(ux, count=3)
    other = ux.models.InboxThread(organization_id=ux.other_id, phone='+17205550804')
    ux.db.session.add(other)
    ux.db.session.flush()
    private = ux.models.InboxMessage(organization_id=ux.other_id, thread_id=other.id, phone=other.phone, direction='inbound', body='Private other-tenant text')
    ux.db.session.add(private)
    ux.db.session.commit()
    login(ux)
    assert ux.client.get(f'/inbox?thread={thread.id}&before={private.id}').status_code == 404
    assert ux.client.get(f'/inbox?thread={thread.id}&before=bad').status_code == 400
    first = ux.client.get('/inbox/status').get_json()
    assert first['latest_message_id'] == messages[-1].id and first['message_count'] == 3
    ux.db.session.delete(messages[0])
    ux.db.session.commit()
    second = ux.client.get('/inbox/status').get_json()
    assert second['latest_message_id'] == first['latest_message_id'] and second['message_count'] == 2


def test_accepted_reply_acknowledges_only_original_submitted_form_and_clears_once(ux):
    thread, _ = thread_with_history(ux, count=1)
    login(ux)
    response = ux.client.post(f'/inbox/{thread.id}/reply', data={'body': 'Accepted draft', 'request_idempotency_key': 'draft-proof-1'})
    assert response.status_code == 302
    saved = ux.models.OutboundRequest.query.one()
    assert saved.status == 'accepted'
    page = ux.client.get(f'/inbox?thread={thread.id}')
    assert b'data-acknowledged-key="draft-proof-1"' in page.data
    assert b'data-acknowledged-key=""' in ux.client.get(f'/inbox?thread={thread.id}').data


def test_forbidden_page_keeps_role_appropriate_navigation_and_http_status(ux):
    login(ux, 'staff')
    response = ux.client.get('/billing')
    assert response.status_code == 403
    assert b'Back to workspace' in response.data and b'workspace owner' in response.data
    assert ux.client.get('/billing', headers={'Accept': 'application/json'}).get_json() == {'error': 'Forbidden'}


def pilot(ux, index):
    org = ux.models.Organization(name=f'Pilot {index}', slug=f'pilot-ux-{index}')
    ux.db.session.add(org)
    ux.db.session.flush()
    invite = ux.models.OrganizationInvitation(organization_id=org.id, email=f'owner-{index}@ux.test', role='owner', token=f'old-pilot-{index}', status='revoked')
    ux.db.session.add(invite)
    ux.db.session.flush()
    application = ux.models.PilotApplication(business_name=org.name, contact_name='Pilot owner', email=invite.email,
        use_case='Testing current invitation', status='approved', source_ip_hash='synthetic', organization_id=org.id, owner_invitation_id=invite.id)
    ux.db.session.add(application)
    return org, application, invite


def test_pilot_page_uses_current_owner_invite_preserving_original_history(ux):
    org, application, original = pilot(ux, 1)
    current = ux.models.OrganizationInvitation(organization_id=org.id, email=original.email, role='owner', token='current-owner-link', status='pending', expires_at=ux.models.utc_now() + timedelta(days=1))
    ux.db.session.add(current)
    ux.db.session.commit()
    login(ux, 'platform')
    page = ux.client.get('/platform/pilot-applications')
    assert b'current-owner-link' in page.data and b'old-pilot-1' not in page.data
    ux.db.session.refresh(application)
    assert application.owner_invitation_id == original.id
    current.expires_at = ux.models.utc_now().replace(tzinfo=None) - timedelta(days=1)
    ux.db.session.commit()
    page = ux.client.get('/platform/pilot-applications')
    assert b'current-owner-link' not in page.data and b'Owner invitation expired' in page.data


def test_pilot_query_count_stays_bounded_with_approved_application_growth(ux):
    login(ux, 'platform')
    counts = []
    for size in (1, 10, 25):
        for index in range(ux.models.PilotApplication.query.count(), size):
            org, _application, original = pilot(ux, index)
            ux.db.session.add(ux.models.OrganizationInvitation(organization_id=org.id, email=original.email, role='owner', token=f'current-pilot-{index}', status='pending'))
        ux.db.session.commit()
        ux.db.session.remove()
        queries = []
        def observed(_connection, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith('SELECT'): queries.append(statement)
        event.listen(ux.db.engine, 'before_cursor_execute', observed)
        try:
            with ux.app.app_context():
                page = ux.client.get('/platform/pilot-applications')
        finally:
            event.remove(ux.db.engine, 'before_cursor_execute', observed)
        assert page.status_code == 200
        counts.append(len(queries))
    assert max(counts) <= 12 and max(counts) - min(counts) <= 2, counts


def test_a2p_approval_without_active_sender_does_not_claim_ready(ux):
    from app.services.twilio_a2p_service import describe_a2p_onboarding
    profile = ux.models.OrganizationMessagingProfile.query.one()
    profile.from_number = profile.messaging_service_sid = profile.phone_number_sid = None
    profile.provider_status = 'pending'
    onboarding = ux.models.OrganizationA2POnboarding(organization_id=ux.org_id, onboarding_status='approved', brand_status='approved', campaign_status='approved')
    view = describe_a2p_onboarding(onboarding, profile)
    assert view['stage'] == 'reviewing'
    assert 'sender pending' in view['title']
    assert 'Ready immediately' not in view['eta']
    from app.routes import _launch_readiness_view
    org = ux.db.session.get(ux.models.Organization, ux.org_id)
    with ux.app.test_request_context('/setup'):
        launch = _launch_readiness_view(org, onboarding, profile)
        assert launch['heading'] == 'Await sender assignment' and launch['can_send'] is False
        profile.from_number = '+17205550901'
        profile.provider_status = 'active'
        launch = _launch_readiness_view(org, onboarding, profile)
        assert launch['heading'] == 'Ready for a controlled test' and launch['can_send'] is True


def permitted_contact(ux, phone='+17205550820'):
    from app.services.recipient_permission_service import record_permission_evidence
    ux.db.session.add(ux.models.CommunityMember(organization_id=ux.org_id, name='Reviewed recipient', phone=phone))
    record_permission_evidence(ux.org_id, phone, purpose='community', decision='granted', source='ux_fixture', evidence_key=phone,
        verified=True, metadata={'proof': 'Synthetic explicit permission for controlled send review.'})
    ux.db.session.commit()
    return phone


def review_fields(**changes):
    return dict({'message_body': 'Hello {first_name}', 'target': 'community', 'include_unsubscribe': 'on',
        'require_review': '1', 'request_idempotency_key': 'review-original-key'}, **changes)


def test_send_review_freezes_audience_before_new_directory_additions(ux, monkeypatch):
    from unittest.mock import MagicMock
    original = permitted_contact(ux)
    login(ux)
    data = review_fields()
    review = ux.client.post('/dashboard/review', data=data)
    assert review.status_code == 200
    assert review.get_json()['eligible'] == 1
    assert ux.models.OutboundRequest.query.count() == 0
    permitted_contact(ux, '+17205550821')
    queue = MagicMock()
    monkeypatch.setattr('app.services.outbound_request_service.get_queue', lambda: queue)
    sent = ux.client.post('/dashboard', data={**data, 'review_token': review.get_json()['review_token']})
    assert sent.status_code == 302
    saved = ux.models.OutboundRequest.query.one()
    assert [row['phone'] for row in json.loads(saved.payload_json)['recipients']] == [original]
    assert ux.models.MessageLog.query.one().total_recipients == 1
    assert queue.enqueue.call_count == 1


def test_confirmed_signup_workspace_does_not_require_individual_reapproval_in_compose_or_review(ux):
    ux.app.config['COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS'] = ['ux-fixture']
    ux.db.session.add(ux.models.CommunityMember(organization_id=ux.org_id, phone='+17205550824', name='Confirmed signup'))
    ux.db.session.commit()
    login(ux)
    assert b'Existing signups do not need individual reapproval' in ux.client.get('/dashboard').data
    response = ux.client.post('/dashboard/review', data=review_fields())
    assert response.status_code == 200 and response.get_json()['eligible'] == 1
    assert 'Existing signups do not need individual reapproval' in response.get_json()['audience_policy']


@pytest.mark.parametrize('change', [{'message_body': 'Changed body'}, {'target': 'event', 'event_id': '999'}, {'schedule_later': 'on', 'schedule_date': '2099-12-31', 'schedule_time': '23:59'}, {'request_idempotency_key': 'different-form-key'}])
def test_send_review_rejects_changed_intent_or_form_key_without_dispatch(ux, change):
    permitted_contact(ux)
    login(ux)
    data = review_fields()
    token = ux.client.post('/dashboard/review', data=data).get_json()['review_token']
    result = ux.client.post('/dashboard', data={**data, **change, 'review_token': token})
    assert result.status_code == 200
    assert b'Review this send again' in result.data or b'Review the message' in result.data or b'Review this send' in result.data
    assert ux.models.OutboundRequest.query.count() == 0 and ux.models.ScheduledMessage.query.count() == 0


def test_send_review_expiry_and_other_actor_are_rejected(ux):
    from unittest.mock import patch
    permitted_contact(ux)
    login(ux)
    data = review_fields()
    with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
        expired = ux.client.post('/dashboard/review', data=data).get_json()['review_token']
    result = ux.client.post('/dashboard', data={**data, 'review_token': expired})
    assert b'review expired' in result.data and b'Hello {first_name}' in result.data
    token = ux.client.post('/dashboard/review', data=data).get_json()['review_token']
    login(ux, 'staff')
    result = ux.client.post('/dashboard', data={**data, 'review_token': token})
    assert b'signed-in account changed' in result.data
    assert ux.models.OutboundRequest.query.count() == 0


def test_send_review_rechecks_withdrawal_without_replacing_frozen_audience(ux):
    from app.services.recipient_permission_service import record_permission_evidence
    phone = permitted_contact(ux)
    login(ux)
    data = review_fields()
    token = ux.client.post('/dashboard/review', data=data).get_json()['review_token']
    record_permission_evidence(ux.org_id, phone, purpose='community', decision='denied', source='later_refusal', evidence_key='withdrawn')
    ux.db.session.commit()
    permitted_contact(ux, '+17205550822')
    result = ux.client.post('/dashboard', data={**data, 'review_token': token})
    assert result.status_code == 200 and b'No eligible recipients' in result.data
    assert ux.models.OutboundRequest.query.count() == 0


def test_native_review_fallback_and_edit_preserve_original_composition(ux):
    permitted_contact(ux)
    login(ux)
    data = review_fields(schedule_later='on', schedule_date='2099-12-31', schedule_time='23:59', client_timezone='America/Denver')
    response = ux.client.post('/dashboard', data=data)
    assert response.status_code == 200 and b'Confirm schedule' in response.data
    assert b'America/Denver' in response.data and b'Reviewed' in response.data
    assert ux.models.ScheduledMessage.query.count() == 0
    token = re.search(rb'name="review_token" value="([^"]+)"', response.data).group(1).decode()
    edit = ux.client.post('/dashboard', data={**data, 'review_token': token, 'edit_review': '1'})
    assert b'Hello {first_name}' in edit.data and b'value="2099-12-31"' in edit.data
    assert b'value="23:59"' in edit.data


def test_reviewed_schedule_keeps_frozen_snapshot_and_existing_limits(ux):
    phone = permitted_contact(ux)
    login(ux)
    data = review_fields(schedule_later='on', schedule_date='2099-12-31', schedule_time='23:59', client_timezone='UTC')
    token = ux.client.post('/dashboard/review', data=data).get_json()['review_token']
    permitted_contact(ux, '+17205550823')
    response = ux.client.post('/dashboard', data={**data, 'review_token': token})
    assert response.status_code == 302
    scheduled = ux.models.ScheduledMessage.query.one()
    assert [row['phone'] for row in json.loads(scheduled.test_recipient_snapshot_json)] == [phone]
    ux.app.config['SEND_MAX_RECIPIENTS'] = 1
    review = ux.client.post('/dashboard/review', data=review_fields(request_idempotency_key='too-many'))
    assert review.status_code == 400
    assert ux.models.OutboundRequest.query.count() == 0


def test_repeated_owner_recovery_keeps_all_history_and_latest_pilot_link(ux):
    org, application, original = pilot(ux, 70)
    original.status = 'pending'
    ux.db.session.commit()
    login(ux, 'platform')
    path = f'/platform/organizations/{org.id}/access/reissue-owner-invite'
    for _ in range(3):
        assert ux.client.post(path, data={'owner_email': original.email}).status_code == 302
    invitations = ux.models.OrganizationInvitation.query.filter_by(organization_id=org.id).order_by(ux.models.OrganizationInvitation.id).all()
    assert len(invitations) == 4
    assert [row.status for row in invitations] == ['revoked', 'revoked', 'revoked', 'pending']
    assert len({row.token for row in invitations}) == 4
    assert application.owner_invitation_id == original.id
    page = ux.client.get('/platform/pilot-applications')
    assert invitations[-1].token.encode() in page.data and original.token.encode() not in page.data


def test_postgresql_invitation_constraint_migration_preserves_rows_and_pending_uniqueness(ux):
    if not os.environ.get('AUDIT_UX_DATABASE_ROOT'):
        pytest.skip('Existing PostgreSQL constraint replacement requires the isolated PostgreSQL fixture.')
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError
    import logging

    org, application, original = pilot(ux, 71)
    ux.db.session.commit()
    with ux.db.engine.begin() as connection:
        connection.execute(text('DROP INDEX ux_org_invitations_pending_email'))
        connection.execute(text('ALTER TABLE organization_invitations ADD CONSTRAINT ux_org_invitations_org_email_status UNIQUE (organization_id, email, status)'))
        before = connection.execute(text('SELECT id,organization_id,email,token,status FROM organization_invitations ORDER BY id')).all()
        migration = importlib.import_module('app.saas_migrations.025_pending_invitation_uniqueness')
        migration.apply(connection, logging.getLogger('ux-migration'))
        migration.apply(connection, logging.getLogger('ux-migration'))
        after = connection.execute(text('SELECT id,organization_id,email,token,status FROM organization_invitations ORDER BY id')).all()
        assert before == after
        assert not any(row['name'] == 'ux_org_invitations_org_email_status' for row in inspect(connection).get_unique_constraints('organization_invitations'))
    ux.db.session.add_all([
        ux.models.OrganizationInvitation(organization_id=org.id, email=original.email, role='owner', status='revoked'),
        ux.models.OrganizationInvitation(organization_id=org.id, email=original.email, role='owner', status='pending'),
    ])
    ux.db.session.commit()
    with pytest.raises(IntegrityError), ux.db.session.begin_nested():
        ux.db.session.add(ux.models.OrganizationInvitation(organization_id=org.id, email=original.email, role='owner', status='pending'))
        ux.db.session.flush()
    assert ux.db.session.get(ux.models.PilotApplication, application.id).owner_invitation_id == original.id
    assert ux.models.OrganizationInvitation.query.filter_by(organization_id=org.id, status='revoked').count() == 2


def test_postgresql_concurrent_owner_reissues_keep_one_current_invitation(ux):
    if not os.environ.get('AUDIT_UX_DATABASE_ROOT'):
        pytest.skip('Owner recovery serialization requires PostgreSQL row locks.')
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    org, application, original = pilot(ux, 72)
    original.status = 'pending'
    ux.db.session.commit()
    org_id, email, original_id, application_id = org.id, original.email, original.id, application.id
    barrier = Barrier(2)

    def reissue():
        with ux.app.app_context():
            client = ux.app.test_client()
            response = client.post('/platform/login', data={'username': 'ux-platform', 'password': 'Isolated-ux-pass1!'})
            assert response.status_code == 302
            barrier.wait(timeout=10)
            try:
                return client.post(f'/platform/organizations/{org_id}/access/reissue-owner-invite', data={'owner_email': email}).status_code
            finally:
                ux.db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(lambda _: reissue(), range(2))) == [302, 302]
    ux.db.session.expire_all()
    invitations = ux.models.OrganizationInvitation.query.filter_by(organization_id=org_id).all()
    assert len(invitations) == 3
    assert sum(row.status == 'pending' for row in invitations) == 1
    assert sum(row.status == 'revoked' for row in invitations) == 2
    assert ux.db.session.get(ux.models.PilotApplication, application_id).owner_invitation_id == original_id

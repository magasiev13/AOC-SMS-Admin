"""Staff data access is a server policy, independent of hidden UI controls."""
from copy import deepcopy
import base64
import json
import importlib
import os
import re
import time
from datetime import timedelta
from html import unescape
from urllib.parse import unquote
from uuid import uuid4
import zlib
from types import SimpleNamespace

import pytest
from flask import Flask, flash, jsonify, redirect
from flask_login import LoginManager, UserMixin, login_required

from app.services.contact_privacy_service import (
    HIDDEN_PHONE_LABEL,
    can_view_phone_numbers,
    contact_display_label,
    redact_phone_numbers,
    redact_private_json,
    require_contact_export_permission,
    staff_phone_privacy_applies,
)
from app.tenant import organization_context


def actor(*, role='admin', membership_role=None, organization_id=1, platform=False, authenticated=True):
    membership = (SimpleNamespace(role=membership_role, organization_id=organization_id)
                  if membership_role is not None else None)
    return SimpleNamespace(is_authenticated=authenticated, is_platform_admin=platform,
                           role=role, effective_role=role, organization_membership=membership)


@pytest.fixture
def policy_app():
    app = Flask('contact-privacy-unit')
    app.config.update(SECRET_KEY='isolated-contact-privacy', SAAS_MODE=True, TESTING=True)
    return app


@pytest.mark.parametrize('global_role', ['admin', 'social_manager', 'owner', 'unknown'])
def test_staff_membership_overrides_every_legacy_role(policy_app, global_role):
    with policy_app.app_context(), organization_context(1):
        staff = actor(role=global_role, membership_role='staff')
        assert can_view_phone_numbers(staff) is False
        assert staff_phone_privacy_applies(staff) is True


def test_owner_must_belong_to_selected_tenant_and_unknown_membership_fails_closed(policy_app):
    with policy_app.app_context(), organization_context(1):
        assert can_view_phone_numbers(actor(membership_role='owner')) is True
        assert can_view_phone_numbers(actor(membership_role='owner', organization_id=2)) is False
        assert can_view_phone_numbers(actor(role='admin')) is False
        assert can_view_phone_numbers(actor(membership_role='admin')) is False
        assert can_view_phone_numbers(actor(membership_role='owner', authenticated=False)) is False


def test_platform_permission_does_not_depend_on_accidental_membership(policy_app):
    with policy_app.app_context():
        assert can_view_phone_numbers(actor(platform=True)) is True
        assert staff_phone_privacy_applies(actor(platform=True)) is False


@pytest.mark.parametrize('role,allowed', [('admin', True), ('social_manager', False), ('staff', False), ('owner', False), ('unknown', False)])
def test_legacy_mode_retains_only_its_existing_admin_role(policy_app, role, allowed):
    policy_app.config['SAAS_MODE'] = False
    with policy_app.app_context():
        assert can_view_phone_numbers(actor(role=role)) is allowed


def test_missing_request_or_configuration_never_assumes_owner_authority():
    assert can_view_phone_numbers() is False
    assert can_view_phone_numbers(actor(membership_role='owner')) is False
    assert staff_phone_privacy_applies() is False


@pytest.mark.parametrize('value', [
    '+17205550123', '7205550123', '(720) 555-0123', '+1 (720) 555-0123',
    '720.555.0123', '720/555/0123', '00 44 20 7946 0018',
    '+442079460018', '555-0123', '+1\u00a0(720)\u00a0555\u00a00123',
    '７２０５５５０１２３', '720-555-0123 ext. 42', 'tel:+17205550123',
    '720\u202f555\u202f0123', '720\u2009555\u20090123', '720\u200b555\u200b0123',
])
def test_freeform_numbers_and_extensions_are_removed(value):
    result = redact_phone_numbers(value)
    assert HIDDEN_PHONE_LABEL in result
    assert not any(character.isdecimal() for character in result), result


def test_freeform_multiple_numbers_redact_without_mutating_adjacent_text():
    assert redact_phone_numbers('Call +17205550123 or (303) 555-0199. Thanks!') == (
        'Call [number hidden] or [number hidden]. Thanks!'
    )


@pytest.mark.parametrize('value', [
    '2026-10-27', '2026-10-27T22:14:30Z', '2026-10-27 22:14:30.123456+00:00',
    '10/27/2026', 'Contact #42', '12 guests', '5 of 10',
    'abc1234567def', 'request_1234567',
])
def test_dates_identifiers_and_counts_are_not_display_phone_fragments(value):
    assert redact_phone_numbers(value) == value


@pytest.mark.parametrize('text', ['Call3035550123now', 'Recipient+17205550123failed', 'Call(303)555-0123now'])
def test_long_phone_sequences_embedded_in_freeform_text_do_not_bypass_redaction(text):
    hidden = redact_phone_numbers(text)
    assert HIDDEN_PHONE_LABEL in hidden
    assert not any(character.isdecimal() for character in hidden)


def test_contact_label_uses_name_or_record_identity_and_never_phone_fallback(policy_app):
    staff = actor(membership_role='staff')
    owner = actor(membership_role='owner')
    with policy_app.app_context(), organization_context(1):
        assert contact_display_label('  Alice  ', 12, user=staff) == 'Alice'
        assert contact_display_label('', 12, user=staff) == 'Contact #12'
        assert contact_display_label('+17205550123', 12, user=staff) == 'Contact #12'
        assert contact_display_label('Alice (720) 555-0123', 12, user=staff) == 'Alice [number hidden]'
        assert contact_display_label('+17205550123', 12, user=owner) == '+17205550123'
        assert contact_display_label(None, None, kind='Respondent', user=staff) == 'Respondent'


def test_structured_redaction_omits_phone_fields_aliases_phone_keys_and_preserves_machine_values():
    source = {
        'id': 1234567890,
        'count': 1234567890,
        'csrf_token': 'csrf-12345678901-protected',
        'review_token': 'opaque-12345678901.protected',
        'request_id': '12345678901',
        'form_key': 'form-12345678901-exact',
        'request_idempotency_key': 'send-12345678901-exact',
        'acknowledged_reply_key': 'reply-12345678901-exact',
        'reply_draft_context': 'draft-12345678901-exact',
        'job_id': 'job-12345678901-exact',
        'created_at': '2026-10-27T22:14:30Z',
        'phone': '+17205550123',
        'normalized_phone': '+17205550123',
        'from_number': '+13035550199',
        'rows': [{'name': 'Alice +17205550123', 'answer': 'Call (303) 555-0199', 'phone': '+17205550123'}],
        'history': {'+17205550123': [{'id': 3}], '+13035550199': [{'id': 4}]},
    }
    before = deepcopy(source)
    result = redact_private_json(source)
    assert source == before
    assert not {'phone', 'normalized_phone', 'from_number'} & result.keys()
    assert result['rows'] == [{'name': 'Alice [number hidden]', 'answer': 'Call [number hidden]'}]
    assert list(result['history']) == ['contact_1', 'contact_2']
    for name in ['id', 'count', 'csrf_token', 'review_token', 'request_id', 'created_at',
                 'form_key', 'request_idempotency_key', 'acknowledged_reply_key', 'reply_draft_context', 'job_id']:
        assert result[name] == source[name]
    assert '+17205550123' not in str(result) and '+13035550199' not in str(result)


def test_structured_redaction_does_not_trust_nested_values_as_opaque_tokens():
    assert redact_private_json({'token': {'phone': '+17205550123', 'body': 'Call +13035550199'}}) == {
        'token': {'body': 'Call [number hidden]'},
    }


def test_export_decorator_denies_before_query_or_stream_factory(policy_app):
    manager = LoginManager(policy_app)
    users = {}

    class User(UserMixin):
        def __init__(self, identifier, membership_role):
            self.id = identifier
            self.role = 'admin'
            self.is_platform_admin = False
            self.organization_membership = SimpleNamespace(role=membership_role, organization_id=1)

    users['staff'] = User('staff', 'staff')
    users['owner'] = User('owner', 'owner')
    manager.user_loader(users.get)
    calls = []

    @policy_app.get('/export')
    @require_contact_export_permission
    def export():
        calls.append('queried')
        return jsonify({'phone': '+17205550123'})

    client = policy_app.test_client()
    assert client.get('/export').status_code == 401
    with client.session_transaction() as session:
        session['_user_id'] = 'staff'
        session['_fresh'] = True
    with organization_context(1):
        denied = client.get('/export', headers={'Accept': 'text/csv'})
        assert denied.status_code == 403 and b'+17205550123' not in denied.data
        assert calls == []
        with client.session_transaction() as session:
            session['_user_id'] = 'owner'
        permitted = client.get('/export')
        assert permitted.status_code == 200
        assert calls == ['queried']
        assert permitted.json == {'phone': '+17205550123'}


def _review_values():
    user = actor(membership_role='staff')
    user.id, user.session_nonce = 7, 'isolated-staff-session'
    intent = {'target': 'event', 'event_id': 12, 'message_body': 'How was the celebration?'}
    recipients = [{'phone': '+17205550123', 'name': 'Alice'}, {'phone': '+13035550199', 'name': None}]
    return user, intent, recipients


def publicly_decode_signed_payload(token):
    """The same base64/zlib decoding available to a browser without any key."""
    compressed = token.startswith('.')
    value = token.split('.')[1 if compressed else 0]
    payload = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    if compressed:
        payload = zlib.decompress(payload)
    return json.loads(payload)


def test_staff_review_token_can_be_publicly_decoded_without_disclosing_frozen_contacts(policy_app):
    from app.services.send_review_service import freeze_review, reviewed_recipients
    user, intent, recipients = _review_values()
    with policy_app.app_context(), organization_context(1):
        token = freeze_review(user, 1, 'form-key', intent, recipients)
        # A browser can decode any signed payload.  Test the complete decoded
        # object, including its nested values, rather than just the token text.
        envelope = publicly_decode_signed_payload(token)
        assert set(envelope) == {'version', 'encrypted'}
        assert envelope['version'] == 2
        assert 'recipients' not in envelope and 'intent' not in envelope
        decoded = json.dumps(envelope)
        for private in ['17205550123', '13035550199', 'Alice', 'How was the celebration?']:
            assert private not in decoded
        assert reviewed_recipients(token, user, 1, 'form-key', intent) == recipients


@pytest.mark.parametrize('change', ['signature', 'actor', 'organization', 'form-key', 'intent', 'session'])
def test_staff_review_binding_cannot_change_a_recipient_action(policy_app, change):
    from app.services.send_review_service import freeze_review, reviewed_recipients
    user, intent, recipients = _review_values()
    with policy_app.app_context(), organization_context(1):
        token = freeze_review(user, 1, 'form-key', intent, recipients)
        org_id, form_key = 1, 'form-key'
        if change == 'signature':
            token = ('x' if token[0] != 'x' else 'y') + token[1:]
        elif change == 'actor':
            user.id = 8
        elif change == 'organization':
            org_id = 2
        elif change == 'form-key':
            form_key = 'another-key'
        elif change == 'intent':
            intent = {**intent, 'event_id': 99}
        elif change == 'session':
            user.session_nonce = 'new-staff-session'
        with pytest.raises(ValueError):
            reviewed_recipients(token, user, org_id, form_key, intent)


def test_staff_review_keeps_original_fifteen_minute_expiry(policy_app, monkeypatch):
    from itsdangerous.timed import TimestampSigner
    from app.services.send_review_service import freeze_review, reviewed_recipients
    user, intent, recipients = _review_values()
    with policy_app.app_context(), organization_context(1):
        with monkeypatch.context() as clock:
            clock.setattr(TimestampSigner, 'get_timestamp', lambda self: int(time.time()) - 901)
            token = freeze_review(user, 1, 'form-key', intent, recipients)
        with pytest.raises(ValueError, match='expired'):
            reviewed_recipients(token, user, 1, 'form-key', intent)


def test_old_cleartext_staff_review_must_refresh_but_owner_compatibility_is_retained(policy_app):
    from app.services.send_review_service import _actor, _signer, reviewed_recipients
    user, intent, recipients = _review_values()
    with policy_app.app_context(), organization_context(1):
        old = _signer().dumps({'actor': _actor(user, 1), 'key': 'form-key', 'intent': intent, 'recipients': recipients})
        with pytest.raises(ValueError, match='refreshed'):
            reviewed_recipients(old, user, 1, 'form-key', intent)
        user.organization_membership.role = 'owner'
        assert reviewed_recipients(old, user, 1, 'form-key', intent) == recipients


@pytest.mark.parametrize('encrypted', [None, 5, 'invalid-ciphertext'])
def test_invalid_encrypted_snapshot_is_rejected_before_any_action(policy_app, encrypted):
    from app.services.send_review_service import _signer, reviewed_recipients
    user, intent, _recipients = _review_values()
    with policy_app.app_context(), organization_context(1):
        malformed = _signer().dumps({'version': 2, 'encrypted': encrypted})
        with pytest.raises(ValueError, match='could not be verified'):
            reviewed_recipients(malformed, user, 1, 'form-key', intent)


@pytest.fixture
def staff_workspace(tmp_path, monkeypatch, request):
    """Only a temporary SQLite file or the explicitly owned loopback PG cluster."""
    pg_root = os.environ.get('STAFF_PRIVACY_TEST_POSTGRES_ROOT')
    database_name = None
    if pg_root:
        if pg_root != 'postgresql://audit@127.0.0.1:55432/postgres':
            raise RuntimeError('Staff privacy tests accept only the owned loopback audit PostgreSQL cluster.')
        import psycopg
        from psycopg import sql
        database_name = 'staff_privacy_' + uuid4().hex
        with psycopg.connect(pg_root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
        def drop_database():
            with psycopg.connect(pg_root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database_name)))
        request.addfinalizer(drop_database)
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'staff-privacy.db')
    for name, value in {
        'DATABASE_URL': uri, 'SAAS_MODE': '1', 'FLASK_DEBUG': '1', 'SCHEDULER_ENABLED': '0',
        'SECRET_KEY': 'isolated-staff-privacy-secret', 'AUTH_ALERTS_ENABLED': '0',
        'PYTHON_DOTENV_DISABLED': '1', 'TWILIO_BROWSER_FAKE_SENDS': '1',
        'TWILIO_ACCOUNT_SID': 'ACisolatedprivacy', 'TWILIO_AUTH_TOKEN': 'isolated-privacy-token',
        'REDIS_URL': 'redis://127.0.0.1:1/0',
    }.items():
        monkeypatch.setenv(name, value)
    import app.config
    importlib.reload(app.config)
    def restore_imported_config():
        monkeypatch.undo()
        importlib.reload(app.config)
    request.addfinalizer(restore_imported_config)
    from app import create_app, db, models
    application = create_app(run_startup_tasks=False, start_scheduler=False)
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    assert application.config['SQLALCHEMY_DATABASE_URI'] == uri
    with application.app_context():
        try:
            db.create_all()
            org = models.Organization(name='Privacy fixture +17205550444', slug='armenians-of-colorado', status='active')
            other = models.Organization(name='Other private tenant', slug='privacy-other', status='active')
            db.session.add_all([org, other])
            db.session.flush()
            db.session.add_all([
                models.OrganizationSubscription(organization=org, status='complimentary'),
                models.OrganizationMessagingProfile(organization=org, provider_mode='platform_managed',
                    status='active', provider_status='active', from_number='+17205550000',
                    twilio_subaccount_sid='ACisolatedprivacy', messaging_service_sid='MGisolatedprivacy',
                    phone_number_sid='PNisolatedprivacy', sender_review_status='approved'),
            ])
            users = {}
            for index, role in enumerate(['owner', 'staff']):
                user = models.AppUser(username='privacy-' + role, phone=f'+1720555091{index}',
                                      role='admin', must_change_password=False)
                user.set_password('Isolated-privacy-pass1!')
                db.session.add(user)
                db.session.flush()
                db.session.add(models.OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
                users[role] = user.id
            member = models.CommunityMember(organization_id=org.id, name='Alice +17205550123', phone='+17205550123')
            other_member = models.CommunityMember(organization_id=other.id, name='Other tenant secret', phone='+16175550111')
            event = models.Event(organization_id=org.id, title='Past celebration +17205550444', date=(models.utc_now() - timedelta(days=2)).date(),
                                 sms_location_note='Venue contact +17205550444')
            db.session.add_all([member, other_member, event])
            db.session.flush()
            registration = models.EventRegistration(organization_id=org.id, event_id=event.id, name=None, phone='+13035550199', booking_spaces=2, booking_comment='Booking contact +17205550777', selections_json=json.dumps([{'label': 'Guest contact +17205550888', 'quantity': 1}]))
            thread = models.InboxThread(organization_id=org.id, phone='+13035550199', contact_name=None,
                last_message_preview='Call me at (303) 555-0199', last_direction='inbound')
            db.session.add_all([registration, thread])
            db.session.flush()
            db.session.add(models.InboxMessage(organization_id=org.id, thread_id=thread.id, phone=thread.phone,
                direction='inbound', body='Call me at (303) 555-0199', raw_payload=json.dumps({'From': thread.phone})))
            survey = models.SurveyFlow(organization_id=org.id, name='Past survey +17205550555', trigger_keyword='PRIVACY17205550555', linked_event_id=event.id,
                                      intro_message='For help call +17205550555')
            survey.set_questions(['What did you think? Call +17205550555'])
            db.session.add(survey)
            db.session.flush()
            survey_session = models.SurveySession(organization_id=org.id, survey_id=survey.id, thread_id=thread.id,
                phone=thread.phone, status='completed', completed_at=models.utc_now().replace(tzinfo=None))
            db.session.add(survey_session)
            db.session.flush()
            db.session.add(models.SurveyResponse(organization_id=org.id, survey_id=survey.id, session_id=survey_session.id,
                phone=thread.phone, question_index=0, question_prompt='What did you think?', answer='Please call +13035550199'))
            log = models.MessageLog(organization_id=org.id, message_body='Ask +17205550123 for details', target='event', event_id=event.id,
                status='sent', total_recipients=1, success_count=0, failure_count=1,
                details=json.dumps([{'phone': thread.phone, 'success': False, 'error': 'Cannot reach +13035550199'}]))
            db.session.add(log)
            db.session.flush()
            schedule = models.ScheduledMessage(organization_id=org.id, message_body='Ask +17205550123 for details',
                target='event', event_id=event.id, scheduled_at=models.utc_now().replace(tzinfo=None) + timedelta(days=2), status='failed',
                error_message='Cannot reach +13035550199')
            db.session.add_all([schedule, models.UnsubscribedContact(organization_id=org.id, phone='+17205550222',
                name=None, reason='Request received from +17205550222', source='List +17205550222')])
            internal = models.OrganizationTestRecipient(organization_id=org.id, phone='+17205550333', label='Private test +17205550333')
            internal_other = models.OrganizationTestRecipient(organization_id=other.id, phone='+16175550333', label='Other tenant internal test')
            keyword = models.KeywordAutomationRule(organization_id=org.id, keyword='HELPINFO17205550666', response_body='Call +17205550666 for help')
            db.session.add_all([internal, internal_other, keyword])
            models.InboxMessage.query.filter_by(thread_id=thread.id).one().matched_keyword = keyword.keyword
            db.session.commit()
            yield SimpleNamespace(app=application, db=db, models=models, users=users, org_id=org.id, other_id=other.id,
                member_id=member.id, event_id=event.id, registration_id=registration.id,
                thread_id=thread.id, survey_id=survey.id, log_id=log.id,
                keyword_id=keyword.id,
                internal_test_id=internal.id, other_internal_test_id=internal_other.id,
                private_phones=['+17205550123', '+13035550199', '+17205550222', '+17205550000', '+17205550910', '+17205550911', '+17205550333', '+17205550444', '+17205550555', '+17205550666', '+17205550777', '+17205550888'],
                client=application.test_client())
        finally:
            db.session.remove()
            db.engine.dispose()
def workspace_login(workspace, role='staff'):
    workspace.client = workspace.app.test_client()
    response = workspace.client.post('/login', data={'username': 'privacy-' + role, 'password': 'Isolated-privacy-pass1!'})
    assert response.status_code == 302


def assert_no_private_numbers(workspace, response):
    displayed = unescape(unquote(response.get_data(as_text=True)))
    headers = unquote('\n'.join(str(value) for _, value in response.headers))
    for phone in workspace.private_phones:
        assert phone not in displayed + headers
        assert phone.lstrip('+') not in displayed + headers
        assert phone[-10:] not in displayed + headers
    assert '+16175550111' not in displayed + headers
    assert 'Other tenant secret' not in displayed


@pytest.mark.parametrize('surface', ['dashboard', 'community', 'all-community', 'event', 'inbox', 'threads', 'surveys', 'survey-list', 'keywords', 'security-contact', 'logs', 'log', 'scheduled', 'unsubscribed'])
def test_staff_pages_do_not_ship_numbers_in_rendered_html_or_machine_attributes(staff_workspace, surface):
    w = staff_workspace
    workspace_login(w)
    routes = {
        'dashboard': '/dashboard', 'community': '/community', 'all-community': '/community?view=all',
        'event': f'/events/{w.event_id}', 'inbox': f'/inbox?thread={w.thread_id}',
        'threads': '/inbox?view=threads', 'surveys': f'/inbox/surveys/{w.survey_id}/submissions',
        'survey-list': '/inbox/surveys', 'keywords': '/inbox/keywords', 'security-contact': '/account/security-contact',
        'logs': '/logs', 'log': f'/logs/{w.log_id}', 'scheduled': '/scheduled', 'unsubscribed': '/unsubscribed',
    }
    response = w.client.get(routes[surface])
    assert response.status_code == 200
    assert_no_private_numbers(w, response)
    assert 'no-store' in response.headers.get('Cache-Control', '')
    assert 'Cookie' in response.vary


@pytest.mark.parametrize('export', ['community', 'event', 'survey', 'unsubscribed'])
def test_staff_direct_export_is_denied_including_ajax_and_csv_negotiation(staff_workspace, monkeypatch, export):
    import app.routes as routes
    w = staff_workspace
    workspace_login(w)
    paths = {'community': '/community/export?view=all', 'event': f'/events/{w.event_id}/export',
        'survey': f'/inbox/surveys/{w.survey_id}/submissions/export', 'unsubscribed': '/unsubscribed/export'}
    def query_forbidden(*args, **kwargs):
        raise AssertionError('Unauthorized export reached dataset construction')
    monkeypatch.setattr(routes, '_bounded_export_query_all', query_forbidden)
    monkeypatch.setattr(routes, '_iter_survey_submission_export_rows', query_forbidden)
    for accept in ['text/csv', 'application/json', 'text/html']:
        response = w.client.get(paths[export], headers={'Accept': accept})
        assert response.status_code == 403
        assert_no_private_numbers(w, response)
        assert not response.headers.get('Content-Disposition', '').startswith('attachment')


def test_staff_membership_even_with_global_admin_cannot_enumerate_contact_forms(staff_workspace):
    w = staff_workspace
    workspace_login(w)
    assert w.models.AppUser.query.filter_by(id=w.users['staff']).one().role == 'admin'
    for route in ['/community/add', f'/community/{w.member_id}/edit', '/community/import', '/unsubscribed/add', '/unsubscribed/import', '/users']:
        response = w.client.get(route)
        assert response.status_code == 403
        assert_no_private_numbers(w, response)


def test_owner_still_sees_authorized_contact_and_can_export_only_own_tenant(staff_workspace):
    w = staff_workspace
    workspace_login(w, 'owner')
    community = w.client.get('/community')
    assert community.status_code == 200 and b'+17205550123' in community.data
    for path in ['/community/export?view=all', f'/events/{w.event_id}/export', f'/inbox/surveys/{w.survey_id}/submissions/export', '/unsubscribed/export']:
        response = w.client.get(path)
        assert response.status_code == 200 and response.mimetype == 'text/csv'
        assert response.headers.get('Content-Disposition', '').startswith('attachment')
        assert b'+16175550111' not in response.data and b'Other tenant secret' not in response.data


def test_staff_review_json_and_its_publicly_decodable_token_keep_contacts_private(staff_workspace):
    w = staff_workspace
    workspace_login(w)
    response = w.client.post('/dashboard/review', data={
        'target': 'community', 'message_body': 'Hello {name}', 'request_idempotency_key': 'privacy-review-1',
    })
    assert response.status_code == 200
    assert_no_private_numbers(w, response)
    review = response.get_json()
    public_envelope = publicly_decode_signed_payload(review['review_token'])
    assert public_envelope['version'] == 2 and set(public_envelope) == {'version', 'encrypted'}
    assert 'recipients' not in public_envelope
    assert 'number hidden' in review['sample']


@pytest.mark.parametrize('invalid', ['tampered', 'expired'])
def test_staff_tampered_or_expired_review_never_creates_request_or_queues(staff_workspace, monkeypatch, invalid):
    from itsdangerous.timed import TimestampSigner
    import app.routes as routes
    w = staff_workspace
    workspace_login(w)
    form = {'target': 'community', 'message_body': 'Hello there', 'request_idempotency_key': 'privacy-invalid-review', 'require_review': '1'}
    if invalid == 'expired':
        with monkeypatch.context() as clock:
            clock.setattr(TimestampSigner, 'get_timestamp', lambda self: int(time.time()) - 901)
            review = w.client.post('/dashboard/review', data=form).get_json()
    else:
        review = w.client.post('/dashboard/review', data=form).get_json()
    assert 'review_token' in review
    token = review['review_token']
    if invalid == 'tampered':
        token = ('x' if token[0] != 'x' else 'y') + token[1:]
    calls = []
    def forbidden_queue():
        calls.append('queue')
        raise AssertionError('Invalid review reached queue')
    monkeypatch.setattr(routes, '_get_queue_with_preflight', forbidden_queue)
    response = w.client.post('/dashboard', data={**form, 'review_token': token})
    assert response.status_code in {200, 302}
    assert_no_private_numbers(w, response)
    assert calls == [] and w.models.OutboundRequest.query.count() == 0
    assert w.models.MessageLog.query.count() == 1


def test_staff_phone_redaction_never_changes_persisted_contact_or_message_content(staff_workspace):
    w = staff_workspace
    workspace_login(w)
    w.client.get(f'/inbox?thread={w.thread_id}')
    w.client.get('/community')
    assert w.models.CommunityMember.query.filter_by(id=w.member_id).one().name == 'Alice +17205550123'
    assert w.models.InboxMessage.query.filter_by(thread_id=w.thread_id).one().body == 'Call me at (303) 555-0199'
    registration = w.models.EventRegistration.query.filter_by(id=w.registration_id).one()
    assert registration.name is None and registration.phone == '+13035550199' and registration.booking_spaces == 2


def test_staff_selects_saved_test_recipient_by_scoped_record_id_not_phone(staff_workspace):
    w = staff_workspace
    workspace_login(w)
    form = {'target': 'community', 'message_body': 'Private test message', 'test_mode': 'on',
        'test_recipient_selection_mode': 'one', 'request_idempotency_key': 'privacy-internal-test'}
    response = w.client.post('/dashboard/review', data={**form, 'test_recipient_id': w.internal_test_id})
    assert response.status_code == 200
    assert_no_private_numbers(w, response)
    assert response.get_json()['eligible'] == 1
    for values in [
        {'test_recipient_id': w.other_internal_test_id},
        {'test_recipient_id': 999999},
        {'test_recipient_phone': '+17205550333'},
    ]:
        denied = w.client.post('/dashboard/review', data={**form, **values})
        assert denied.status_code == 400
        assert_no_private_numbers(w, denied)


def test_demoted_owner_uses_membership_on_next_request_and_cannot_export(staff_workspace):
    w = staff_workspace
    workspace_login(w, 'owner')
    first = w.client.get('/community')
    assert b'+17205550123' in first.data
    membership = w.models.OrganizationMembership.query.filter_by(user_id=w.users['owner'], organization_id=w.org_id).one()
    membership.role = 'staff'
    w.db.session.commit()
    second = w.client.get('/community')
    assert second.status_code == 200
    assert_no_private_numbers(w, second)
    assert 'no-store' in second.headers.get('Cache-Control', '')
    assert w.client.get('/community/export?view=all').status_code == 403


def test_staff_number_search_cannot_reflect_numbers_in_body_or_redirect(staff_workspace):
    w = staff_workspace
    workspace_login(w)
    for path in ['/community?search=%2B17205550123', '/inbox?search=%2B13035550199&view=threads',
                 f'/inbox/surveys/{w.survey_id}/submissions?search=%2B13035550199']:
        response = w.client.get(path)
        assert response.status_code in {200, 302, 400}
        assert_no_private_numbers(w, response)


@pytest.mark.parametrize('resource', ['event', 'keyword', 'survey'])
def test_staff_cannot_retrieve_phone_bearing_saved_content_through_editor(staff_workspace, resource):
    w = staff_workspace
    workspace_login(w)
    paths = {'event': f'/events/{w.event_id}/edit', 'keyword': f'/inbox/keywords/{w.keyword_id}/edit',
             'survey': f'/inbox/surveys/{w.survey_id}/edit'}
    response = w.client.get(paths[resource])
    assert response.status_code == 403
    assert_no_private_numbers(w, response)
    data = {
        'event': {'title': 'Changed event', 'date': '2026-10-27', 'sms_location_note': 'Changed note'},
        'keyword': {'keyword': 'HELPINFO', 'response_body': 'Changed reply', 'is_active': 'on'},
        'survey': {'name': 'Changed survey', 'trigger_keyword': 'PRIVACY', 'questions': 'Changed question', 'is_active': 'on'},
    }[resource]
    posted = w.client.post(paths[resource], data=data)
    assert posted.status_code == 403
    assert_no_private_numbers(w, posted)
    assert w.models.Event.query.filter_by(id=w.event_id).one().sms_location_note == 'Venue contact +17205550444'
    assert w.models.KeywordAutomationRule.query.filter_by(id=w.keyword_id).one().response_body == 'Call +17205550666 for help'
    assert w.models.SurveyFlow.query.filter_by(id=w.survey_id).one().intro_message == 'For help call +17205550555'


def test_staff_cannot_overwrite_phone_bearing_thread_name_through_inline_form(staff_workspace):
    w = staff_workspace
    thread = w.models.InboxThread.query.filter_by(id=w.thread_id).one()
    thread.contact_name = 'Bob +13035550199'
    w.db.session.commit()
    workspace_login(w)
    page = w.client.get(f'/inbox?thread={w.thread_id}')
    assert page.status_code == 200
    assert_no_private_numbers(w, page)
    assert b'id="threadContactName"' not in page.data
    response = w.client.post(f'/inbox/threads/{w.thread_id}/update', data={'contact_name': 'Changed'})
    assert response.status_code == 403
    assert w.models.InboxThread.query.filter_by(id=w.thread_id).one().contact_name == 'Bob +13035550199'


@pytest.mark.parametrize('role', ['staff', 'owner'])
def test_signed_browser_session_flashes_follow_contact_privacy_before_serialization(staff_workspace, role):
    w = staff_workspace
    @w.app.post('/privacy-fixture-flash')
    @login_required
    def privacy_fixture_flash():
        flash('Call +13035550199 about the event', 'warning')
        return redirect('/community')

    workspace_login(w, role)
    response = w.client.post('/privacy-fixture-flash')
    assert response.status_code == 302
    cookie = w.client.get_cookie(w.app.config['SESSION_COOKIE_NAME'])
    payload = w.app.session_interface.get_signing_serializer(w.app).loads(cookie.value)
    messages = [message for _category, message in payload['_flashes']]
    if role == 'staff':
        assert messages == ['Call [number hidden] about the event']
        assert '+13035550199' not in json.dumps(payload)
    else:
        assert messages == ['Call +13035550199 about the event']


def test_large_confidential_multipart_review_parses_before_csrf_only_on_dashboard_and_never_dispatches(staff_workspace, monkeypatch):
    import app.routes as routes
    from app.services.send_review_service import freeze_review
    w = staff_workspace
    workspace_login(w)
    w.app.config['WTF_CSRF_ENABLED'] = True
    page = w.client.get('/dashboard')
    assert page.status_code == 200
    csrf = re.search(rb'name="csrf_token"\s+value="([^"]+)"', page.data).group(1).decode()
    user = w.models.AppUser.query.filter_by(id=w.users['staff']).one()
    recipients = [{'phone': f'+1720{5500000 + index:07d}', 'name': f'Synthetic person {index}'} for index in range(5000)]
    intent = {'target': 'community', 'message_body': 'How was the celebration?'}
    token = freeze_review(user, w.org_id, 'large-review', intent, recipients)
    assert len(token) > w.app.config['MAX_FORM_MEMORY_SIZE']
    assert len(token) < min(w.app.config['MAX_CONTENT_LENGTH'], 2 * 1024 * 1024)
    calls = []
    def forbidden_queue():
        calls.append('queue')
        raise AssertionError('Editing or rejecting a review must not dispatch')
    monkeypatch.setattr(routes, '_get_queue_with_preflight', forbidden_queue)
    form = {'csrf_token': csrf, 'target': 'community', 'message_body': intent['message_body'],
            'request_idempotency_key': 'large-review', 'review_token': token, 'edit_review': '1'}
    response = w.client.post('/dashboard', data=form, content_type='multipart/form-data')
    assert response.status_code == 200
    assert_no_private_numbers(w, response)
    assert recipients[0]['phone'].encode() not in response.data
    assert recipients[-1]['phone'].encode() not in response.data
    rejected = w.client.post(f'/inbox/{w.thread_id}/reply', data={**form, 'body': 'Should not send'}, content_type='multipart/form-data')
    assert rejected.status_code == 413
    assert calls == [] and w.models.OutboundRequest.query.count() == 0
    assert w.models.MessageLog.query.count() == 1


def test_booking_notes_and_selections_are_private_display_without_changing_booking(staff_workspace):
    w = staff_workspace
    registration = w.models.EventRegistration.query.filter_by(event_id=w.event_id).one()
    before = (registration.name, registration.phone, registration.booking_spaces,
              registration.booking_comment, registration.selections_json)
    workspace_login(w)
    hidden = w.client.get(f'/events/{w.event_id}')
    assert hidden.status_code == 200
    assert_no_private_numbers(w, hidden)
    workspace_login(w, 'owner')
    visible = w.client.get(f'/events/{w.event_id}')
    assert visible.status_code == 200
    assert b'+17205550777' in visible.data and b'+17205550888' in visible.data
    w.db.session.expire_all()
    assert (registration.name, registration.phone, registration.booking_spaces,
            registration.booking_comment, registration.selections_json) == before


@pytest.mark.parametrize('protected_source', ['event_title', 'keyword'])
def test_feedback_prefill_never_discloses_protected_saved_text_or_changes_its_intent(staff_workspace, protected_source, monkeypatch):
    import app.routes as routes
    w = staff_workspace
    event = w.models.Event.query.filter_by(id=w.event_id).one()
    event.title = 'Celebration +17205550444' if protected_source == 'event_title' else 'Celebration'
    flow = w.models.SurveyFlow(organization_id=w.org_id, name='Event feedback', purpose='event_feedback',
        feedback_event_id=event.id, trigger_keyword='RATE17205550555' if protected_source == 'keyword' else 'RATEPARTY', is_active=True)
    flow.set_questions(['How was the celebration?'])
    w.db.session.add(flow)
    w.db.session.commit()
    before = (event.title, flow.trigger_keyword, flow.questions_json)
    def forbidden_queue():
        raise AssertionError('Opening a protected invitation must not dispatch')
    monkeypatch.setattr(routes, '_get_queue_with_preflight', forbidden_queue)
    workspace_login(w)
    detail = w.client.get(f'/events/{event.id}/feedback/{flow.id}')
    assert detail.status_code == 200
    assert_no_private_numbers(w, detail)
    assert b'An owner needs to prepare this invitation' in detail.data
    assert b'>Review invitation</a>' not in detail.data
    form = {'target': 'event', 'event_id': event.id, 'feedback_id': flow.id}
    for response in [w.client.get('/dashboard', query_string=form), w.client.post('/dashboard', data=form)]:
        assert response.status_code == 403
        assert_no_private_numbers(w, response)
    workspace_login(w, 'owner')
    owner = w.client.get('/dashboard', query_string=form)
    assert owner.status_code == 200
    protected = '+17205550444' if protected_source == 'event_title' else '17205550555'
    assert protected.encode() in owner.data
    assert (event.title, flow.trigger_keyword, flow.questions_json) == before
    assert w.models.OutboundRequest.query.count() == 0 and w.models.MessageLog.query.count() == 1


@pytest.mark.parametrize('role', ['staff', 'owner'])
def test_native_html_send_review_obeys_privacy_and_preserves_sealed_send_intent(staff_workspace, role, monkeypatch):
    import app.routes as routes
    from app.services.send_review_service import _signer, _review_cipher
    w = staff_workspace
    workspace_login(w, role)
    def forbidden_queue():
        raise AssertionError('A native HTML review must not dispatch')
    monkeypatch.setattr(routes, '_get_queue_with_preflight', forbidden_queue)
    form = {'target': 'community', 'message_body': 'Hello {full_name}', 'test_mode': 'on',
            'test_recipient_selection_mode': 'one', 'require_review': '1',
            'request_idempotency_key': 'native-private-review'}
    if role == 'staff':
        form['test_recipient_id'] = str(w.internal_test_id)
    else:
        form['test_recipient_phone'] = '+17205550333'
    response = w.client.post('/dashboard', data=form)
    assert response.status_code == 200 and b'Review send' in response.data
    if role == 'staff':
        assert_no_private_numbers(w, response)
        assert b'[number hidden]' in response.data
    else:
        assert b'+17205550000' in response.data and b'Private test +17205550333' in response.data
    token = unescape(re.search(rb'name="review_token" value="([^"]+)"', response.data).group(1).decode())
    envelope = _signer().loads(token)
    assert set(envelope) == {'version', 'encrypted'}
    payload = json.loads(_review_cipher().decrypt(envelope['encrypted'].encode()))
    assert payload['intent']['message_body'] == 'Hello {full_name}'
    assert payload['recipients'][0]['phone'] == '+17205550333'
    assert w.models.OutboundRequest.query.count() == 0 and w.models.MessageLog.query.count() == 1


@pytest.mark.parametrize('path', ['/setup', '/setup/pending'])
def test_staff_setup_display_does_not_disclose_phone_bearing_organization_name(staff_workspace, path):
    w = staff_workspace
    original = w.models.Organization.query.filter_by(id=w.org_id).one().name
    workspace_login(w)
    response = w.client.get(path, follow_redirects=True)
    assert response.status_code == 200
    assert_no_private_numbers(w, response)
    assert w.models.Organization.query.filter_by(id=w.org_id).one().name == original


@pytest.mark.parametrize('surface,prefix', [
    ('log', 43), ('pending-schedule', 73), ('past-schedule', 43), ('dashboard', 50),
    ('survey-intro', 70), ('keyword-response', 110), ('survey-answer', 110),
    ('inbox-cache', 173), ('feedback-default', 93),
])
def test_complete_phone_is_redacted_before_any_display_preview_cut(staff_workspace, surface, prefix):
    w = staff_workspace
    phone = '+17205550145'
    w.private_phones.append(phone)
    source = 'X' * prefix + phone + 'z' * 30
    if surface in {'log', 'dashboard'}:
        record = w.models.MessageLog.query.filter_by(id=w.log_id).one()
        field, path = 'message_body', '/logs' if surface == 'log' else '/dashboard'
    elif surface in {'pending-schedule', 'past-schedule'}:
        record = w.models.ScheduledMessage.query.one()
        if surface == 'pending-schedule':
            record.status = 'pending'
        field, path = 'message_body', '/scheduled'
    elif surface == 'survey-intro':
        record = w.models.SurveyFlow.query.filter_by(id=w.survey_id).one()
        field, path = 'intro_message', '/inbox/surveys'
    elif surface == 'keyword-response':
        record = w.models.KeywordAutomationRule.query.filter_by(id=w.keyword_id).one()
        field, path = 'response_body', '/inbox/keywords'
    elif surface == 'survey-answer':
        record = w.models.SurveyResponse.query.filter_by(survey_id=w.survey_id).one()
        field, path = 'answer', f'/inbox/surveys/{w.survey_id}/submissions'
    elif surface == 'inbox-cache':
        record = w.models.InboxMessage.query.filter_by(thread_id=w.thread_id).one()
        field, path = 'body', '/inbox?view=threads'
    else:
        record = w.models.Event.query.filter_by(id=w.event_id).one()
        field, path = 'title', f'/events/{w.event_id}/feedback/new'
    setattr(record, field, source)
    if surface == 'inbox-cache':
        # Reproduce both existing cache writers, without changing their stored
        # contract. The staff display must use the complete original message.
        from app.services.inbox_service import _refresh_thread_rollup
        thread = w.models.InboxThread.query.filter_by(id=w.thread_id).one()
        _refresh_thread_rollup(thread)
        assert thread.last_message_preview == source[:180]
    w.db.session.commit()
    workspace_login(w)
    hidden = w.client.get(path)
    assert hidden.status_code == 200
    assert_no_private_numbers(w, hidden)
    # Checking only complete phones misses the original disclosure.
    assert '+172055' not in hidden.get_data(as_text=True)
    workspace_login(w, 'owner')
    owner = w.client.get(path)
    assert owner.status_code == 200
    assert source[:prefix + 7].encode() in owner.data
    w.db.session.expire_all()
    assert getattr(record, field) == source
    if surface == 'inbox-cache':
        assert thread.last_message_preview == source[:180]
    assert w.models.OutboundRequest.query.count() == 0


def test_staff_orphaned_truncated_inbox_cache_fails_closed_without_rewriting_it(staff_workspace):
    w = staff_workspace
    w.models.InboxMessage.query.filter_by(thread_id=w.thread_id).delete()
    thread = w.models.InboxThread.query.filter_by(id=w.thread_id).one()
    original = 'X' * 173 + '+172055'
    thread.last_message_preview = original
    w.db.session.commit()
    workspace_login(w)
    hidden = w.client.get('/inbox?view=threads')
    assert hidden.status_code == 200
    assert '+172055' not in hidden.get_data(as_text=True)
    assert b'Message preview unavailable' in hidden.data
    workspace_login(w, 'owner')
    owner = w.client.get('/inbox?view=threads')
    assert original.encode() in owner.data
    assert thread.last_message_preview == original


def test_staff_preview_reads_latest_complete_body_in_one_tenant_scoped_query(staff_workspace):
    from flask_login import login_user
    from sqlalchemy import event
    from app.services.contact_privacy_service import build_thread_display_previews
    w = staff_workspace
    now = w.models.utc_now().replace(tzinfo=None)
    own = []
    for index in range(10):
        thread = w.models.InboxThread(organization_id=w.org_id, phone=f'+17205559{index:03d}',
            last_message_preview='Cached fragment +172055')
        w.db.session.add(thread)
        w.db.session.flush()
        # An older message has the higher ID, so max(id) would choose wrongly.
        w.db.session.add(w.models.InboxMessage(organization_id=w.org_id, thread_id=thread.id,
            phone=thread.phone, direction='inbound', body=f'Latest #{index} +17205550145', created_at=now))
        w.db.session.flush()
        w.db.session.add(w.models.InboxMessage(organization_id=w.org_id, thread_id=thread.id,
            phone=thread.phone, direction='inbound', body='Older content must not be the preview',
            created_at=now - timedelta(hours=2)))
        own.append(thread)
    foreign = w.models.InboxThread(organization_id=w.other_id, phone='+16175550444',
        last_message_preview='Other tenant cached content')
    w.db.session.add(foreign)
    w.db.session.flush()
    w.db.session.add(w.models.InboxMessage(organization_id=w.other_id, thread_id=foreign.id,
        phone=foreign.phone, direction='inbound', body='Other tenant full content', created_at=now))
    w.db.session.commit()
    user = w.models.AppUser.query.filter_by(id=w.users['staff']).one()
    statements = []
    def count_selects(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    with w.app.test_request_context('/inbox'), organization_context(w.org_id):
        login_user(user)
        assert can_view_phone_numbers() is False  # Warm the membership relationship.
        event.listen(w.db.engine, 'before_cursor_execute', count_selects)
        try:
            previews = build_thread_display_previews([*own, foreign])
        finally:
            event.remove(w.db.engine, 'before_cursor_execute', count_selects)
    assert len(statements) == 1
    assert foreign.id not in previews
    assert len(previews) == len(own)
    for index, thread in enumerate(own):
        assert previews[thread.id] == f'Latest #{index} [number hidden]'
        assert thread.last_message_preview == 'Cached fragment +172055'

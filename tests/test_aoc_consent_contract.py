"""The confirmed AOC community sign-up contract, including actual opt-outs."""
import importlib
import json
import os
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


@pytest.fixture
def aoc(tmp_path, monkeypatch):
    root = os.environ.get('AUDIT_AOC_DATABASE_ROOT')
    database_name = None
    if root:
        assert root == 'postgresql://audit@127.0.0.1:55432/postgres'
        import psycopg
        from psycopg import sql
        database_name = 'aoc_contract_' + uuid4().hex
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'aoc.db')
    for key, value in {
        'DATABASE_URL': uri, 'SAAS_MODE': '1', 'FLASK_DEBUG': '1', 'SCHEDULER_ENABLED': '0',
        'PYTHON_DOTENV_DISABLED': '1', 'SECRET_KEY': 'aoc-contract-test-secret',
        'COMMUNITY_SIGNUP_CONSENT_ORGANIZATION_SLUGS': 'armenians-of-colorado',
    }.items():
        monkeypatch.setenv(key, value)
    import app.config
    importlib.reload(app.config)
    from app import create_app, db, models
    from app.services import recipient_permission_service as permission
    from app.services.twilio_service import TwilioService
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    try:
        with app.app_context():
            db.create_all()
            org = models.Organization(name='Armenians of Colorado', slug='armenians-of-colorado')
            other = models.Organization(name='Another organization', slug='another-community')
            user = models.AppUser(username='aoc-owner', email='owner@aoc.test', phone='+17205550121', role='admin', must_change_password=False)
            user.set_password('Aoc-owner-pass1!')
            db.session.add_all([org, other, user])
            db.session.flush()
            db.session.add_all([
                models.OrganizationMembership(organization_id=org.id, user_id=user.id, role='owner'),
                models.OrganizationSubscription(organization=org, status='complimentary'),
                models.OrganizationMessagingProfile(organization=org, status='active', provider_status='active',
                    sender_review_status='approved', from_number='+17205550999', phone_number_sid='PNfixture',
                    twilio_subaccount_sid='ACfixture', messaging_service_sid='MGfixture'),
            ])
            phone = '+17205550145'
            member = models.CommunityMember(organization=org, phone=phone, name='Confirmed signup')
            db.session.add_all([member, models.CommunityMember(organization=other, phone=phone, name='Other tenant')])
            db.session.commit()
            provider = TwilioService.__new__(TwilioService)
            provider.organization_id = org.id
            provider.account_sid = 'ACfixture'
            provider.messaging_service_sid = 'MGfixture'
            provider.from_number = '+17205550999'
            provider.client = MagicMock()
            provider.client.messages.create.return_value = SimpleNamespace(sid='SM' + 'a' * 32, status='queued', account_sid='ACfixture', num_segments='1')
            yield SimpleNamespace(app=app, db=db, models=models, org=org, other=other, user=user, member=member,
                                  phone=phone, permission=permission, provider=provider, client=app.test_client())
            db.session.remove()
            db.engine.dispose()
    finally:
        if database_name:
            import psycopg
            from psycopg import sql
            with psycopg.connect(root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database_name)))


def evidence(fixture, decision, *, purpose='community', source='owner_review', second=0, metadata=None):
    fixture.permission.record_permission_evidence(
        fixture.org.id, fixture.phone, purpose=purpose, decision=decision, source=source,
        evidence_key=uuid4().hex, occurred_at=fixture.models.utc_now() + timedelta(seconds=second),
        verified=decision == 'granted', metadata=metadata or {'proof': 'Retained explicit sign-up or withdrawal'},
    )
    fixture.db.session.commit()


def test_confirmed_signups_need_no_individual_reapproval_or_fabricated_backfill(aoc):
    status = aoc.permission.recipient_permission_status(aoc.org.id, aoc.phone)
    assert status.allowed and status.source_label
    assert aoc.models.RecipientPermissionEvidence.query.count() == 0
    assert not aoc.permission.recipient_permission_status(aoc.other.id, aoc.phone).allowed
    assert not aoc.permission.recipient_permission_status(aoc.org.id, '+17205550199').allowed


@pytest.mark.parametrize('signal', [False, None])
def test_synthetic_or_missing_booking_flag_does_not_revoke_broad_community_optin(aoc, signal):
    evidence(aoc, 'denied' if signal is False else 'unknown', source='event_sync:wordpress:booking-1',
             metadata={'sms_consent': signal, 'community_sms_consent': None})
    assert aoc.permission.recipient_permission_status(aoc.org.id, aoc.phone).allowed
    assert aoc.provider.send_message(aoc.phone, 'Community update', send_kind='blast')['success']
    aoc.provider.client.messages.create.assert_called_once()


def test_event_signup_also_authorizes_community_but_not_an_unrelated_event_audience(aoc):
    event = aoc.models.Event(organization=aoc.org, title='Community event')
    other_event = aoc.models.Event(organization=aoc.other, title='Other tenant event')
    signup_phone = '+17205550146'
    aoc.db.session.add_all([event, other_event])
    aoc.db.session.flush()
    aoc.db.session.add(aoc.models.EventRegistration(organization_id=aoc.org.id, event_id=event.id, phone=signup_phone))
    aoc.db.session.commit()
    assert aoc.permission.recipient_permission_status(aoc.org.id, signup_phone).allowed
    assert aoc.permission.recipient_permission_status(aoc.org.id, signup_phone, purpose='event', event_id=event.id).allowed
    assert not aoc.permission.recipient_permission_status(aoc.org.id, signup_phone, purpose='event', event_id=other_event.id).allowed


def test_STOP_blocks_and_START_restores_existing_AOC_signup_consent(aoc):
    evidence(aoc, 'denied', purpose='all', source='inbound_stop')
    blocked = aoc.provider.send_message(aoc.phone, 'Must not send', send_kind='blast')
    assert not blocked['success']
    aoc.provider.client.messages.create.assert_not_called()
    evidence(aoc, 'unknown', purpose='all', source='inbound_start', second=1)
    assert aoc.permission.recipient_permission_status(aoc.org.id, aoc.phone).allowed
    assert aoc.provider.send_message(aoc.phone, 'Resubscribed update', send_kind='blast')['success']
    aoc.provider.client.messages.create.assert_called_once()


@pytest.mark.parametrize('kind', ['unsubscribed', 'suppressed', 'owner_withdrawal'])
def test_actual_optout_or_delivery_suppression_still_wins_at_provider_boundary(aoc, kind):
    if kind == 'unsubscribed':
        aoc.db.session.add(aoc.models.UnsubscribedContact(organization_id=aoc.org.id, phone=aoc.phone))
    elif kind == 'suppressed':
        aoc.db.session.add(aoc.models.SuppressedContact(organization_id=aoc.org.id, phone=aoc.phone, category='hard_failure'))
    else:
        evidence(aoc, 'denied')
    aoc.db.session.commit()
    result = aoc.provider.send_message(aoc.phone, 'Must remain blocked', send_kind='blast')
    assert not result['success']
    aoc.provider.client.messages.create.assert_not_called()


def test_policy_cannot_be_selected_from_another_active_tenant(aoc):
    from app.tenant import organization_context
    with organization_context(aoc.other.id), pytest.raises(ValueError):
        aoc.permission.recipient_permission_status(aoc.org.id, aoc.phone)


def test_actual_preview_and_saved_request_include_AOC_signup_without_reapproval(aoc, monkeypatch):
    response = aoc.client.post('/login', data={'username': 'aoc-owner', 'password': 'Aoc-owner-pass1!'})
    assert response.status_code == 302
    preview = aoc.client.get('/dashboard/audience-preview?target=community')
    assert preview.status_code == 200
    assert preview.json['eligible'] == 1
    queue = MagicMock()
    monkeypatch.setattr('app.queue.get_queue', lambda: queue)
    response = aoc.client.post('/dashboard', data={'target': 'community', 'message_body': 'AOC community update', 'request_idempotency_key': 'aoc-contract-request'})
    assert response.status_code == 302
    saved = aoc.models.OutboundRequest.query.one()
    assert json.loads(saved.payload_json)['recipients'] == [{'name': 'Confirmed signup', 'phone': aoc.phone}]
    assert aoc.models.RecipientPermissionEvidence.query.count() == 0


def test_community_pages_are_bounded_sorted_and_tenant_scoped(aoc):
    from flask import template_rendered
    aoc.db.session.add_all([
        aoc.models.CommunityMember(organization_id=aoc.org.id, phone=f'+1720556{index:04d}', name=f'Member {index:03d}')
        for index in range(120)
    ])
    aoc.db.session.add(aoc.models.CommunityMember(organization_id=aoc.other.id, phone='+17205559991', name='Foreign private sentinel'))
    aoc.db.session.commit()
    aoc.client.post('/login', data={'username': 'aoc-owner', 'password': 'Aoc-owner-pass1!'})
    contexts = []
    def capture(sender, template, context, **extra):
        if template.name == 'community/list.html':
            contexts.append(context)
    with template_rendered.connected_to(capture, aoc.app):
        response = aoc.client.get('/community?sort=name&dir=desc&page=2')
        assert response.status_code == 200
        current = contexts[-1]
        assert len(current['members']) == 50
        assert (current['total_count'], current['total_pages'], current['page']) == (121, 3, 2)
        names = [member.name for member in current['members']]
        assert names == sorted(names, key=str.lower, reverse=True)
        html = response.get_data(as_text=True)
        assert 'Foreign private sentinel' not in html
        assert html.count('id="memberPreview"') == 1
        assert 'Select this page' in html and 'Selection applies to this page' in html
        aoc.client.get('/community?page=999999')
        assert contexts[-1]['page'] == 3
        assert len(contexts[-1]['members']) == 21
        aoc.client.get('/community?search=Member%20119&page=999999')
        assert contexts[-1]['total_count'] == 1
        assert contexts[-1]['members'][0].name == 'Member 119'


def test_postgresql_app_connections_use_UTC_without_changing_the_database_default(aoc):
    if aoc.db.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL connection timezone contract requires the explicit isolated PostgreSQL root.')
    import psycopg
    from sqlalchemy import text
    with psycopg.connect(os.environ['AUDIT_AOC_DATABASE_ROOT']) as direct:
        original_default = direct.execute('SHOW TimeZone').fetchone()[0]
    assert aoc.db.session.execute(text('SHOW TimeZone')).scalar() == 'UTC'
    aoc.db.session.refresh(aoc.member)
    now = aoc.models.utc_now().replace(tzinfo=None)
    assert abs((now - aoc.member.created_at).total_seconds()) < 60
    with psycopg.connect(os.environ['AUDIT_AOC_DATABASE_ROOT']) as direct:
        assert direct.execute('SHOW TimeZone').fetchone()[0] == original_default


def test_5000_contact_page_weight_is_bounded_without_truncating_broadcast_audience(aoc, monkeypatch, tmp_path):
    aoc.db.session.bulk_save_objects([
        aoc.models.CommunityMember(organization_id=aoc.org.id, phone=f'+172066{index:05d}', name=f'Load fixture {index:04d}')
        for index in range(4999)
    ])
    aoc.db.session.commit()
    aoc.client.post('/login', data={'username': 'aoc-owner', 'password': 'Aoc-owner-pass1!'})
    started = time.perf_counter()
    response = aoc.client.get('/community')
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    tags = len(re.findall(r'<[A-Za-z][^>]*>', html))
    assert len(response.data) < 700000
    assert tags < 8000
    assert html.count('id="memberPreview"') == 1
    assert 'of 5000 members' in html
    assert aoc.client.get('/dashboard/audience-preview?target=community').json['eligible'] == 5000
    queue = MagicMock()
    monkeypatch.setattr('app.queue.get_queue', lambda: queue)
    sent = aoc.client.post('/dashboard', data={
        'target': 'community', 'message_body': 'Whole community stays intact', 'request_idempotency_key': 'all-5000',
    })
    assert sent.status_code == 302
    assert len(json.loads(aoc.models.OutboundRequest.query.one().payload_json)['recipients']) == 5000
    print(json.dumps({'fixture_contacts': 5000, 'page_html_bytes': len(response.data), 'page_elements': tags,
                      'response_seconds': round(elapsed, 4), 'broadcast_snapshot_recipients': 5000}, sort_keys=True))

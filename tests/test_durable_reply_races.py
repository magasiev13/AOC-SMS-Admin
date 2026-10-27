"""Real PostgreSQL reply replay/recording interleavings; no external traffic."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
import pytest


@pytest.fixture
def replies(tmp_path, monkeypatch):
    from app import db, models
    from app.tenant import init_tenant_scoping, organization_context
    from app.services.outbound_request_service import reserve_outbound_request
    from app.services.twilio_service import TwilioService
    root = os.environ.get('REPLY_TEST_POSTGRES_ROOT')
    database_name = None
    if root:
        assert root == 'postgresql://audit@127.0.0.1:55432/postgres', 'Only the isolated loopback test cluster is allowed.'
        import psycopg
        from psycopg import sql
        database_name = 'reply_test_' + uuid4().hex
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
        uri = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    else:
        uri = 'sqlite:///' + str(tmp_path / 'reply.db')
    app = Flask(__name__)
    app.config.update(TESTING=True, SAAS_MODE=True, SQLALCHEMY_DATABASE_URI=uri, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    init_tenant_scoping()
    try:
        with app.app_context():
            db.create_all()
            org = models.Organization(name='Reply test', slug='reply-test', status='active')
            actor = models.AppUser(username='reply-owner', password_hash='synthetic', is_platform_admin=False)
            db.session.add_all([org, actor])
            db.session.flush()
            db.session.add(models.OrganizationSubscription(organization_id=org.id, status='complimentary'))
            db.session.add(models.OrganizationMessagingProfile(
                organization_id=org.id, status='active', provider_status='active',
                provider_mode='platform_managed', twilio_subaccount_sid='ACreplytest',
                messaging_service_sid='MGreplytest', sender_review_status='approved',
                from_number='+17205550101', phone_number_sid='PNreplytest',
            ))
            thread = models.InboxThread(organization_id=org.id, phone='+17205550147')
            db.session.add(thread)
            db.session.commit()
            with organization_context(org.id):
                request, _ = reserve_outbound_request(
                    organization_id=org.id, actor=actor, key='saved-reply', kind='reply',
                    intent={'thread_id': thread.id, 'phone': thread.phone, 'message_body': 'Saved reply'},
                    recipient_data=[{'phone': thread.phone}], message_body='Saved reply', thread_id=thread.id,
                )
                request.status = 'processing'
                db.session.commit()
            fixture = SimpleNamespace(app=app, db=db, models=models, org_id=org.id, thread_id=thread.id, request_id=request.id, postgres=bool(root))
            service = TwilioService.__new__(TwilioService)
            service.organization_id = org.id
            service.account_sid = 'ACreplytest'
            service.from_number = '+17205550101'
            service.messaging_service_sid = 'MGreplytest'
            service.client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: SimpleNamespace(sid='SMreply', status='queued', account_sid='ACreplytest')))
            fixture.service = service
            monkeypatch.setattr('app.services.inbox_service.get_twilio_service', lambda _: service)
            yield fixture
            db.session.remove()
            db.engine.dispose()
    finally:
        if database_name:
            import psycopg
            from psycopg import sql
            with psycopg.connect(root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database_name)))


def send(fixture):
    from app.services.inbox_service import send_thread_reply_with_key
    from app.tenant import organization_context
    with fixture.app.app_context(), organization_context(fixture.org_id):
        try:
            return send_thread_reply_with_key(fixture.thread_id, 'Saved reply', 'reply-owner', f'reply-request:{fixture.request_id}')
        finally:
            fixture.db.session.remove()


def test_sent_attempt_replay_records_one_bubble_and_atomic_result(replies):
    from app.tenant import organization_context
    model = replies.models
    with organization_context(replies.org_id):
        replies.db.session.add(model.MessageDispatchAttempt(
            organization_id=replies.org_id, logical_send_key=f'reply-request:{replies.request_id}',
            recipient_phone='+17205550147', message_body_digest=hashlib.sha256(b'Saved reply').hexdigest(),
            send_kind='manual_reply', status='sent', provider_message_sid='SMcommitted', attempt_count=1,
        ))
        replies.db.session.commit()
    calls = []
    replies.service.client.messages.create = lambda **kwargs: calls.append(kwargs)
    first = send(replies)
    second = send(replies)
    replies.db.session.expire_all()
    assert calls == []
    assert first['inbox_message_id'] == second['inbox_message_id']
    assert model.InboxMessage.query.count() == 1
    assert model.OutboundRequest.query.one().status == 'accepted'


def test_concurrent_unresolved_replay_then_acceptance_updates_same_bubble(replies):
    if not replies.postgres:
        pytest.skip('Separate-session provider overlap requires PostgreSQL row locks.')
    entered, release = Event(), Event()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(10), 'Provider barrier timed out'
        return SimpleNamespace(sid='SMconcurrent', status='queued', account_sid='ACreplytest')
    replies.service.client.messages.create = create
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, replies)
        assert entered.wait(10)
        try:
            second = pool.submit(send, replies)
            unresolved = second.result(timeout=10)
            assert unresolved['status'] == 'sending'
        finally:
            release.set()
        accepted = first.result(timeout=10)
    replies.db.session.expire_all()
    assert len(calls) == 1
    assert accepted['success'] is True
    assert accepted['inbox_message_id'] == unresolved['inbox_message_id']
    assert replies.models.InboxMessage.query.count() == 1
    assert replies.models.InboxMessage.query.one().message_sid == 'SMconcurrent'
    saved = replies.models.OutboundRequest.query.one()
    assert saved.status == 'accepted'
    assert json.loads(saved.result_json)['sid'] == 'SMconcurrent'


@pytest.mark.parametrize('later_block', ['stop', 'provider_inactive', 'billing_inactive', 'all'])
def test_committed_reply_recovery_preserves_acceptance_after_later_send_block(replies, monkeypatch, later_block):
    """Eligibility changes cannot rewrite a provider-accepted attempt as failure."""
    from app.tenant import organization_context

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(sid='SMaccepted-before-block', status='queued', account_sid='ACreplytest')

    replies.service.client.messages.create = create
    with monkeypatch.context() as interruption:
        def lose_inbox_commit(*_args, **_kwargs):
            raise ConnectionResetError('Interrupted after attempt commit, before inbox commit')

        interruption.setattr('app.services.inbox_service._append_inbox_message', lose_inbox_commit)
        with pytest.raises(ConnectionResetError):
            send(replies)

    models = replies.models
    replies.db.session.expire_all()
    attempt = models.MessageDispatchAttempt.query.one()
    assert (attempt.status, attempt.provider_message_sid) == ('sent', 'SMaccepted-before-block')
    assert models.OutboundRequest.query.one().status == 'processing'
    assert models.InboxMessage.query.count() == 0
    with organization_context(replies.org_id):
        if later_block in {'stop', 'all'}:
            from app.services.recipient_permission_service import record_permission_evidence
            replies.db.session.add(models.UnsubscribedContact(
                organization_id=replies.org_id, phone=attempt.recipient_phone, source='inbound', reason='Later STOP',
            ))
            record_permission_evidence(
                replies.org_id, attempt.recipient_phone, purpose='all', decision='denied',
                source='inbound_stop', evidence_key='later-stop',
            )
        if later_block in {'provider_inactive', 'all'}:
            profile = models.OrganizationMessagingProfile.query.one()
            profile.status = 'suspended'
            profile.provider_status = 'suspended'
            profile.twilio_subaccount_sid = None
        if later_block in {'billing_inactive', 'all'}:
            models.OrganizationSubscription.query.one().status = 'past_due'
        replies.db.session.commit()

    def no_provider_reconstruction(_organization_id):
        raise AssertionError('Known outcome recovery must not construct a provider or submit another message.')

    monkeypatch.setattr('app.services.inbox_service.get_twilio_service', no_provider_reconstruction)
    recovered = send(replies)
    replay = send(replies)
    replies.db.session.expire_all()
    assert len(calls) == 1
    assert recovered['success'] is True and recovered['sid'] == 'SMaccepted-before-block'
    assert replay['inbox_message_id'] == recovered['inbox_message_id']
    assert models.InboxMessage.query.count() == 1
    assert models.InboxMessage.query.one().message_sid == 'SMaccepted-before-block'
    assert models.OutboundRequest.query.one().status == 'accepted'
    assert models.MessageDispatchAttempt.query.one().provider_message_sid == 'SMaccepted-before-block'
    if later_block in {'stop', 'all'}:
        assert models.UnsubscribedContact.query.count() == 1

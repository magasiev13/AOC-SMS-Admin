"""Account mail uses synthetic SMTP and loopback-only fixtures."""
import re
from datetime import timedelta
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services import account_mail_service as mail
from app.services.inbox_service import process_inbound_sms
from tests.test_ux_recovery import ux, login


@pytest.fixture
def configured(ux, monkeypatch):
    ux.app.config.update(ACCOUNT_MAIL_ENABLED=True, ACCOUNT_MAIL_SMTP_HOST='smtp.example.test',
                         ACCOUNT_MAIL_SMTP_PORT=465, ACCOUNT_MAIL_SMTP_USER='fixture',
                         ACCOUNT_MAIL_SMTP_PASSWORD='fixture', ACCOUNT_MAIL_FROM='mail@example.test',
                         APP_BASE_URL='https://workspace.example.test')
    user = ux.db.session.get(ux.models.AppUser, ux.users['owner'])
    user.email = 'owner@example.test'
    ux.db.session.commit()
    delivered = []
    monkeypatch.setattr(mail, '_send', delivered.append)
    ux.captured = delivered
    ux.owner = user
    ux.redis = Mock()
    ux.redis.set.return_value = True
    monkeypatch.setattr('app.account_mail_routes.get_redis_connection', lambda: ux.redis)
    return ux


def consume(fixture, kind):
    row = fixture.models.AccountMailDelivery.query.filter_by(kind=kind, status='pending').one()
    mail.deliver_mail(fixture.org_id, row.id)
    assert row.status == 'accepted'
    message = fixture.captured[-1]
    code = re.search(r'Code: ([A-Za-z0-9_-]{43})', message.get_content()).group(1)
    assert row.token_digest != code
    assert f'https://workspace.example.test/account/email/{kind}#{code}' in message.get_content()
    return row, code


def test_verify_recover_throttle_single_use_and_session_invalidation(configured):
    f = configured
    login(f)
    signed_in = f.client
    assert signed_in.get('/account/email').status_code == 200
    assert signed_in.post('/account/email', data={'action':'verify','password':'incorrect'}).status_code == 302
    assert f.models.AccountMailDelivery.query.count() == 0
    signed_in.post('/account/email', data={'action':'enable'})
    assert f.owner.primary_membership.reply_notice_email is None
    signed_in.post('/account/email', data={'action':'verify','password':'Isolated-ux-pass1!'})
    verify_row, code = consume(f, 'verify')
    assert not f.owner.verified_recovery_email
    verify_form = signed_in.get('/account/email/verify')
    assert verify_form.status_code == 200 and verify_form.headers['Referrer-Policy'] == 'same-origin'
    assert verify_form.cache_control.no_store
    assert signed_in.post('/account/email/verify', data={'code':code}).status_code == 302
    assert verify_row.status == 'used' and f.owner.verified_recovery_email == f.owner.email
    assert f.owner.primary_membership.reply_notice_email is None
    public = f.app.test_client()
    payload = {'email':f.owner.email}
    known = public.post('/forgot-password', data=payload, follow_redirects=True)
    unknown = public.post('/forgot-password', data={'email':'unknown@example.test'}, follow_redirects=True)
    assert b'If this workspace account has a verified recovery email' in known.data and known.status_code == unknown.status_code
    public.post('/forgot-password', data=payload)
    assert f.models.AccountMailDelivery.query.filter_by(kind='recover').count() == 1
    row, reset_code = consume(f, 'recover')
    assert signed_in.get('/account/email').status_code == 200
    before = f.owner.session_nonce
    fields = {'code':reset_code, 'password':'Isolated-ux-pass1!', 'confirmation':'Isolated-ux-pass1!'}
    assert b'different from recently' in public.post('/account/email/recover', data=fields).data
    assert row.status == 'accepted'
    fields.update(password='Changed-secret-999!', confirmation='Changed-secret-999!')
    assert public.post('/account/email/recover', data=fields).status_code == 302
    assert row.status == 'used' and f.owner.session_nonce != before
    assert f.owner.check_password('Changed-secret-999!')
    # The fixture keeps an outer app context; reload login state as a real new request does.
    with f.app.app_context():
        assert signed_in.get('/account/email').status_code == 302
    assert b'no longer valid' in public.post('/account/email/recover', data=fields).data
    assert f.models.AuthEvent.query.filter_by(event_type='password_recovered').count() == 1
    assert f.models.UserPasswordHistory.query.filter_by(user_id=f.owner.id).count() == 1


def test_recovery_refuses_unverified_disabled_expired_and_stale_accounts(configured):
    f = configured
    public = f.app.test_client()
    payload = {'email': f.owner.email}
    public.post('/forgot-password', data=payload)
    assert f.models.AccountMailDelivery.query.count() == 0
    f.owner.verified_recovery_email = f.owner.email
    f.db.session.commit()
    f.redis.set.return_value = False
    public.post('/forgot-password', data=payload)
    f.redis.set.side_effect = RedisConnectionError()
    public.post('/forgot-password', data=payload)
    assert f.models.AccountMailDelivery.query.count() == 0
    f.redis.set.side_effect = None
    f.redis.set.return_value = True
    public.post('/forgot-password', data=payload)
    row, reset = consume(f, 'recover')
    row.expires_at = mail._now() - timedelta(seconds=1)
    f.db.session.commit()
    assert mail.lock_link(reset, 'recover') == (None, None)
    row.expires_at = mail._now() + timedelta(minutes=10)
    f.owner.rotate_session_nonce()
    f.db.session.commit()
    assert mail.lock_link(reset, 'recover') == (None, None)
    f.owner.email = 'changed@example.test'
    f.db.session.commit()
    assert f.owner.verified_recovery_email is None
    f.app.config['ACCOUNT_MAIL_ENABLED'] = False
    assert b'Email recovery is unavailable' in public.get('/forgot-password').data
    assert not mail.mail_ready()


def test_inbound_reply_outbox_is_private_deduplicated_and_opt_in(configured):
    f = configured
    login(f)
    inbound = {'MessageSid':'SMnotice1','From':'+17205550099','To':'+17205550901','Body':'Private test body'}
    assert process_inbound_sms(inbound)['status'] == 'stored'
    assert f.models.AccountMailDelivery.query.count() == 0
    f.owner.verified_recovery_email = f.owner.email
    f.db.session.commit()
    f.client.post('/account/email',data={'action':'enable'})
    for _ in range(2):
        process_inbound_sms(dict(inbound, MessageSid='SMnotice2'))
    row = f.models.AccountMailDelivery.query.filter_by(kind='reply').one()
    mail.deliver_mail(f.other_id, row.id)
    assert not f.captured
    for _ in range(2):
        mail.deliver_mail(f.org_id, row.id)
    assert len(f.captured) == 1 and row.status == 'accepted'
    body = f.captured[0].get_content()
    assert 'https://workspace.example.test/inbox' in body
    assert all(value not in body for value in (inbound['From'], inbound['To'], inbound['Body'],'SMnotice2',f.owner.username))
    process_inbound_sms(dict(inbound, MessageSid='SMnotice3'))
    pending = f.models.AccountMailDelivery.query.filter_by(status='pending').one()
    f.client.post('/account/email',data={'action':'disable'})
    mail.deliver_mail(f.org_id, pending.id)
    assert pending.status == 'expired' and len(f.captured) == 1


def test_queue_retry_rollback_and_uncertain_smtp_is_never_replayed(configured, monkeypatch):
    f = configured
    mail.request_account_link(f.owner.id, 'verify')
    row = f.models.AccountMailDelivery.query.one()
    queue = Mock()
    monkeypatch.setattr('app.queue.get_queue', lambda: queue)
    queue.enqueue.side_effect = RedisConnectionError()
    mail.recover_mail()
    assert row.status == 'pending' and row.next_enqueue_at > mail._now()
    row.next_enqueue_at = None
    f.db.session.commit()
    queue.enqueue.side_effect = None
    mail.recover_mail()
    assert queue.enqueue.call_args.args[1:] == (f.org_id, row.id)
    func = Mock(side_effect=mail.BeforeSubmissionError())
    monkeypatch.setattr(mail, '_send', func)
    mail.deliver_mail(f.org_id, row.id)
    assert row.status == 'pending' and row.token_digest is None
    func.side_effect = TimeoutError('simulated ambiguous acceptance')
    mail.deliver_mail(f.org_id, row.id)
    assert row.status == 'unconfirmed'
    mail.deliver_mail(f.org_id, row.id)
    assert func.call_count == 2
    f.owner.verified_recovery_email = f.owner.email
    f.owner.primary_membership.reply_notice_email = f.owner.email
    f.db.session.commit()
    # Failed inbound transaction rolls back the notice with its parent message.
    from types import SimpleNamespace
    mail.save_reply_notices(SimpleNamespace(organization_id=f.org_id,id=123))
    f.db.session.flush()
    f.db.session.rollback()
    assert f.models.AccountMailDelivery.query.filter_by(kind='reply').count() == 0


@pytest.mark.parametrize('port, transport', [(465, 'SMTP_SSL'), (587, 'SMTP')])
def test_smtp_transport_requires_tls_and_credentials(ux, monkeypatch, port, transport):
    from email.message import EmailMessage
    import smtplib
    ux.app.config.update(ACCOUNT_MAIL_SMTP_HOST='fixture.example.test', ACCOUNT_MAIL_SMTP_PORT=port,
                         ACCOUNT_MAIL_SMTP_USER='fixture', ACCOUNT_MAIL_SMTP_PASSWORD='fixture')
    client = Mock()
    connect = Mock(return_value=client)
    monkeypatch.setattr(smtplib, transport, connect)
    message = EmailMessage()
    message.set_content('Fixture')
    mail._send(message)
    assert connect.call_args.kwargs['port'] == port
    assert [call[0] for call in client.method_calls] == (
        ['starttls', 'login', 'send_message', 'close'] if port == 587 else ['login', 'send_message', 'close'])
    client.login.side_effect = OSError()
    with pytest.raises(mail.BeforeSubmissionError):
        mail._send(message)
    assert client.send_message.call_count == 1


def test_mail_migration_replays_without_enrolling_or_invalidating_existing_users(ux):
    from importlib import import_module
    from sqlalchemy import inspect, text
    apply = import_module('app.saas_migrations.036_account_mail').apply
    ux.db.session.remove()
    with ux.db.engine.begin() as connection:
        previous = connection.execute(text('SELECT id, session_nonce FROM users ORDER BY id')).all()
        ux.models.AccountMailDelivery.__table__.drop(connection)
        connection.execute(text('ALTER TABLE users DROP COLUMN verified_recovery_email'))
        connection.execute(text('ALTER TABLE organization_memberships DROP COLUMN reply_notice_email'))
        for _ in range(2):
            apply(connection, ux.app.logger)
        assert 'account_mail_deliveries' in inspect(connection).get_table_names()
        assert connection.execute(text('SELECT id, session_nonce FROM users ORDER BY id')).all() == previous
        assert connection.execute(text('SELECT COUNT(*) FROM users WHERE verified_recovery_email IS NOT NULL')).scalar() == 0
        assert connection.execute(text('SELECT COUNT(*) FROM organization_memberships WHERE reply_notice_email IS NOT NULL')).scalar() == 0
        assert connection.execute(text('SELECT COUNT(*) FROM account_mail_deliveries')).scalar() == 0


def test_postgres_competing_workers_and_link_redemptions_are_single_use(configured):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f = configured
    if f.db.engine.dialect.name != 'postgresql':
        pytest.skip('Requires PostgreSQL row-lock semantics')
    f.owner.verified_recovery_email = f.owner.email
    f.db.session.commit()
    mail.request_account_link(f.owner.id, 'recover')
    delivery = f.models.AccountMailDelivery.query.one().id
    barrier = Barrier(2)

    def send():
        with f.app.app_context():
            barrier.wait(timeout=10)
            mail.deliver_mail(f.org_id, delivery)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(send) for _ in range(2)]
        for job in jobs:
            job.result(timeout=20)
    assert len(f.captured) == 1
    code = re.search(r'Code: ([A-Za-z0-9_-]{43})', f.captured[0].get_content()).group(1)
    barrier = Barrier(2)

    def redeem():
        with f.app.app_context():
            browser = f.app.test_client()
            barrier.wait(timeout=10)
            return browser.post('/account/email/recover', data={
                'code':code, 'password':'Changed-simultaneously999!', 'confirmation':'Changed-simultaneously999!',
            }).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(redeem) for _ in range(2)]
        assert sorted(job.result(timeout=30) for job in jobs) == [200, 302]
    assert f.models.AuthEvent.query.filter_by(event_type='password_recovered').count() == 1

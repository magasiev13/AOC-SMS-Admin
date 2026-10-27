"""Opt-in real-PG upgrade and billing-timer preservation of historical usage.

Synthetic data deliberately matches the incident's scale and mixed old states.
It never imports customer data or connects to production/provider resources.
"""
from datetime import timedelta
from decimal import Decimal
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, inspect, text


LOCAL_ROOT = 'postgresql://audit@127.0.0.1:55432/postgres'
MARKER = 'historical_observation_only'
OLD_USAGE_COUNT = 7974
ACCOUNT = 'AC' + 'a' * 32
PRESERVED_TABLES = (
    'community_members', 'event_registrations', 'message_dispatch_attempts',
    'organization_invitations', 'organization_subscriptions',
    'organization_usage_billing_periods', 'scheduled_messages',
    'stripe_checkout_sessions', 'suppressed_contacts', 'unsubscribed_contacts',
    'recipient_permission_evidence',
)


def _shift_month(value, offset):
    months = value.year * 12 + value.month - 1 + offset
    return value.replace(year=months // 12, month=months % 12 + 1)


def _snapshot(connection, table, *, without_marker=False, old_usage_only=False):
    quoted = connection.dialect.identifier_preparer.quote(table)
    expression = "to_jsonb(t) - 'historical_observation_only'" if without_marker else 'to_jsonb(t)'
    where = f' WHERE id <= {OLD_USAGE_COUNT}' if old_usage_only else ''
    rows = sorted(connection.execute(text(f'SELECT ({expression})::text FROM {quoted} AS t{where}')).scalars())
    return {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}


def _snapshots(fixture, tables=PRESERVED_TABLES):
    fixture.db.session.remove()
    with fixture.db.engine.connect() as connection:
        return {table: _snapshot(connection, table) for table in tables}


@pytest.fixture
def historical(tmp_path, monkeypatch, request):
    root = os.environ.get('HISTORICAL_TEST_POSTGRES_ROOT')
    if not root:
        pytest.skip('Historical upgrade safety requires the explicit isolated PostgreSQL root.')
    if root != LOCAL_ROOT:
        raise RuntimeError('Historical recovery tests accept only audit@127.0.0.1:55432/postgres.')
    import psycopg
    from psycopg import sql

    database_name = 'historical_safety_' + uuid4().hex
    with psycopg.connect(root, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(database_name)))
    database_url = 'postgresql+psycopg://audit@127.0.0.1:55432/' + database_name
    for key, value in {
        'DATABASE_URL': database_url, 'SAAS_MODE': '1', 'FLASK_DEBUG': '1',
        'SCHEDULER_ENABLED': '0', 'PYTHON_DOTENV_DISABLED': '1',
        'SECRET_KEY': 'isolated-historical-recovery-test', 'AUTH_ALERTS_ENABLED': '0',
        'REDIS_URL': 'redis://127.0.0.1:1/0', 'TWILIO_BROWSER_FAKE_SENDS': '1',
        'TWILIO_ACCOUNT_SID': ACCOUNT, 'TWILIO_AUTH_TOKEN': 'unused-test-token',
        'STRIPE_SECRET_KEY': '',
    }.items():
        monkeypatch.setenv(key, value)
    import app.config

    importlib.reload(app.config)
    from app import create_app, db, models
    from app.saas_migrations.runner import run_pending_saas_migrations
    from app.services import billing_service, delivery_status_service, twilio_service

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, USAGE_RECONCILE_BATCH_SIZE=100,
                      USAGE_RECONCILE_TIME_BUDGET_SECONDS=30)
    evidence = {'environment': 'Owned synthetic PostgreSQL database on loopback; all providers mocked',
                'historical_rows': OLD_USAGE_COUNT}
    try:
        with app.app_context():
            assert app.config['SQLALCHEMY_DATABASE_URI'] == database_url
            # Migration 001 creates current metadata. Build an independent
            # physical pre-027 table first so 027 must actually add its column;
            # never remove or alter a column on the application's metadata.
            previous = MetaData()
            for table in db.metadata.sorted_tables:
                table.to_metadata(previous)
            usage_026 = previous.tables['messaging_usage_records']
            usage_026._columns.remove(usage_026.c[MARKER])
            previous.create_all(db.engine)
            applied = run_pending_saas_migrations(db.engine, app.logger, target_version='026')
            assert applied[-1] == '026'
            with db.engine.connect() as connection:
                assert MARKER not in {column['name'] for column in inspect(connection).get_columns('messaging_usage_records')}
            fixture = SimpleNamespace(app=app, db=db, models=models, billing=billing_service,
                                      delivery=delivery_status_service, twilio=twilio_service,
                                      migrate=run_pending_saas_migrations, evidence=evidence)
            _seed_026(fixture)
            yield fixture
            db.session.remove()
            db.engine.dispose()
    finally:
        evidence_directory = os.environ.get('HISTORICAL_TEST_EVIDENCE_DIR')
        if evidence_directory:
            directory = Path(evidence_directory)
            directory.mkdir(parents=True, exist_ok=True)
            name = re.sub(r'[^a-zA-Z0-9_-]', '_', request.node.name)
            (directory / f'{name}.json').write_text(json.dumps(evidence, indent=2, default=str) + '\n')
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database_name)))


def _seed_026(fixture):
    db, m = fixture.db, fixture.models
    start, end = fixture.twilio.previous_billing_period_window()
    fixture.start, fixture.end = start, end
    org = m.Organization(name='Synthetic historical tenant', slug='historical-safety', status='active')
    other = m.Organization(name='Synthetic independent tenant', slug='historical-other', status='active')
    owner = m.AppUser(username='historical-owner', password_hash='unused-test-hash')
    db.session.add_all([org, other, owner])
    db.session.flush()
    fixture.org_id, fixture.other_id = org.id, other.id
    for organization, account, number in [(org, ACCOUNT, '+17205559000'), (other, 'AC' + 'b' * 32, '+17205559001')]:
        db.session.add(m.OrganizationSubscription(organization=organization, status='complimentary'))
        db.session.add(m.OrganizationMessagingProfile(
            organization=organization, provider_mode='platform_managed', twilio_subaccount_sid=account,
            messaging_service_sid='MG' + ('a' if organization is org else 'b') * 32,
            from_number=number, status='active', provider_status='active', sender_review_status='approved'))
    db.session.add(m.OrganizationMembership(organization_id=org.id, user_id=owner.id, role='owner'))
    db.session.add(m.OrganizationInvitation(organization_id=org.id, email='history@example.test', role='owner',
                                           token='retained-invitation', status='accepted', invited_by_user_id=owner.id,
                                           expires_at=start + timedelta(days=5)))
    events = [m.Event(organization_id=organization.id, title='Retained event') for organization in [org, other]]
    db.session.add_all(events)
    db.session.flush()
    for index in range(310):
        phone = f'+1720555{index:04d}'
        db.session.add(m.CommunityMember(organization_id=org.id, name=f'Retained member {index}', phone=phone))
        if index < 269:
            db.session.add(m.EventRegistration(organization_id=org.id, event_id=events[0].id, name=f'Retained registration {index}', phone=phone))
    db.session.add(m.CommunityMember(organization_id=other.id, name='Other tenant, same phone', phone='+17205550000'))
    db.session.add(m.EventRegistration(organization_id=other.id, event_id=events[1].id, phone='+17205550000'))
    log = m.MessageLog(organization_id=org.id, message_body='Retained original send', target='community', status='sent',
                       total_recipients=100, success_count=100, failure_count=0,
                       details=json.dumps([{'sid': f'SM{index:032x}', 'phone': f'+1720555{index-1:04d}',
                                            'success': True, 'status': 'sent'} for index in range(1, 101)]))
    db.session.add(log)
    db.session.flush()
    fixture.log_id = log.id
    db.session.add_all([
        m.SuppressedContact(id=23, organization_id=org.id, phone='+17205550004', category='hard_fail',
                            reason='Retained suppression evidence', source='manual', source_type='message_log',
                            source_message_log_id=log.id, created_at=start, updated_at=start),
        m.UnsubscribedContact(organization_id=org.id, phone='+17205550309', source='inbound', reason='Retained STOP', created_at=start),
        m.RecipientPermissionEvidence(organization_id=org.id, phone='+17205550001', purpose='community', scope_key='',
                                      decision='granted', source='owner_verified', evidence_key='retained-proof', verified=True,
                                      metadata_json='{"proof":"Synthetic retained consent"}', occurred_at=start, recorded_at=start),
        m.MessageDispatchAttempt(organization_id=org.id, message_log_id=log.id, logical_send_key='retained-accepted-attempt',
                                 recipient_phone='+17205550000', message_body_digest='a' * 64, send_kind='blast',
                                 status='sent', attempt_count=1, provider_message_sid='SM' + '1' * 32),
        m.ScheduledMessage(organization_id=org.id, scheduled_at=end + timedelta(days=50), target='event', event_id=events[0].id,
                           status='pending', message_body='Retained scheduled message', request_idempotency_key='retained-schedule'),
        m.StripeCheckoutSession(organization_id=org.id, stripe_checkout_session_id='cs_retained_history', billing_plan_code='monthly',
                                recurring_price_id='price_retained_history', offer_version='retained-offer', status='complete'),
    ])
    for offset in range(6):
        db.session.add(m.OrganizationUsageBillingPeriod(
            id=6-offset, organization_id=org.id, period_start=_shift_month(start, -offset),
            period_end=_shift_month(end, -offset), status='included', included_units=1000,
            used_units=3828 if offset == 0 else 800 + offset, overage_units=0, sell_amount=0,
            currency='usd', created_at=start, updated_at=start))
    db.session.commit()
    fixture.messages = {}
    with db.engine.begin() as connection:
        connection.execute(text("SELECT setval(pg_get_serial_sequence('organization_usage_billing_periods','id'), 6)"))
        old = Table('messaging_usage_records', MetaData(), autoload_with=connection)
        payload = []
        for index in range(1, OLD_USAGE_COUNT + 1):
            # The most recent period contains only the nine priced rows with
            # review=False, so an unrelated legacy review flag cannot mask a
            # missing historical guard. Other months retain mixed old states.
            offset = 0 if index <= 9 or index == OLD_USAGE_COUNT else 1 + (index - 10) % 5
            period_start, period_end = _shift_month(start, -offset), _shift_month(end, -offset)
            known = index <= 9
            row = {
                'id': index, 'organization_id': other.id if index == OLD_USAGE_COUNT else org.id,
                'message_sid': f'SM{index:032x}', 'direction': 'outbound', 'source': 'blast',
                'twilio_subaccount_sid': 'AC' + 'b' * 32 if index == OLD_USAGE_COUNT else ACCOUNT,
                'twilio_message_status': 'sent', 'provider_currency': 'usd',
                'provider_cost': Decimal('0.0075') if known else Decimal('0'),
                'sell_rate': Decimal('0.03'), 'sell_amount': Decimal('0'), 'margin': Decimal('0'),
                'billable_units': (304 if index <= 8 else 312) if known else 1, 'billable': True,
                'reconciliation_status': 'pending', 'delivery_status': 'sent' if index <= 100 else 'pending',
                'delivery_evidence_json': '[{"status":"sent","sources":["poll"]}]' if index <= 100 else '[]',
                'delivery_expires_at': period_end + timedelta(days=3) if index <= 100 else None,
                'cost_expires_at': period_end + timedelta(days=7) if index <= 100 else None,
                'cost_status': 'known' if known else 'pending', 'units_status': 'known',
                'billing_review_required': not known and index != OLD_USAGE_COUNT,
                'reconcile_attempt_count': 1 if index <= 100 else 0,
                'billing_period_start': period_start, 'billing_period_end': period_end,
                'created_at': period_start + timedelta(days=3), 'reconciled_at': period_end if index <= 100 else None,
            }
            payload.append(row)
            if index <= 100:
                fixture.messages[row['message_sid']] = row
        connection.execute(old.insert(), payload)
        connection.execute(text("SELECT setval(pg_get_serial_sequence('messaging_usage_records','id'), :last_id)"), {'last_id': OLD_USAGE_COUNT})
    db.session.remove()


def _upgrade(fixture):
    assert fixture.migrate(fixture.db.engine, fixture.app.logger, target_version='027') == ['027']
    with fixture.db.engine.connect() as connection:
        column = next(row for row in inspect(connection).get_columns('messaging_usage_records') if row['name'] == MARKER)
        assert column['nullable'] is False and 'true' in str(column['default']).lower()
        assert connection.scalar(text('SELECT count(*) FROM messaging_usage_records WHERE historical_observation_only IS TRUE')) == OLD_USAGE_COUNT
        assert connection.scalar(text("SELECT count(*) FROM messaging_usage_records WHERE billing_review_required IS FALSE AND cost_status='known' AND historical_observation_only IS TRUE")) == 9


def test_actual_026_to_027_preserves_all_old_fields_and_new_live_marker_on_rerun(historical):
    h = historical
    with h.db.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names()) - {'saas_schema_migrations', 'messaging_usage_records'}
        before = {table: _snapshot(connection, table) for table in tables}
        usage_before = _snapshot(connection, 'messaging_usage_records')
    _upgrade(h)
    with h.db.engine.connect() as connection:
        assert {table: _snapshot(connection, table) for table in tables} == before
        assert _snapshot(connection, 'messaging_usage_records', without_marker=True) == usage_before
    live_sid = 'SM' + 'f' * 32
    h.twilio.record_usage_candidates(h.org_id, [{'sid': live_sid, 'status': 'queued', 'account_sid': ACCOUNT}], source='reply')
    live = h.models.MessagingUsageRecord.query.filter_by(message_sid=live_sid).one()
    assert live.historical_observation_only is False
    # Re-observing a pre-existing SID is not permission to turn it live.
    h.twilio.record_usage_candidates(h.org_id, [{'sid': 'SM' + f'{1:032x}', 'status': 'sent', 'account_sid': ACCOUNT}], source='reply')
    assert h.models.MessagingUsageRecord.query.filter_by(id=1).one().historical_observation_only is True
    h.db.session.remove()
    with h.db.engine.begin() as connection:
        # An unknown/raw writer does not inherit the privileged live default.
        raw = Table('messaging_usage_records', MetaData(), autoload_with=connection)
        payload = dict(h.messages['SM' + f'{1:032x}'])
        payload.pop('id')
        payload['message_sid'] = 'SM' + 'e' * 32
        connection.execute(raw.insert(), payload)
        assert connection.scalar(text("SELECT historical_observation_only FROM messaging_usage_records WHERE message_sid=:sid"), {'sid': payload['message_sid']}) is True
    before_rerun = _snapshots(h, tables)
    marker_counts_before = dict(h.db.session.execute(text('SELECT historical_observation_only, count(*) FROM messaging_usage_records GROUP BY historical_observation_only')).all())
    assert h.migrate(h.db.engine, h.app.logger, target_version='027') == []
    migration = importlib.import_module('app.saas_migrations.027_historical_usage_observation_boundary')
    h.db.session.remove()
    with h.db.engine.begin() as connection:
        migration.apply(connection, h.app.logger)
        assert dict(connection.execute(text('SELECT historical_observation_only, count(*) FROM messaging_usage_records GROUP BY historical_observation_only')).all()) == marker_counts_before
    assert _snapshots(h, tables) == before_rerun
    assert _snapshots(h) == {table: before[table] for table in PRESERVED_TABLES}
    assert h.models.MessagingUsageRecord.query.filter_by(message_sid=live_sid).one().historical_observation_only is False
    h.evidence.update(migration_tables_before=before, old_usage_before=usage_before,
                      all_old_fields_unchanged=True, all_7974_flagged=True,
                      known_price_review_false_flagged=9, new_live_flag=False,
                      existing_sid_replay_flag=True, raw_writer_protected=True,
                      migration_rerun_kept_live=True)


@pytest.mark.parametrize('provider_code', ['30003', '30006', '21211', '21610'])
def test_actual_billing_timer_observes_old_100_without_customer_or_financial_mutation(historical, monkeypatch, provider_code):
    h = historical
    _upgrade(h)
    before = _snapshots(h)
    with h.db.engine.connect() as connection:
        target_known_units = connection.scalar(text("SELECT SUM(billable_units) FROM messaging_usage_records WHERE organization_id=:org AND billing_period_start=:start AND cost_status='known'"), {'org': h.org_id, 'start': h.start})
        assert target_known_units == 2744
        assert connection.scalar(text('SELECT count(*) FROM messaging_usage_records WHERE organization_id=:org AND billing_period_start=:start AND billing_review_required IS TRUE'), {'org': h.org_id, 'start': h.start}) == 0
    observed = []
    def client_for_org(organization_id):
        assert organization_id == h.org_id
        def messages(sid):
            row = h.messages[sid]
            observed.append(sid)
            return SimpleNamespace(fetch=lambda: SimpleNamespace(
                sid=sid, account_sid=ACCOUNT, direction='outbound-api', status='undelivered',
                price='-0.0075' if row['id'] <= 9 else None, price_unit='usd', num_segments='1',
                date_created=row['created_at'], error_code=provider_code, error_message='Historical provider failure'))
        return SimpleNamespace(messages=messages)
    monkeypatch.setattr(h.twilio, '_client_for_usage_reconciliation', client_for_org)
    stripe = MagicMock()
    monkeypatch.setattr(h.billing, '_stripe_module', lambda: stripe)
    summary = h.billing.reconcile_billing_subscriptions()
    assert summary['usage_records_seen'] == len(set(observed)) == 100
    assert summary['usage_records_errored'] == 0
    assert summary['usage_periods_updated'] == summary['usage_periods_posted'] == 0
    assert _snapshots(h) == before
    stripe.assert_not_called()
    assert stripe.mock_calls == []
    assert h.models.CommunityMember.query.filter_by(organization_id=h.org_id).count() == 310
    assert h.models.EventRegistration.query.filter_by(organization_id=h.org_id).count() == 269
    assert h.models.OrganizationUsageBillingPeriod.query.filter_by(status='included').count() == 6
    target = h.models.OrganizationUsageBillingPeriod.query.filter_by(id=6).one()
    assert target.used_units == 3828 and target.status == 'included'
    assert h.twilio.usage_period_billing_block_reason(target) == 'historical_usage_review_required'
    assert h.models.MessagingUsageRecord.query.filter_by(historical_observation_only=True).count() == OLD_USAGE_COUNT
    assert h.models.MessagingUsageRecord.query.filter_by(suppression_applied_at=None).count() == OLD_USAGE_COUNT
    h.evidence.update(customer_financial_hashes=before, preserved_after_timer=True,
                      old_provider_reads=len(observed), provider_error_code=provider_code,
                      retained_included_periods=6, retained_target_used_units=3828,
                      incomplete_known_units=2744, target_has_no_legacy_review_flag=True,
                      retained_contacts=310, retained_registrations=269, stripe_operations=0,
                      timer_summary=summary)

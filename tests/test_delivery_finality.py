"""Delivery receipts and cost facts evolve without rewriting acceptance evidence."""
import hashlib
import importlib
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import MagicMock, patch
from uuid import uuid4


class TestDeliveryFinality(unittest.TestCase):
    def setUp(self):
        self.saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix='twinevia-delivery-finality-')
        self.postgres_root = os.environ.get('DELIVERY_TEST_POSTGRES_ROOT')
        self.postgres_db_name = None
        if self.postgres_root:
            parsed = urlsplit(self.postgres_root)
            if parsed.hostname != '127.0.0.1' or parsed.port != 55432 or parsed.username != 'audit' or parsed.path != '/postgres':
                raise RuntimeError('Delivery tests accept only the isolated local PostgreSQL audit root.')
            import psycopg
            from psycopg import sql

            self.postgres_db_name = 'delivery_finality_' + uuid4().hex
            with psycopg.connect(self.postgres_root, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(self.postgres_db_name)))
            database_url = urlunsplit(('postgresql+psycopg', parsed.netloc, '/' + self.postgres_db_name, '', ''))
        else:
            database_url = 'sqlite:///' + self.tmp.name + '/delivery.db'
        os.environ.update(
            DATABASE_URL=database_url, SAAS_MODE='1', FLASK_DEBUG='1', SCHEDULER_ENABLED='0',
            PYTHON_DOTENV_DISABLED='1', AUTH_ALERTS_ENABLED='0', SECRET_KEY='isolated-delivery-test-secret',
        )
        import app.config

        importlib.reload(app.config)
        from app import create_app, db, models
        from app.tenant import organization_context
        from app.services import delivery_status_service, twilio_service

        self.db, self.models, self.organization_context = db, models, organization_context
        self.delivery, self.twilio = delivery_status_service, twilio_service
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True, WTF_CSRF_ENABLED=False, TWILIO_AUTH_TOKEN='fake-delivery-signing-token',
            TWILIO_BROWSER_FAKE_SENDS=True, SAAS_BASE_URL='https://app.example.test',
            DELIVERY_RECEIPT_TIMEOUT_HOURS=72, USAGE_FACTS_TIMEOUT_DAYS=7,
        )
        self.context = self.app.app_context()
        self.context.push()
        self.assertEqual(self.app.config['SQLALCHEMY_DATABASE_URI'], database_url)
        db.create_all()
        self.org = models.Organization(name='Delivery fixture', slug='delivery-fixture', status='active')
        self.profile = models.OrganizationMessagingProfile(
            organization=self.org, provider_mode='platform_managed', twilio_subaccount_sid='AC' + 'a' * 32,
            messaging_service_sid='MG' + 'a' * 32, from_number='+17205559000', provider_status='active', status='active',
            sender_review_status='approved',
        )
        subscription = models.OrganizationSubscription(organization=self.org, status='complimentary')
        owner = models.AppUser(username='delivery-owner', email='delivery@fixture.test', phone='+17205559001', role='admin')
        owner.set_password('Delivery-fixture-owner1!')
        db.session.add_all([self.org, self.profile, subscription, owner])
        db.session.flush()
        db.session.add(models.OrganizationMembership(organization_id=self.org.id, user_id=owner.id, role='owner'))
        for policy, key in [('terms','TERMS_POLICY_VERSION'),('privacy','PRIVACY_POLICY_VERSION'),('acceptable_use','ACCEPTABLE_USE_POLICY_VERSION'),('sms','SMS_POLICY_VERSION'),('billing','BILLING_POLICY_VERSION')]:
            db.session.add(models.CustomerPolicyAcceptance(organization_id=self.org.id, user_id=owner.id, policy_name=policy, policy_version=self.app.config[key], accepted_ip='127.0.0.1'))
        db.session.commit()
        self.org_id, self.owner_id = self.org.id, owner.id
        self.client = self.app.test_client()
        self.client.post('/login', data={'username': owner.email, 'password': 'Delivery-fixture-owner1!'})
        self.sid, self.phone = 'SM' + 'a' * 32, '+17205550145'
        self.now = self.delivery.utc_naive()
        self.poll_number = 0

    def tearDown(self):
        self.db.session.remove()
        self.db.engine.dispose()
        self.context.pop()
        if self.postgres_db_name:
            import psycopg
            from psycopg import sql

            with psycopg.connect(self.postgres_root, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.postgres_db_name)))
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.saved_env)

    def _candidate(self, status='queued'):
        thread = self.models.InboxThread(organization_id=self.org_id, phone=self.phone)
        log = self.models.MessageLog(
            organization_id=self.org_id, message_body='Hello fixture', target='community', status='sent',
            success_count=1, failure_count=0, total_recipients=1,
            details=json.dumps([{'phone': self.phone, 'name': 'Fixture', 'sid': self.sid, 'success': True, 'status': status}]),
        )
        self.db.session.add_all([thread, log])
        self.db.session.flush()
        inbox = self.models.InboxMessage(organization_id=self.org_id, thread_id=thread.id, phone=self.phone, direction='outbound', body='Hello fixture', message_sid=self.sid, delivery_status=status)
        attempt = self.models.MessageDispatchAttempt(
            organization_id=self.org_id, message_log_id=log.id, logical_send_key='delivery-acceptance-proof',
            recipient_phone=self.phone, message_body_digest=hashlib.sha256(b'Hello fixture').hexdigest(),
            send_kind='blast', status='sent', provider_message_sid=self.sid,
        )
        self.db.session.add_all([inbox, attempt])
        self.db.session.commit()
        self.log_id, self.inbox_id, self.attempt_id = log.id, inbox.id, attempt.id
        self.original_log_details = log.details
        self.twilio.record_usage_candidates(self.org_id, [{'sid': self.sid, 'status': status, 'account_sid': self.profile.twilio_subaccount_sid}])
        return self._record()

    def _record(self):
        self.db.session.expire_all()
        return self.models.MessagingUsageRecord.query.filter_by(organization_id=self.org_id, message_sid=self.sid).one()

    def _message(self, status='sent', price=None, segments='1', **overrides):
        payload = dict(sid=self.sid, account_sid='AC' + 'a' * 32, direction='outbound-api', status=status,
                       price=price, price_unit='usd', num_segments=segments,
                       date_created=self.now, error_code=None, error_message=None)
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def _poll(self, status='sent', price=None, segments='1', *, at=None, include_expired=False, **overrides):
        self.poll_number += 1
        at = at or self.now + timedelta(minutes=2 ** self.poll_number)
        client = MagicMock()
        client.messages.return_value.fetch.return_value = self._message(status, price, segments, **overrides)
        with patch('app.services.twilio_service._client_for_usage_reconciliation', return_value=client):
            result = self.twilio.reconcile_messaging_usage(organization_id=self.org_id, include_expired=include_expired, now=at)
        return result, client

    def _callback(self, status, *, org_id=None, valid=True, account=None, sid=None, code=None):
        from twilio.request_validator import RequestValidator

        org_id = org_id or self.org_id
        path = f'/webhooks/twilio/delivery?organization_id={org_id}'
        payload = {'MessageSid': sid or self.sid, 'AccountSid': account or 'AC' + 'a' * 32, 'MessageStatus': status}
        if code is not None:
            payload.update(ErrorCode=str(code), ErrorMessage='Carrier failure ' + str(code))
        signature = RequestValidator(self.app.config['TWILIO_AUTH_TOKEN']).compute_signature('http://localhost' + path, payload)
        with self.app.app_context(), self.app.test_client() as client:
            return client.post(path, data=payload, headers={'X-Twilio-Signature': signature if valid else 'forged'})

    def _assert_acceptance_intact(self):
        self.db.session.expire_all()
        log = self.db.session.get(self.models.MessageLog, self.log_id)
        self.assertEqual((log.status, log.success_count, log.failure_count, log.details), ('sent', 1, 0, self.original_log_details))
        self.assertEqual(self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id).status, 'sent')

    def test_sent_is_not_final_delivery_and_later_failure_price_are_retained_once(self):
        self._candidate()
        summary, _ = self._poll('sent')
        record = self._record()
        self.assertEqual((record.delivery_status, record.cost_status, record.units_status), ('sent','pending','known'))
        self.assertEqual((record.reconciliation_status, summary['records_finalized']), ('pending', 0))
        self.assertIsNone(record.delivery_finalized_at)
        summary, _ = self._poll('undelivered', '-0.0175', error_code=30003, error_message='Unknown subscriber')
        record = self._record()
        self.assertEqual((record.delivery_status, record.cost_status, record.reconciliation_status), ('undelivered','known','finalized'))
        self.assertEqual(record.provider_cost, Decimal('0.0175'))
        self.assertEqual(summary['suppression_actions'], 1)
        self.assertEqual(self._callback('undelivered', code=30003).status_code, 204)
        self.assertEqual(self._callback('sent').status_code, 204)
        self.assertEqual(self._record().delivery_status, 'undelivered')
        self.assertEqual(self.models.MessagingUsageRecord.query.count(), 1)
        self.assertEqual(self.models.SuppressedContact.query.filter_by(phone=self.phone).count(), 1)
        self._assert_acceptance_intact()

    def test_terminal_delivery_waits_independently_for_late_price(self):
        self._candidate()
        self._poll('delivered')
        record = self._record()
        self.assertEqual((record.delivery_status, record.cost_status, record.reconciliation_status), ('delivered','pending','pending'))
        self._poll('delivered', '-0.0200')
        record = self._record()
        self.assertEqual((record.cost_status, record.reconciliation_status, record.provider_cost), ('known','finalized',Decimal('0.0200')))
        self._assert_acceptance_intact()

    def test_explicit_zero_cost_is_known_and_missing_cost_is_not(self):
        self._candidate()
        self._poll('sent', None)
        self.assertEqual(self._record().cost_status, 'pending')
        self._poll('delivered', '0')
        self.assertEqual((self._record().cost_status, self._record().provider_cost), ('known', Decimal('0')))

    def test_initial_zero_segments_are_pending_and_not_fabricated_from_price(self):
        self._candidate()
        self._poll('queued', '-0.01', segments='0')
        record = self._record()
        self.assertEqual((record.units_status, record.billable_units), ('pending', 0))
        self._poll('sent', '-0.01', segments='2')
        self.assertEqual((self._record().units_status, self._record().billable_units), ('known', 2))

    def test_receipt_ageout_is_unknown_then_a_late_signed_receipt_can_improve_it(self):
        self._candidate()
        self._poll('sent', '-0.01')
        self._poll('sent', '-0.01', at=self.now + timedelta(days=4))
        record = self._record()
        self.assertEqual((record.delivery_status, record.reconciliation_status), ('unknown','finalized'))
        self.assertEqual(self._callback('delivered').status_code, 204)
        self.assertEqual(self._record().delivery_status, 'delivered')

    def test_missing_cost_ages_out_unknown_with_no_automatic_infinite_poll_or_zero_charge(self):
        self._candidate()
        self._poll('sent', None, at=self.now + timedelta(days=8))
        record = self._record()
        self.assertEqual((record.delivery_status, record.cost_status, record.reconciliation_status), ('unknown','unknown','unknown'))
        self.assertIsNone(record.next_reconcile_at)
        summary, client = self._poll('delivered', '-0.02', at=self.now + timedelta(days=9))
        self.assertEqual(summary['records_seen'], 0)
        client.messages.assert_not_called()
        self._poll('delivered', '-0.02', at=self.now + timedelta(days=9), include_expired=True)
        self.assertEqual(self._record().cost_status, 'known')
        with self.assertRaises(ValueError):
            self.twilio.reconcile_messaging_usage(include_expired=True)

    def test_signed_callback_is_required_even_if_inbound_signature_flag_is_off(self):
        self._candidate()
        self.app.config['TWILIO_VALIDATE_INBOUND_SIGNATURE'] = False
        self.assertEqual(self._callback('delivered', valid=False).status_code, 403)
        self.assertEqual(self._record().delivery_status, 'queued')
        self.assertEqual(self._callback('delivered').status_code, 204)
        self.assertEqual(self._record().delivery_status, 'delivered')

    def test_callback_unknown_sid_wrong_account_and_wrong_tenant_do_not_mutate_record(self):
        self._candidate()
        other = self.models.Organization(name='Other delivery tenant', slug='other-delivery', status='active')
        profile = self.models.OrganizationMessagingProfile(organization=other, provider_mode='platform_managed', twilio_subaccount_sid='AC' + 'b' * 32)
        self.db.session.add_all([other, profile])
        self.db.session.commit()
        self.assertEqual(self._callback('delivered', org_id=other.id, account='AC' + 'b' * 32).status_code, 204)
        self.assertEqual(self._callback('delivered', account='AC' + 'b' * 32).status_code, 400)
        self.assertEqual(self._callback('delivered', sid='SM' + 'c' * 32).status_code, 204)
        self.assertEqual(self._record().delivery_status, 'queued')
        self.assertEqual(self.models.MessagingUsageRecord.query.count(), 1)

    def test_paused_provider_and_org_still_accept_authenticated_receipts(self):
        self._candidate()
        self.profile.set_provider_status('error')
        self.org.status = 'suspended'
        self.db.session.commit()
        self.assertEqual(self._callback('undelivered', code=30003).status_code, 204)
        self.assertEqual(self._record().delivery_status, 'undelivered')

    def test_error_provider_poll_uses_organization_read_client_not_master_fallback(self):
        self._candidate()
        self.profile.set_provider_status('error')
        self.db.session.commit()
        with patch('app.services.twilio_service._build_subaccount_client') as client, patch('app.services.twilio_service._master_client') as master:
            self.assertIs(self.twilio._client_for_usage_reconciliation(self.org_id), client.return_value)
            master.assert_not_called()

    def test_duplicate_and_stale_observations_never_regress_terminal_delivery_or_known_cost(self):
        self._candidate()
        self._poll('delivered', '-0.02')
        cost_at = self._record().cost_finalized_at
        for status in ('queued','sent','delivered','delivered'):
            self.assertEqual(self._callback(status).status_code, 204)
        self.twilio.record_usage_candidates(self.org_id, [{'sid': self.sid, 'status': 'sent'}])
        record = self._record()
        self.assertEqual((record.delivery_status, record.cost_status, record.cost_finalized_at), ('delivered','known',cost_at))
        self.assertEqual(sum(row['status']=='delivered' for row in json.loads(record.delivery_evidence_json)), 1)

    def test_conflicting_terminal_receipts_are_visible_and_never_claim_delivery_success(self):
        self._candidate()
        self._callback('delivered')
        self._callback('undelivered', code=30003)
        record = self._record()
        self.assertEqual(record.delivery_status, 'conflict')
        self.assertEqual({row['status'] for row in json.loads(record.delivery_evidence_json)} & {'delivered','undelivered'}, {'delivered','undelivered'})
        self._callback('delivered')
        self.assertEqual(self._record().delivery_status, 'conflict')

    def test_inbox_and_log_show_current_receipt_error_without_rewriting_acceptance(self):
        self._candidate()
        self._poll('undelivered', '-0.01', error_code=30003, error_message='Unknown subscriber')
        self.db.session.expire_all()
        inbox = self.db.session.get(self.models.InboxMessage, self.inbox_id)
        self.assertEqual((inbox.delivery_status, inbox.delivery_error), ('undelivered','Unknown subscriber'))
        response = self.client.get(f'/logs/{self.log_id}')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Accepted', body)
        self.assertIn('Undelivered', body)
        self.assertIn('Unknown subscriber', body)
        response = self.client.get('/inbox?thread=' + str(inbox.thread_id))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Undelivered', response.get_data(as_text=True))
        self._assert_acceptance_intact()

    def test_invalid_provider_identity_and_network_failure_do_not_apply_foreign_receipts(self):
        self._candidate()
        for overrides in ({'sid':'SM'+'b'*32},{'account_sid':'AC'+'b'*32},{'direction':'inbound'}):
            with self.subTest(overrides=overrides):
                summary, _ = self._poll('delivered', '-0.02', **overrides)
                self.assertEqual(summary['records_errored'], 1)
                self.assertEqual(self._record().delivery_status, 'queued')

    def test_settlement_uses_complete_cost_and_units_without_waiting_for_delivery(self):
        self._candidate()
        start, end = self.twilio.previous_billing_period_window()
        self._poll('sent', '-0.02', date_created=end - timedelta(hours=1), at=end + timedelta(hours=1), include_expired=True)
        period = self.models.OrganizationUsageBillingPeriod.query.filter_by(organization_id=self.org_id).one()
        self.assertIsNone(self.twilio.usage_period_billing_block_reason(period))
        self.assertEqual(self._record().delivery_status, 'sent')
        record = self._record()
        record.cost_status = 'pending'
        self.db.session.commit()
        self.assertEqual(self.twilio.usage_period_billing_block_reason(period), 'usage_facts_incomplete')

    def test_historical_recovery_preserves_period_links_and_holds_billing_for_review(self):
        record = self._candidate()
        start, end = self.twilio.previous_billing_period_window()
        record.billing_period_start, record.billing_period_end = start, end
        record.reconciliation_status, record.twilio_message_status = 'finalized', 'sent'
        record.billing_review_required = True
        self.db.session.commit()
        self._poll('delivered', '-0.03', date_created=start - timedelta(days=5))
        record = self._record()
        self.assertEqual((record.billing_period_start,record.billing_period_end), (start,end))
        period = self.models.OrganizationUsageBillingPeriod.query.filter_by(organization_id=self.org_id).one()
        self.assertEqual((period.status,period.settlement_due_at), ('review_required',None))
        self.assertEqual(self.twilio.usage_period_billing_block_reason(period), 'historical_usage_review_required')
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.filter_by(action='usage_historical_review').count(), 1)
        self._callback('delivered')
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.filter_by(action='usage_historical_review').count(), 1)

    def test_settled_period_snapshot_is_frozen_and_variance_creates_one_incident(self):
        self._candidate()
        start, end = self.twilio.previous_billing_period_window()
        period = self.models.OrganizationUsageBillingPeriod(
            organization_id=self.org_id, period_start=start, period_end=end, used_units=2, included_units=0,
            overage_units=2, sell_amount=Decimal('0.06'), status='posted', invoiced_units=2,
            stripe_invoice_item_id='ii-retained', stripe_invoice_id='in-retained',
        )
        self.db.session.add(period)
        self.db.session.commit()
        period_id = period.id
        self._poll('delivered', '-0.01', date_created=start + timedelta(days=2))
        self.twilio.upsert_closed_usage_billing_periods()
        self.db.session.expire_all()
        period = self.db.session.get(self.models.OrganizationUsageBillingPeriod, period_id)
        self.assertEqual((period.used_units,period.invoiced_units,period.sell_amount,period.status,period.stripe_invoice_item_id,period.stripe_invoice_id), (2,2,Decimal('0.06'),'posted','ii-retained','in-retained'))
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.filter_by(action='usage_settled_variance').count(), 1)

    def test_candidate_sid_cannot_be_reused_for_another_tenant(self):
        self._candidate()
        other = self.models.Organization(name='Other usage tenant', slug='other-usage', status='active')
        self.db.session.add(other)
        self.db.session.commit()
        with self.assertRaises(ValueError):
            self.twilio.record_usage_candidates(other.id, [{'sid':self.sid,'status':'sent'}])
        self.db.session.rollback()
        self.assertEqual(self._record().organization_id, self.org_id)

    def test_status_callback_url_is_bound_to_the_organization_without_provider_resource_updates(self):
        self.app.config['TWILIO_BROWSER_FAKE_SENDS'] = False
        provider = self.twilio.TwilioService.__new__(self.twilio.TwilioService)
        provider.organization_id, provider.account_sid, provider.from_number = self.org_id, self.profile.twilio_subaccount_sid, self.profile.from_number
        provider.messaging_service_sid, provider.client = self.profile.messaging_service_sid, MagicMock()
        provider.client.messages.create.return_value = SimpleNamespace(sid=self.sid, status='queued', account_sid=provider.account_sid)
        result = provider.send_message(self.phone, 'Operational fixture', send_kind='manual_live_test')
        self.assertTrue(result['success'])
        self.assertEqual(provider.client.messages.create.call_args.kwargs['status_callback'], f'https://app.example.test/webhooks/twilio/delivery?organization_id={self.org_id}')
        provider.client.messaging.v1.services.assert_not_called()

    def test_callback_storage_failure_rolls_back_and_retry_retains_receipt_once(self):
        self._candidate()
        with patch('app.services.twilio_service.apply_usage_record_failure_suppression', side_effect=RuntimeError('isolated persistence failure')):
            self.assertEqual(self._callback('undelivered', code=30003).status_code, 500)
        self.assertEqual(self._record().delivery_status, 'queued')
        self.assertEqual(self.models.SuppressedContact.query.count(), 0)
        self.assertEqual(self._callback('undelivered', code=30003).status_code, 204)
        self.assertEqual(self._record().delivery_status, 'undelivered')
        self.assertEqual(self.models.SuppressedContact.query.count(), 1)

    def test_cost_only_poll_does_not_advance_delivery_revision(self):
        self._candidate()
        self._poll('sent')
        delivery_at = self._record().delivery_updated_at
        self._poll('sent', '-0.02')
        self.assertEqual(self._record().delivery_updated_at, delivery_at)

    def test_real_sdk_clients_get_explicit_read_and_submission_timeouts_without_network(self):
        from twilio.rest import Client

        client = Client('AC'+'a'*32, 'fake-no-network-token')
        self.assertIsNone(client.http_client.timeout)
        self.twilio._bounded_message_http_client(client)
        self.assertEqual(client.http_client.timeout, 30)
        self.twilio._bounded_message_http_client(client, reconciliation=True)
        self.assertEqual(client.http_client.timeout, 10)
        client.http_client.timeout = 3
        self.twilio._bounded_message_http_client(client)
        self.assertEqual(client.http_client.timeout, 3)

    def test_zero_terminal_units_wait_for_real_cost_facts(self):
        self._candidate()
        self._poll('failed',None,segments='0')
        self.assertEqual(self._record().units_status, 'pending')
        self._poll('failed','0',segments='0')
        self.assertEqual((self._record().units_status,self._record().billable_units), ('known',0))

    def test_migration_reopens_historical_sent_unknown_price_additively_and_idempotently(self):
        from sqlalchemy import text

        self.db.session.remove()
        with self.db.engine.begin() as connection:
            self.models.MessagingUsageRecord.__table__.drop(connection)
            connection.execute(text('''CREATE TABLE messaging_usage_records (
                id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL,
                message_sid VARCHAR(64) NOT NULL UNIQUE, direction VARCHAR(10) NOT NULL DEFAULT 'outbound',
                source VARCHAR(20) NOT NULL DEFAULT 'blast', twilio_subaccount_sid VARCHAR(64),
                twilio_message_status VARCHAR(30), provider_currency VARCHAR(8) NOT NULL DEFAULT 'usd',
                provider_cost NUMERIC(12,4) NOT NULL DEFAULT 0, sell_rate NUMERIC(12,4) NOT NULL DEFAULT 0,
                sell_amount NUMERIC(12,4) NOT NULL DEFAULT 0, margin NUMERIC(12,4) NOT NULL DEFAULT 0,
                billable_units INTEGER NOT NULL DEFAULT 0, billable BOOLEAN NOT NULL DEFAULT FALSE,
                reconciliation_status VARCHAR(20) NOT NULL DEFAULT 'pending', billing_period_start TIMESTAMP,
                billing_period_end TIMESTAMP, last_error TEXT, created_at TIMESTAMP NOT NULL, reconciled_at TIMESTAMP
            )'''))
            start, end = self.twilio.previous_billing_period_window()
            for row_id, sid, status, cost in [(1,'SM'+'d'*32,'sent','0'),(2,'SM'+'e'*32,'delivered','0.0200')]:
                connection.execute(text('''INSERT INTO messaging_usage_records
                    (id, organization_id, message_sid, twilio_subaccount_sid, twilio_message_status,
                     provider_cost, billable_units, billable, reconciliation_status, billing_period_start,
                     billing_period_end, created_at, reconciled_at)
                    VALUES (:id,:org,:sid,:account,:status,:cost,1,TRUE,'finalized',:start,:end,:created,:created)'''),
                    {'id':row_id,'org':self.org_id,'sid':sid,'account':'AC'+'a'*32,'status':status,'cost':cost,
                     'start':start,'end':end,'created':start+timedelta(days=3)})
            migration = importlib.import_module('app.saas_migrations.023_separate_delivery_and_usage_finality')
            migration.apply(connection, MagicMock())
            migration.apply(connection, MagicMock())
        self.db.session.expire_all()
        records = self.models.MessagingUsageRecord.query.order_by(self.models.MessagingUsageRecord.id).all()
        self.assertEqual(len(records), 2)
        self.assertEqual((records[0].reconciliation_status,records[0].cost_status,records[0].billing_review_required), ('pending','pending',True))
        self.assertEqual((records[1].delivery_status,records[1].cost_status,records[1].units_status), ('delivered','known','known'))
        self.assertEqual((records[0].billing_period_start,records[0].billing_period_end), (start,end))
        self.assertEqual(records[0].provider_cost, Decimal('0'))
        self.sid = 'SM'+'d'*32
        self._poll('undelivered','-0.0312',date_created=start-timedelta(days=2))
        record = self._record()
        self.assertEqual((record.delivery_status,record.provider_cost,record.billing_period_start), ('undelivered',Decimal('0.0312'),start))
        self.assertTrue(record.billing_review_required)

    def test_postgres_concurrent_duplicate_callbacks_create_one_receipt_and_suppression(self):
        if not self.postgres_root:
            self.skipTest('Concurrent callbacks require isolated PostgreSQL.')
        self._candidate()
        barrier = threading.Barrier(3)
        results, errors = [], []

        def deliver():
            try:
                barrier.wait(timeout=5)
                results.append(self._callback('undelivered',code=30003).status_code)
            except BaseException as error:
                errors.append(error)

        self.db.session.rollback()
        workers = [threading.Thread(target=deliver) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(10)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(results, [204,204])
        record = self._record()
        self.assertEqual(record.delivery_status, 'undelivered')
        self.assertEqual(sum(row['status']=='undelivered' for row in json.loads(record.delivery_evidence_json)), 1)
        self.assertEqual(self.models.SuppressedContact.query.count(), 1)

    def test_postgres_callback_wins_over_concurrent_stale_poll_and_suppression_is_once(self):
        if not self.postgres_root:
            self.skipTest('Separate-session callback/poll race requires isolated PostgreSQL.')
        self._candidate()
        fetched, release = threading.Event(), threading.Event()
        errors = []
        client = MagicMock()

        def stale_provider():
            fetched.set()
            if not release.wait(8):
                raise TimeoutError('Test barrier not released')
            return self._message('sent', '-0.02')

        client.messages.return_value.fetch.side_effect = stale_provider

        def worker():
            try:
                self.twilio.reconcile_messaging_usage(organization_id=self.org_id, now=self.now+timedelta(minutes=2))
            except BaseException as error:
                errors.append(error)

        def in_app_worker():
            with self.app.app_context():
                worker()

        self.db.session.rollback()
        poller = threading.Thread(target=in_app_worker)
        with patch('app.services.twilio_service._client_for_usage_reconciliation', return_value=client):
            poller.start()
            try:
                self.assertTrue(fetched.wait(5))
                self.assertEqual(self._callback('undelivered', code=30003).status_code, 204)
            finally:
                release.set()
                poller.join(10)
        self.assertFalse(poller.is_alive())
        self.assertEqual(errors, [])
        record = self._record()
        self.assertEqual((record.delivery_status,record.provider_cost), ('undelivered',Decimal('0.02')))
        self.assertEqual(self.models.SuppressedContact.query.filter_by(phone=self.phone).count(), 1)
        self._assert_acceptance_intact()

"""Verified reconciliation is read-only at the provider and never resends."""
import hashlib
import importlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import MagicMock, patch
from uuid import uuid4


class TestOutboundReview(unittest.TestCase):
    def setUp(self):
        self.saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix="twinevia-outbound-review-")
        self.postgres_root = os.environ.get('AUDIT_REVIEW_DATABASE_ROOT')
        self.postgres_db_name = None
        if self.postgres_root:
            parsed = urlsplit(self.postgres_root)
            if parsed.hostname != '127.0.0.1' or parsed.port != 55432 or parsed.username != 'audit' or parsed.path != '/postgres':
                raise RuntimeError('Outbound review tests accept only the isolated local PostgreSQL audit root.')
            import psycopg
            from psycopg import sql

            self.postgres_db_name = 'outbound_review_' + uuid4().hex
            with psycopg.connect(self.postgres_root, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(self.postgres_db_name)))
            database_url = urlunsplit(('postgresql+psycopg', parsed.netloc, '/' + self.postgres_db_name, '', ''))
        else:
            database_url = 'sqlite:///' + self.tmp.name + '/review.db'
        os.environ.update(
            DATABASE_URL=database_url, SAAS_MODE='1', FLASK_DEBUG='1', SCHEDULER_ENABLED='0',
            PYTHON_DOTENV_DISABLED='1', AUTH_ALERTS_ENABLED='0', SECRET_KEY='isolated-review-test-secret',
        )
        import app.config

        importlib.reload(app.config)
        from app import create_app, db, models
        from app.tenant import organization_context
        from app.services import outbound_review_service

        self.db, self.models = db, models
        self.organization_context = organization_context
        self.service = outbound_review_service
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, TWILIO_BROWSER_FAKE_SENDS=False)
        self.context = self.app.app_context()
        self.context.push()
        self.assertEqual(self.app.config['SQLALCHEMY_DATABASE_URI'], database_url)
        db.create_all()
        org = models.Organization(name='Review fixture', slug='review-fixture', status='active')
        profile = models.OrganizationMessagingProfile(
            organization=org, provider_mode='platform_managed', twilio_subaccount_sid='AC' + 'a' * 32,
            messaging_service_sid='MG' + 'a' * 32, from_number='+17205550001', status='active', provider_status='active',
            sender_review_status='approved',
        )
        subscription = models.OrganizationSubscription(organization=org, status='complimentary')
        admin = models.AppUser(username='review-admin', password_hash='isolated-no-login', is_platform_admin=True)
        owner = models.AppUser(username='review-owner', password_hash='isolated-no-login', is_platform_admin=False)
        db.session.add_all([org, profile, subscription, admin, owner])
        db.session.flush()
        self.org_id, self.actor_id, self.owner_id = org.id, admin.id, owner.id
        self.body = 'Hello Fixture\nExact personalized body.'
        self.phone = '+17205550145'
        self.sid = 'SM' + '1' * 32
        self.template = 'Hello {name}\nExact personalized body.'
        log = models.MessageLog(organization_id=org.id, message_body=self.template, target='community', status='ambiguous', total_recipients=1)
        db.session.add(log)
        db.session.flush()
        from app.services.outbound_request_service import intent_digest

        intent = {'target': 'community', 'event_id': None, 'test_mode': False, 'message_body': self.template}
        request = models.OutboundRequest(
            organization_id=org.id, actor_user_id=owner.id, actor_key=f'user:{owner.id}',
            request_key='review-fixture', kind='blast', payload_digest=intent_digest('blast', intent),
            payload_json=json.dumps({'intent': intent, 'message_body': self.template, 'recipients': [{'phone': self.phone, 'name': 'Fixture'}]}),
            message_log_id=log.id, status='ambiguous', result_json=json.dumps({'details': [{'status': 'ambiguous'}]}),
        )
        db.session.add(request)
        db.session.flush()
        prefix = f'blast-log:{log.id}'
        self.logical_key = prefix + ':' + hashlib.sha256(f'{prefix}|{self.phone}|{self.body}'.encode()).hexdigest()
        attempt = models.MessageDispatchAttempt(
            organization_id=org.id, message_log_id=log.id, logical_send_key=self.logical_key,
            recipient_phone=self.phone, message_body_digest=hashlib.sha256(self.body.encode()).hexdigest(),
            send_kind='blast', status='ambiguous', sending_at=models.utc_now().replace(tzinfo=None), last_error='Original unknown outcome',
        )
        db.session.add(attempt)
        db.session.commit()
        self.request_id, self.attempt_id = request.id, attempt.id
        self.provider = MagicMock()
        self.record = SimpleNamespace(
            sid=self.sid, account_sid='AC' + 'a' * 32, direction='outbound-api', to=self.phone,
            body=self.body, status='sent', error_code=None, date_created=models.utc_now(),
        )
        self.provider.messages.return_value.fetch.return_value = self.record

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

    def _resolve(self, **overrides):
        params = dict(attempt_id=self.attempt_id, message_sid=self.sid, actor_user_id=self.actor_id, evidence_reference='local-evidence/provider-message-1')
        params.update(overrides)
        with patch('app.services.twilio_service._client_for_usage_reconciliation', return_value=self.provider):
            return self.service.resolve_outbound_attempt(self.org_id, self.request_id, **params)

    def _assert_unchanged(self):
        self.db.session.rollback()
        self.assertEqual(self.db.session.get(self.models.OutboundRequest, self.request_id).status, 'ambiguous')
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        self.assertEqual(attempt.status, 'ambiguous')
        self.assertIsNone(attempt.provider_message_sid)
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.count(), 0)
        self.provider.messages.create.assert_not_called()

    def test_default_inspection_reads_no_provider_and_changes_nothing(self):
        with patch('app.services.twilio_service._client_for_usage_reconciliation') as provider_factory:
            result = self.service.inspect_outbound_request(self.org_id, self.request_id)
        provider_factory.assert_not_called()
        self.assertEqual(result['attempts'][0]['attempt_id'], self.attempt_id)
        self.assertNotIn(self.body, json.dumps(result))
        self.assertNotIn(self.phone, json.dumps(result))
        self._assert_unchanged()

    def test_cli_defaults_to_inspection_and_requires_explicit_target_ids(self):
        from app.outbound_review import main

        output = io.StringIO()
        with patch('app.outbound_review.create_app', return_value=self.app), redirect_stdout(output):
            self.assertEqual(main(['--organization-id', str(self.org_id), '--request-id', str(self.request_id)]), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'ambiguous')
        with self.assertRaises(SystemExit) as error:
            main(['--resolve', '--organization-id', str(self.org_id), '--request-id', str(self.request_id)])
        self.assertEqual(error.exception.code, 2)
        self._assert_unchanged()

    def test_verified_provider_acceptance_is_audited_and_original_request_recovered(self):
        result = self._resolve()
        self.assertEqual((result['attempt_status'], result['request_status']), ('sent', 'pending'))
        self.provider.messages.assert_called_once_with(self.sid)
        self.provider.messages.return_value.fetch.assert_called_once_with()
        self.provider.messages.create.assert_not_called()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        self.assertEqual((attempt.status, attempt.provider_message_sid), ('sent', self.sid))
        self.assertEqual((request.id, request.request_key), (self.request_id, 'review-fixture'))
        self.assertIsNotNone(request.next_enqueue_at)
        review = json.loads(request.result_json)['operator_reviews'][0]
        self.assertEqual((review['actor_user_id'], review['evidence_reference']), (self.actor_id, 'local-evidence/provider-message-1'))
        audit = self.models.OrganizationProviderAuditLog.query.one()
        self.assertEqual((audit.action, audit.actor_user_id), ('outbound_attempt_review', self.actor_id))
        self.assertNotIn(self.body, audit.metadata_json)

    def test_verified_terminal_provider_failure_remains_nonretryable(self):
        self.record.status = 'undelivered'
        self.record.error_code = '30007'
        result = self._resolve()
        self.assertEqual(result['attempt_status'], 'failed')
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        self.assertIn('undelivered', attempt.last_error)
        self.assertEqual(attempt.provider_message_sid, self.sid)
        self.provider.messages.create.assert_not_called()

    def test_unresolved_no_sid_owner_or_missing_evidence_cannot_resolve(self):
        for overrides in ({'message_sid': ''}, {'actor_user_id': self.owner_id}, {'evidence_reference': ''}):
            with self.subTest(overrides=overrides), self.assertRaises(self.service.OutboundReviewError):
                self._resolve(**overrides)
            self._assert_unchanged()
        self.provider.messages.assert_not_called()

    def test_wrong_tenant_or_request_attempt_linkage_is_rejected(self):
        with self.assertRaises(self.service.OutboundReviewError):
            self.service.inspect_outbound_request(self.org_id + 100, self.request_id)
        with self.organization_context(self.org_id + 100), self.assertRaises(self.service.OutboundReviewError):
            self.service.inspect_outbound_request(self.org_id, self.request_id)
        with self.assertRaises(self.service.OutboundReviewError):
            self._resolve(attempt_id=self.attempt_id + 100)
        self._assert_unchanged()

    def test_wrong_account_recipient_body_direction_or_sid_is_rejected(self):
        for field, value in (
            ('account_sid', 'AC' + 'b' * 32), ('to', '+17205550999'),
            ('body', self.body + ' '), ('direction', 'inbound'), ('sid', 'SM' + '2' * 32),
            ('status', 'unknown'),
        ):
            with self.subTest(field=field):
                original = getattr(self.record, field)
                setattr(self.record, field, value)
                with self.assertRaises(self.service.OutboundReviewError):
                    self._resolve()
                self._assert_unchanged()
                setattr(self.record, field, original)

    def test_provider_lookup_failure_does_not_change_local_state(self):
        self.provider.messages.return_value.fetch.side_effect = TimeoutError('isolated provider read timeout')
        with self.assertRaises(self.service.OutboundReviewError):
            self._resolve()
        self._assert_unchanged()

    def test_unrelated_old_or_later_identical_message_and_missing_timestamps_are_rejected(self):
        for value in (self.models.utc_now() - timedelta(days=1), self.models.utc_now() + timedelta(minutes=10), None):
            with self.subTest(provider_date=value):
                self.record.date_created = value
                with self.assertRaises(self.service.OutboundReviewError):
                    self._resolve()
                self._assert_unchanged()
        self.record.date_created = self.models.utc_now()
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        attempt.sending_at = None
        self.db.session.commit()
        with self.assertRaises(self.service.OutboundReviewError):
            self._resolve()
        self._assert_unchanged()

    def test_known_accepted_request_is_never_reopened_or_downgraded(self):
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        request.status = 'accepted'
        self.db.session.commit()
        with self.assertRaises(self.service.OutboundReviewError):
            self._resolve()
        self.assertEqual(request.status, 'accepted')
        self.provider.messages.assert_not_called()
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.count(), 0)

    def test_request_finalized_during_provider_fetch_is_not_overwritten(self):
        def finalize_during_read():
            request = self.db.session.get(self.models.OutboundRequest, self.request_id)
            request.status = 'accepted'
            self.db.session.commit()
            return self.record

        self.provider.messages.return_value.fetch.side_effect = finalize_during_read
        with self.assertRaises(self.service.OutboundReviewError):
            self._resolve()
        self.db.session.rollback()
        self.assertEqual(self.db.session.get(self.models.OutboundRequest, self.request_id).status, 'accepted')
        self.assertEqual(self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id).status, 'ambiguous')
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.count(), 0)

    def test_review_history_survives_final_completion_and_legacy_is_rejected(self):
        from app.services.outbound_request_service import complete_request

        self._resolve()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        complete_request(request, {'success': True, 'details': []})
        self.db.session.commit()
        self.assertEqual(len(json.loads(request.result_json)['operator_reviews']), 1)
        self.app.config['SAAS_MODE'] = False
        with self.assertRaises(self.service.OutboundReviewError):
            self.service.inspect_outbound_request(self.org_id, self.request_id)

    def test_recovery_finalizes_same_verified_attempt_without_provider_submission(self):
        from app.services.recipient_permission_service import record_permission_evidence
        from app.services.twilio_service import TwilioService
        from app.tasks import send_bulk_job

        self._resolve()
        record_permission_evidence(
            self.org_id, self.phone, purpose='community', decision='granted', source='owner_verified',
            evidence_key='review-recovery-fixture', verified=True, metadata={'proof': 'Isolated fixture consent'},
        )
        self.db.session.commit()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        service = TwilioService.__new__(TwilioService)
        service.organization_id = self.org_id
        service.account_sid = 'AC' + 'a' * 32
        service.client = MagicMock()
        with patch('app.tasks.create_app', return_value=self.app), patch('app.tasks.get_twilio_service', return_value=service):
            send_bulk_job(request.message_log_id, self.org_id, [{'phone': self.phone, 'name': 'Fixture'}], self.template, delay=0)
        self.db.session.expire_all()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        self.assertEqual(request.status, 'accepted')
        self.assertEqual(len(json.loads(request.result_json)['operator_reviews']), 1)
        self.assertEqual(self.models.MessageDispatchAttempt.query.count(), 1)
        service.client.messages.create.assert_not_called()
        self.provider.messages.create.assert_not_called()

    def test_existing_provider_sid_cannot_be_reassigned_to_another_attempt(self):
        other = self.models.MessageDispatchAttempt(
            organization_id=self.org_id, logical_send_key='other-known-attempt', recipient_phone=self.phone,
            message_body_digest='b' * 64, send_kind='blast', status='sent', provider_message_sid=self.sid,
        )
        self.db.session.add(other)
        self.db.session.commit()
        with self.assertRaises(self.service.OutboundReviewError):
            self._resolve()
        self._assert_unchanged()

    def test_reply_review_uses_request_logical_key_and_preserves_conversation_identity(self):
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        request.kind = 'reply'
        request.message_log_id = None
        thread = self.models.InboxThread(organization_id=self.org_id, phone=self.phone)
        self.db.session.add(thread)
        self.db.session.flush()
        request.thread_id = thread.id
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        attempt.message_log_id = None
        attempt.logical_send_key = f'reply-request:{self.request_id}'
        attempt.send_kind = 'manual_reply'
        self.db.session.commit()
        result = self._resolve()
        self.assertEqual(result['attempt_status'], 'sent')
        self.assertEqual(request.thread_id, thread.id)
        self.assertEqual(self.models.InboxMessage.query.count(), 0)
        self.provider.messages.create.assert_not_called()

    def test_sender_exception_cannot_overwrite_concurrent_verified_resolution(self):
        from app.services.twilio_service import TwilioService

        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        attempt.status = 'queued'
        self.db.session.commit()
        provider = TwilioService.__new__(TwilioService)
        provider.organization_id = self.org_id
        provider.account_sid = 'AC' + 'a' * 32

        def resolve_before_lost_response(*_args, **_kwargs):
            self._resolve()
            raise TimeoutError('Response lost after independent provider-evidence review')

        with patch.object(provider, 'send_message', side_effect=resolve_before_lost_response):
            result = provider.send_message_with_attempt(
                self.phone, self.body, self.logical_key, attempt.message_log_id, None, 'blast',
            )
        self.assertTrue(result['success'])
        self.assertEqual(result['sid'], self.sid)
        self.assertEqual(self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id).status, 'sent')

    def test_postgres_non_utc_session_preserves_utc_submission_and_review_timestamps(self):
        if not self.postgres_root:
            self.skipTest('Explicit non-UTC PostgreSQL timestamp verification requires the isolated audit root.')
        from sqlalchemy import text
        from app.services.twilio_service import TwilioService

        self.db.session.execute(text("SET TIME ZONE 'America/Denver'"))
        self.assertEqual(self.db.session.execute(text('SHOW TimeZone')).scalar_one(), 'America/Denver')
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        attempt.status = 'queued'
        self.db.session.commit()
        provider = TwilioService.__new__(TwilioService)
        provider.organization_id = self.org_id
        provider.account_sid = 'AC' + 'a' * 32
        before = self.models.utc_now().replace(tzinfo=None)
        with patch.object(provider, 'send_message', return_value={'success': False, 'status': 'failed', 'error': 'fixture rejection'}):
            provider.send_message_with_attempt(self.phone, self.body, self.logical_key, attempt.message_log_id, None, 'blast')
        self.db.session.expire_all()
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        after = self.models.utc_now().replace(tzinfo=None)
        self.assertLessEqual(before, attempt.sending_at)
        self.assertLessEqual(attempt.sending_at, after)
        self.assertLessEqual(before, attempt.resolved_at)
        self.assertLessEqual(attempt.resolved_at, after)
        attempt.status = 'ambiguous'
        attempt.resolved_at = None
        self.db.session.commit()
        self.record.date_created = self.models.utc_now()
        self._resolve()
        self.db.session.expire_all()
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        audit = self.models.OrganizationProviderAuditLog.query.one()
        after_review = self.models.utc_now().replace(tzinfo=None)
        self.assertLessEqual(before, attempt.resolved_at)
        self.assertLessEqual(attempt.resolved_at, after_review)
        self.assertLessEqual(before, audit.created_at)
        self.assertLessEqual(audit.created_at, after_review)

    def _known_attempt(self, *, status='sent'):
        attempt = self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id)
        attempt.status = status
        attempt.provider_message_sid = self.sid if status == 'sent' else None
        if status == 'sent':
            attempt.last_error = None
        self.db.session.commit()
        return attempt

    def _finalize_known(self, **overrides):
        params = dict(actor_user_id=self.actor_id, evidence_reference='local-evidence/complete-known-attempts')
        params.update(overrides)
        return self.service.finalize_known_outbound_request(self.org_id, self.request_id, **params)

    def test_known_sent_attempt_recovers_stale_ambiguous_request_without_provider_or_enqueue(self):
        from app.services.recipient_permission_service import record_permission_evidence
        from app.services.twilio_service import TwilioService
        from app.tasks import send_bulk_job

        self._known_attempt()
        with patch('app.services.twilio_service._client_for_usage_reconciliation') as reads, patch(
            'app.services.twilio_service.get_twilio_service', side_effect=AssertionError('Review must not construct a provider'),
        ), patch('app.services.outbound_request_service.get_queue', side_effect=AssertionError('Review must not access a queue')):
            result = self._finalize_known()
        reads.assert_not_called()
        self.assertEqual((result['request_status'], result['accepted_count'], result['known_attempts']), ('pending', 1, 1))
        self.assertFalse(result['enqueued'])
        self.assertEqual(self.db.session.get(self.models.MessageDispatchAttempt, self.attempt_id).status, 'sent')
        record_permission_evidence(
            self.org_id, self.phone, purpose='community', decision='granted', source='owner_verified',
            evidence_key='known-attempt-recovery', verified=True, metadata={'proof': 'Retained fixture consent'},
        )
        self.db.session.commit()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        provider = TwilioService.__new__(TwilioService)
        provider.organization_id, provider.account_sid, provider.client = self.org_id, 'AC' + 'a' * 32, MagicMock()
        with patch('app.tasks.create_app', return_value=self.app), patch('app.tasks.get_twilio_service', return_value=provider):
            send_bulk_job(request.message_log_id, self.org_id, [{'phone': self.phone, 'name': 'Fixture'}], self.template, delay=0)
        self.db.session.expire_all()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        self.assertEqual(request.status, 'accepted')
        self.assertEqual(json.loads(request.result_json)['operator_reviews'][0]['action'], 'finalize_known')
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.one().action, 'outbound_known_finalize')
        self.assertEqual(self.models.MessageDispatchAttempt.query.count(), 1)
        provider.client.messages.create.assert_not_called()

    def test_finalize_known_rejects_incomplete_or_unresolved_frozen_audience(self):
        self._known_attempt()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        payload = json.loads(request.payload_json)
        second_phone = '+17205550146'
        payload['recipients'].append({'phone': second_phone, 'name': 'Second'})
        request.payload_json = json.dumps(payload)
        self.db.session.commit()
        with self.assertRaises(self.service.OutboundReviewError):
            self._finalize_known()
        self.db.session.rollback()
        prefix = f'blast-log:{request.message_log_id}'
        second_body = self.template.replace('{name}', 'Second')
        key = prefix + ':' + hashlib.sha256(f'{prefix}|{second_phone}|{second_body}'.encode()).hexdigest()
        attempt = self.models.MessageDispatchAttempt(
            organization_id=self.org_id, message_log_id=request.message_log_id, logical_send_key=key,
            recipient_phone=second_phone, message_body_digest=hashlib.sha256(second_body.encode()).hexdigest(),
            send_kind='blast', status='queued',
        )
        self.db.session.add(attempt)
        self.db.session.commit()
        for status in ('queued', 'sending', 'ambiguous'):
            with self.subTest(status=status):
                attempt.status = status
                self.db.session.commit()
                with self.assertRaises(self.service.OutboundReviewError):
                    self._finalize_known()
                self.db.session.rollback()
                self.assertEqual(self.db.session.get(self.models.OutboundRequest, self.request_id).status, 'ambiguous')
                self.assertEqual(self.models.OrganizationProviderAuditLog.query.count(), 0)

    def test_finalize_known_preserves_a_known_failed_attempt_without_sid_or_retry(self):
        attempt = self._known_attempt(status='failed')
        attempt.last_error = 'Known provider rejection'
        self.db.session.commit()
        result = self._finalize_known()
        self.assertEqual((result['accepted_count'], result['failed_count']), (0, 1))
        self.assertEqual((attempt.status, attempt.last_error), ('failed', 'Known provider rejection'))
        self.assertIsNone(attempt.provider_message_sid)

    def test_finalize_known_rejects_identity_scope_extra_attempts_and_missing_acceptance_sid(self):
        attempt = self._known_attempt()
        other = self.models.Organization(name='Other known review org', slug='other-known-review', status='active')
        self.db.session.add(other)
        self.db.session.commit()
        for field, value in (
            ('recipient_phone', '+17205550999'), ('message_body_digest', 'b' * 64),
            ('logical_send_key', 'wrong-prefix:' + 'b' * 64), ('send_kind', 'manual_reply'),
            ('organization_id', other.id), ('provider_message_sid', None),
        ):
            with self.subTest(field=field):
                original = getattr(attempt, field)
                setattr(attempt, field, value)
                self.db.session.commit()
                with self.assertRaises(self.service.OutboundReviewError):
                    self._finalize_known()
                self.db.session.rollback()
                self.assertEqual(self.db.session.get(self.models.OutboundRequest, self.request_id).status, 'ambiguous')
                self.assertEqual(self.models.OrganizationProviderAuditLog.query.count(), 0)
                setattr(attempt, field, original)
                self.db.session.commit()
        extra = self.models.MessageDispatchAttempt(
            organization_id=self.org_id, message_log_id=attempt.message_log_id, logical_send_key='extra-linked-attempt',
            recipient_phone='+17205550146', message_body_digest='b' * 64, send_kind='blast', status='sent',
        )
        self.db.session.add(extra)
        self.db.session.commit()
        with self.assertRaises(self.service.OutboundReviewError):
            self._finalize_known()

    def test_finalize_known_requires_actor_evidence_tenant_and_preserves_accepted_terminal(self):
        self._known_attempt()
        for overrides in ({'actor_user_id': self.owner_id}, {'evidence_reference': ''}):
            with self.subTest(overrides=overrides), self.assertRaises(self.service.OutboundReviewError):
                self._finalize_known(**overrides)
            self.db.session.rollback()
        with self.organization_context(self.org_id + 100), self.assertRaises(self.service.OutboundReviewError):
            self._finalize_known()
        with self.assertRaises(self.service.OutboundReviewError):
            self.service.finalize_known_outbound_request(self.org_id + 100, self.request_id, actor_user_id=self.actor_id, evidence_reference='wrong tenant')
        self.db.session.rollback()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        request.status = 'accepted'
        self.db.session.commit()
        with self.assertRaises(self.service.OutboundReviewError):
            self._finalize_known()
        self.db.session.rollback()
        self.assertEqual(request.status, 'accepted')
        self.assertEqual(self.models.OrganizationProviderAuditLog.query.count(), 0)

    def test_finalize_known_reply_validates_original_literal_body_and_conversation_key(self):
        from app.services.outbound_request_service import intent_digest

        attempt = self._known_attempt()
        thread = self.models.InboxThread(organization_id=self.org_id, phone=self.phone)
        self.db.session.add(thread)
        self.db.session.flush()
        request = self.db.session.get(self.models.OutboundRequest, self.request_id)
        request.kind, request.message_log_id, request.thread_id = 'reply', None, thread.id
        intent = {'thread_id': thread.id, 'phone': self.phone, 'message_body': self.body}
        request.payload_json = json.dumps({'intent': intent, 'recipients': [{'phone': self.phone}], 'message_body': self.body})
        request.payload_digest = intent_digest('reply', intent)
        attempt.logical_send_key, attempt.message_log_id, attempt.send_kind = f'reply-request:{request.id}', None, 'manual_reply'
        self.db.session.commit()
        self.assertEqual(self._finalize_known()['known_attempts'], 1)
        self.assertEqual(self.models.InboxMessage.query.count(), 0)

    def test_cli_finalize_known_is_explicit_targeted_and_excludes_sid_arguments(self):
        from app.outbound_review import main

        self._known_attempt()
        base = ['--organization-id', str(self.org_id), '--request-id', str(self.request_id), '--finalize-known']
        for extra in ([], ['--actor-user-id', str(self.actor_id)], ['--resolve'], [
            '--actor-user-id', str(self.actor_id), '--evidence-reference', 'local-proof', '--message-sid', self.sid,
        ]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                main(base + extra)
            self.assertEqual(error.exception.code, 2)
        output = io.StringIO()
        with patch('app.outbound_review.create_app', return_value=self.app), redirect_stdout(output):
            result = main(base + ['--actor-user-id', str(self.actor_id), '--evidence-reference', 'local-proof'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['known_attempts'], 1)

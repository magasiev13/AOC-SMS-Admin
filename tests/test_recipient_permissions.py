import importlib
import json
import logging
import os
import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestRecipientPermissions(unittest.TestCase):
    def create_postgres_database(self, label):
        import psycopg
        from psycopg import sql

        root = os.environ.get('AUDIT_PERMISSION_DATABASE_ROOT')
        if root != 'postgresql://audit@127.0.0.1:55432/postgres':
            raise RuntimeError('Permission PostgreSQL tests accept only the isolated local audit cluster.')
        name = f'permission_{label}_{uuid.uuid4().hex}'
        with psycopg.connect(root, autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0 ENCODING UTF8 LC_COLLATE {} LC_CTYPE {}').format(
                sql.Identifier(name), sql.Literal('C'), sql.Literal('C'),
            ))
        self.postgres_databases.append(name)
        return f'postgresql+psycopg://audit@127.0.0.1:55432/{name}'

    def setUp(self):
        self.original_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix='twinevia-permission-')
        self.postgres_databases = []
        database_url = (
            self.create_postgres_database('case') if os.environ.get('AUDIT_PERMISSION_DATABASE_ROOT')
            else 'sqlite:///' + self.tmp.name + '/permissions.db'
        )
        os.environ.update(
            DATABASE_URL=database_url,
            SAAS_MODE='1', FLASK_DEBUG='1', SCHEDULER_ENABLED='0',
            SECRET_KEY='permission-test-secret', PYTHON_DOTENV_DISABLED='1',
        )
        import app.config
        importlib.reload(app.config)
        from app import create_app, db
        from app import models
        from app.services import recipient_permission_service
        self.models, self.db, self.permissions = models, db, recipient_permission_service
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.assertEqual(self.app.config['SQLALCHEMY_DATABASE_URI'], database_url)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, TWILIO_BROWSER_FAKE_SENDS=False)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.org = models.Organization(name='Permission org', slug='permission-org', status='active')
        self.other = models.Organization(name='Other org', slug='permission-other', status='active')
        db.session.add_all([self.org, self.other])
        db.session.flush()
        db.session.add_all([
            models.OrganizationSubscription(organization=self.org, status='complimentary'),
            models.OrganizationMessagingProfile(
                organization=self.org, provider_mode='platform_managed',
                provider_status='active', status='active', sender_review_status='approved',
                from_number='+15550009999', messaging_service_sid='MGpermission',
                phone_number_sid='PNpermission', consent_acknowledged_at=models.utc_now(),
            ),
        ])
        db.session.commit()
        self.phone = '+17205550145'
        self.now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        self.counter = 0

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()
        if self.postgres_databases:
            import psycopg
            from psycopg import sql
            with psycopg.connect('postgresql://audit@127.0.0.1:55432/postgres', autocommit=True) as connection:
                for name in self.postgres_databases:
                    connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        os.environ.clear()
        os.environ.update(self.original_env)

    def record(self, *, decision='granted', purpose='community', event_id=None,
               source='owner_verified', at=0, organization_id=None, phone=None,
               evidence_key=None, verified=None, metadata=None):
        self.counter += 1
        return self.permissions.record_permission_evidence(
            organization_id or self.org.id, phone or self.phone,
            purpose=purpose, event_id=event_id, decision=decision,
            source=source, evidence_key=evidence_key or f'proof-{self.counter}',
            occurred_at=self.now + timedelta(seconds=at),
            verified=(decision == 'granted') if verified is None else verified,
            metadata=metadata if metadata is not None else {'proof': 'Retained opt-in source and date.'},
        )

    def status(self, **kwargs):
        return self.permissions.recipient_permission_status(self.org.id, self.phone, **kwargs)

    def provider(self):
        from app.services.twilio_service import TwilioService
        provider = TwilioService.__new__(TwilioService)
        provider.organization_id = self.org.id
        provider.account_sid = 'ACpermission'
        provider.messaging_service_sid = 'MGpermission'
        provider.from_number = '+15550009999'
        provider.client = MagicMock()
        provider.client.messages.create.return_value = SimpleNamespace(
            sid='SMpermission', status='queued', account_sid='ACpermission',
            num_segments='1', price=None, price_unit='usd',
        )
        return provider

    def test_unknown_and_explicit_denial_are_distinct_and_fail_closed(self):
        self.assertEqual((self.status().decision, self.status().reason), ('unknown', 'permission_unknown'))
        self.record(decision='unknown')
        self.assertFalse(self.status().allowed)
        self.record(decision='denied', at=1)
        self.assertEqual((self.status().decision, self.status().reason), ('denied', 'permission_denied'))

    def test_grant_requires_verified_proof(self):
        for verified, metadata in [(False, {'proof': 'claimed'}), (True, {}), (True, {'proof': ' '}), (True, {'proof': {'untrusted': 'value'}})]:
            with self.subTest(verified=verified, metadata=metadata), self.assertRaises(ValueError):
                self.record(verified=verified, metadata=metadata)
        self.record()
        self.assertTrue(self.status().allowed)

    def test_event_permission_does_not_grant_general_or_other_event(self):
        self.record(purpose='event', event_id=123)
        self.assertTrue(self.status(purpose='event', event_id=123).allowed)
        self.assertFalse(self.status(purpose='event', event_id=124).allowed)
        self.assertFalse(self.status().allowed)

    def test_later_denial_requires_later_independently_verified_grant(self):
        self.record(at=0)
        self.record(decision='denied', source='event_sync:booking', at=2)
        self.assertFalse(self.status().allowed)
        self.record(source='another_verified_source', at=1)
        self.assertFalse(self.status().allowed)
        self.record(source='another_verified_source', at=3)
        self.assertTrue(self.status().allowed)

    def test_unknown_revision_does_not_revive_grant_older_than_denial(self):
        self.record(source='owner_verified', at=0)
        self.record(source='event_sync:booking', decision='denied', at=1)
        self.record(source='event_sync:booking', decision='unknown', at=2)
        self.assertEqual(self.status().reason, 'permission_denied')

    def test_same_timestamp_grant_does_not_override_ambiguous_ordered_denial(self):
        self.record(decision='denied', source='event_sync:booking', at=1)
        self.record(source='owner_verified', at=1)
        self.assertFalse(self.status().allowed)
        self.record(source='owner_verified', at=2)
        self.assertTrue(self.status().allowed)

    def test_stale_source_record_cannot_overwrite_newer_decision(self):
        self.record(decision='granted', at=3)
        self.record(decision='denied', at=1)
        self.assertTrue(self.status().allowed)
        self.record(decision='unknown', at=4)
        self.assertFalse(self.status().allowed)

    def test_stop_dominates_and_start_does_not_recreate_old_grant(self):
        self.record(at=0)
        self.record(purpose='all', decision='denied', source='inbound_stop', at=2)
        self.record(at=3)
        self.assertEqual(self.status().reason, 'permission_revoked')
        self.record(purpose='all', decision='unknown', source='inbound_start', at=4)
        # A grant recorded after revocation is still explicit purpose evidence.
        self.assertTrue(self.status().allowed)

    def test_start_only_restores_transport_not_pre_stop_purpose_grant(self):
        self.record(at=0)
        self.record(purpose='all', decision='denied', source='inbound_stop', at=2)
        self.record(purpose='all', decision='unknown', source='inbound_start', at=4)
        self.assertEqual(self.status().reason, 'permission_revoked')
        self.record(at=5)
        self.assertTrue(self.status().allowed)

    def test_identity_replay_is_idempotent_and_conflicts_are_rejected(self):
        first = self.record(evidence_key='one-action')
        same = self.record(evidence_key='one-action')
        self.assertEqual(first.id, same.id)
        with self.assertRaises(ValueError):
            self.record(evidence_key='one-action', decision='denied')
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)

    def test_tenant_isolation_in_scoped_and_platform_contexts(self):
        from app.tenant import organization_context, without_tenant_scope
        self.record(organization_id=self.other.id)
        self.assertFalse(self.status().allowed)
        with organization_context(self.org.id):
            self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)
            with self.assertRaises(ValueError):
                self.permissions.recipient_permission_status(self.other.id, self.phone)
        # A platform boundary intentionally disables ORM criteria; service still
        # binds each query to its explicit target organization.
        with without_tenant_scope():
            self.assertFalse(self.status().allowed)
            self.assertTrue(self.permissions.recipient_permission_status(self.other.id, self.phone).allowed)

    def test_batch_filter_exposes_reason_without_deleting_directory_records(self):
        from app.tenant import organization_context
        with organization_context(self.org.id):
            self.db.session.add(self.models.CommunityMember(name='Retained', phone=self.phone))
            self.db.session.flush()
        self.record(decision='denied')
        allowed, skipped, phones = self.permissions.filter_permission_recipients(
            [{'phone': self.phone, 'name': 'Retained'}], organization_id=self.org.id,
        )
        self.assertEqual(allowed, [])
        self.assertEqual(skipped[0]['permission_reason'], 'permission_denied')
        self.assertEqual(phones, {self.phone})
        self.assertEqual(self.models.CommunityMember.query.count(), 1)

    def test_final_provider_boundary_rechecks_withdrawal_after_snapshot(self):
        self.record(at=0)
        recipients = [{'phone': self.phone, 'name': 'Fixture'}]
        self.assertEqual(len(self.permissions.filter_permission_recipients(recipients, organization_id=self.org.id)[0]), 1)
        self.record(decision='denied', at=1)
        provider = self.provider()
        result = provider.send_message(self.phone, 'Frozen snapshot must not bypass withdrawal', send_kind='blast')
        self.assertEqual(result['reason'], 'permission_denied')
        provider.client.messages.create.assert_not_called()

    def test_provider_default_community_gate_and_explicit_event_context(self):
        self.record(purpose='event', event_id=123)
        provider = self.provider()
        self.assertEqual(provider.send_message(self.phone, 'General', send_kind='blast')['reason'], 'permission_unknown')
        with self.permissions.permission_context('event', 123):
            self.assertTrue(provider.send_message(self.phone, 'Event update', send_kind='blast')['success'])
        self.assertEqual(provider.send_message(self.phone, 'General again', send_kind='blast')['reason'], 'permission_unknown')
        provider.client.messages.create.assert_called_once()

    def test_reply_does_not_require_broadcast_grant_but_stop_still_blocks_it(self):
        provider = self.provider()
        self.assertTrue(provider.send_message(self.phone, 'Conversational response', send_kind='manual_reply')['success'])
        self.record(purpose='all', decision='denied', source='inbound_stop')
        self.assertEqual(provider.send_message(self.phone, 'Blocked', send_kind='manual_reply')['reason'], 'permission_revoked')
        self.assertEqual(provider.client.messages.create.call_count, 1)

    def test_verified_rate_limit_retries_same_attempt_then_accepts(self):
        from twilio.base.exceptions import TwilioRestException
        from app.services.twilio_service import TwilioTransientError

        self.record()
        provider = self.provider()
        provider.client.messages.create.side_effect = [
            TwilioRestException(429, '/Messages', msg='Too many requests', code=20429),
            SimpleNamespace(sid='SMrateaccepted', status='queued', account_sid='ACpermission'),
        ]
        with self.assertRaises(TwilioTransientError) as caught:
            provider.send_message_with_attempt(self.phone, 'Rate-limited update', 'rate-retry', None, None, 'blast')
        self.assertEqual(caught.exception.__cause__.status, 429)
        attempt = self.models.MessageDispatchAttempt.query.one()
        self.assertEqual((attempt.status, attempt.attempt_count, attempt.provider_message_sid), ('queued', 1, None))
        result = provider.send_message_with_attempt(self.phone, 'Rate-limited update', 'rate-retry', None, None, 'blast')
        replay = provider.send_message_with_attempt(self.phone, 'Rate-limited update', 'rate-retry', None, None, 'blast')
        self.assertTrue(result['success'])
        self.assertEqual(replay['sid'], 'SMrateaccepted')
        self.assertEqual(provider.client.messages.create.call_count, 2)
        self.assertEqual(self.models.MessageDispatchAttempt.query.count(), 1)
        self.assertEqual((attempt.status, attempt.attempt_count), ('sent', 2))

    def test_verified_rate_limit_exhausts_four_calls_without_fifth_submission(self):
        from twilio.base.exceptions import TwilioRestException
        from app.services.twilio_service import TwilioTransientError

        self.record()
        provider = self.provider()
        provider.client.messages.create.side_effect = TwilioRestException(429, '/Messages', msg='Too many requests', code=20429)
        for _ in range(3):
            with self.assertRaises(TwilioTransientError):
                provider.send_message_with_attempt(self.phone, 'Bounded update', 'rate-exhaustion', None, None, 'blast')
        result = provider.send_message_with_attempt(self.phone, 'Bounded update', 'rate-exhaustion', None, None, 'blast')
        replay = provider.send_message_with_attempt(self.phone, 'Bounded update', 'rate-exhaustion', None, None, 'blast')
        self.assertEqual((result['status'], result['reason'], result['sid']), ('failed', 'rejected_rate_limit', None))
        self.assertEqual(replay['status'], 'failed')
        self.assertIn('rejected_rate_limit', replay['error'])
        self.assertEqual(provider.client.messages.create.call_count, 4)
        attempt = self.models.MessageDispatchAttempt.query.one()
        self.assertEqual((attempt.status, attempt.attempt_count, attempt.provider_message_sid), ('failed', 4, None))

    def test_unknown_provider_failures_remain_ambiguous_while_definite_rejection_is_failed(self):
        from twilio.base.exceptions import TwilioRestException

        self.record()
        for label, error, expected in [
            ('bad-request', TwilioRestException(400, '/Messages', msg='Invalid request', code=21211), 'failed'),
            ('server-error', TwilioRestException(500, '/Messages', msg='Server error'), 'ambiguous'),
            ('unavailable', TwilioRestException(503, '/Messages', msg='Unavailable'), 'ambiguous'),
            ('timeout', TimeoutError('Provider may have accepted before timeout'), 'ambiguous'),
        ]:
            with self.subTest(error=label):
                provider = self.provider()
                provider.client.messages.create.side_effect = error
                result = provider.send_message_with_attempt(self.phone, 'Uncertain update', label, None, None, 'blast')
                replay = provider.send_message_with_attempt(self.phone, 'Uncertain update', label, None, None, 'blast')
                self.assertEqual(result['status'], expected)
                self.assertEqual(replay['status'], expected)
                self.assertEqual(provider.client.messages.create.call_count, 1)

    def test_bulk_rate_limit_retains_partial_results_and_reuses_prior_acceptance(self):
        from twilio.base.exceptions import TwilioRestException
        from app.services.twilio_service import TwilioTransientError

        self.record()
        second_phone = '+17205550148'
        self.record(phone=second_phone)
        provider = self.provider()
        provider.client.messages.create.side_effect = [
            SimpleNamespace(sid='SMratefirst', status='queued', account_sid='ACpermission'),
            TwilioRestException(429, '/Messages', msg='Too many requests', code=20429),
            SimpleNamespace(sid='SMratesecond', status='queued', account_sid='ACpermission'),
        ]
        recipients = [{'phone': self.phone, 'name': 'First'}, {'phone': second_phone, 'name': 'Second'}]
        with self.assertRaises(TwilioTransientError) as caught:
            provider.send_bulk_with_attempts(recipients, 'Notice {name}', 'rate-bulk', None, None, 0, 'blast')
        partial = caught.exception.results
        self.assertEqual(partial['success_count'], 1)
        self.assertEqual([row['sid'] for row in partial['details']], ['SMratefirst'])
        result = provider.send_bulk_with_attempts(recipients, 'Notice {name}', 'rate-bulk', None, None, 0, 'blast')
        self.assertEqual((result['success_count'], result['failure_count']), (2, 0))
        self.assertEqual([row['sid'] for row in result['details']], ['SMratefirst', 'SMratesecond'])
        self.assertEqual(provider.client.messages.create.call_count, 3)
        attempts = self.models.MessageDispatchAttempt.query.order_by(self.models.MessageDispatchAttempt.id).all()
        self.assertEqual([(row.status, row.attempt_count) for row in attempts], [('sent', 1), ('sent', 2)])

    def test_legacy_mode_retains_compatibility_without_permission_table_query(self):
        self.app.config['SAAS_MODE'] = False
        self.assertTrue(self.status().allowed)

    def review_fixture(self, *, role='owner', platform=False):
        from app.tenant import organization_context

        suffix = self.models.AppUser.query.count()
        user = self.models.AppUser(
            username=f'review-user-{suffix}', phone=f'+15550001{suffix:03d}',
            role='admin', must_change_password=False, is_platform_admin=platform,
        )
        user.set_password('Permission-review-pass1!')
        self.db.session.add(user)
        self.db.session.flush()
        if not platform:
            self.db.session.add(self.models.OrganizationMembership(
                organization_id=self.org.id, user_id=user.id, role=role,
            ))
        with organization_context(self.org.id):
            member = self.models.CommunityMember(name='Review contact', phone=self.phone)
            event = self.models.Event(title='Permission review event')
            self.db.session.add_all([member, event])
            self.db.session.flush()
            registration = self.models.EventRegistration(event_id=event.id, phone=self.phone, name='Review attendee')
            self.db.session.add(registration)
            self.db.session.flush()
        self.db.session.commit()
        self.client = self.app.test_client()
        with patch('app.auth.send_security_alert'):
            response = self.client.post('/platform/login' if platform else '/login', data={
                'username': user.username, 'password': 'Permission-review-pass1!',
            })
        self.assertEqual(response.status_code, 302)
        return user, member, event, registration

    def review_form(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        token = re.search(r'name="review_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
        return {
            'review_token': token, 'decision': 'granted', 'source_label': 'Retained opt-in form',
            'proof': 'Contact explicitly requested these SMS updates; original form is retained.',
            'occurred_at': self.now.strftime('%Y-%m-%dT%H:%M:%S'), 'verified': '1',
        }

    def test_owner_review_requires_source_proof_attestation_and_valid_time(self):
        user, member, _event, _registration = self.review_fixture()
        path = f'/community/{member.id}/permission'
        data = self.review_form(path)
        invalid_fields = [
            {'source_label': ''}, {'proof': ''}, {'verified': ''},
            {'occurred_at': ''}, {'occurred_at': '2099-01-01T00:00'},
            {'decision': 'all'},
        ]
        for change in invalid_fields:
            with self.subTest(change=change):
                response = self.client.post(path, data={**data, **change})
                self.assertEqual(response.status_code, 422)
                self.assertIn(b'The review was not saved.', response.data)
                if 'proof' not in change:
                    self.assertIn(data['proof'].encode(), response.data)
                self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertTrue(self.status().allowed)
        evidence = self.models.RecipientPermissionEvidence.query.one()
        metadata = json.loads(evidence.metadata_json)
        self.assertTrue(evidence.verified)
        self.assertEqual(metadata['actor_user_id'], user.id)
        self.assertEqual(metadata['source_label'], data['source_label'])
        self.assertTrue(metadata['verified_attestation'])
        self.assertEqual(self.models.CommunityMember.query.count(), 1)
        self.assertIn(b'Current permission status: granted', self.client.get(path).data)

    def test_owner_review_replay_is_idempotent_and_changed_evidence_rejected(self):
        _user, member, _event, _registration = self.review_fixture()
        path = f'/community/{member.id}/permission'
        data = self.review_form(path)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)
        self.assertEqual(self.client.post(path, data={**data, 'proof': 'Different proof'}).status_code, 422)
        self.assertEqual(self.client.post(path, data={**data, 'decision': 'denied'}).status_code, 422)
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 1)
        self.assertTrue(self.status().allowed)

    def test_review_success_uses_canonical_local_destination_ignoring_external_next(self):
        _user, member, event, registration = self.review_fixture()
        for path in [f'/community/{member.id}/permission', f'/events/{event.id}/registrations/{registration.id}/permission']:
            with self.subTest(path=path):
                data = self.review_form(path)
                response = self.client.post(path + '?next=https://external.invalid/recovery', data=data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers['Location'], path)

    def test_review_uses_evidence_time_instead_of_review_time_to_override_denial(self):
        _user, member, _event, _registration = self.review_fixture()
        self.record(decision='denied', source='booking_refusal', at=2)
        self.db.session.commit()
        path = f'/community/{member.id}/permission'
        data = self.review_form(path)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.status().reason, 'permission_denied')
        later = self.review_form(path)
        later['occurred_at'] = (self.now + timedelta(seconds=3)).strftime('%Y-%m-%dT%H:%M:%S')
        self.assertEqual(self.client.post(path, data=later).status_code, 302)
        self.assertTrue(self.status().allowed)

    def test_event_review_only_grants_that_event_and_registration_must_match(self):
        _user, _member, event, registration = self.review_fixture()
        path = f'/events/{event.id}/registrations/{registration.id}/permission'
        data = self.review_form(path)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertTrue(self.status(purpose='event', event_id=event.id).allowed)
        self.assertFalse(self.status().allowed)
        from app.tenant import organization_context
        with organization_context(self.org.id):
            other_event = self.models.Event(title='Other event')
            self.db.session.add(other_event)
            self.db.session.commit()
        self.assertEqual(self.client.get(f'/events/{other_event.id}/registrations/{registration.id}/permission').status_code, 404)
        self.assertEqual(self.client.post(f'/events/{other_event.id}/registrations/{registration.id}/permission', data=data).status_code, 404)

    def test_staff_and_platform_cannot_review_workspace_permission(self):
        _user, member, event, registration = self.review_fixture(role='staff')
        paths = [f'/community/{member.id}/permission', f'/events/{event.id}/registrations/{registration.id}/permission']
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post(path, data={}).status_code, 403)
        # A platform session remains on its intentional platform boundary.
        platform = self.models.AppUser(username='review-platform', phone='+15550002999', role='admin', is_platform_admin=True, must_change_password=False)
        platform.set_password('Permission-review-pass1!')
        self.db.session.add(platform)
        self.db.session.commit()
        self.client = self.app.test_client()
        with patch('app.auth.send_security_alert'):
            self.client.post('/platform/login', data={'username': platform.username, 'password': 'Permission-review-pass1!'})
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 302)
            self.assertEqual(self.client.post(path, data={}).status_code, 403)
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def test_review_routes_hide_other_tenant_contacts_and_registrations(self):
        self.review_fixture()
        from app.tenant import organization_context
        with organization_context(self.other.id):
            member = self.models.CommunityMember(name='Private other tenant', phone=self.phone)
            event = self.models.Event(title='Private other event')
            self.db.session.add_all([member, event])
            self.db.session.flush()
            registration = self.models.EventRegistration(event_id=event.id, phone=self.phone)
            self.db.session.add(registration)
            self.db.session.commit()
        for path in [f'/community/{member.id}/permission', f'/events/{event.id}/registrations/{registration.id}/permission']:
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path, data={}).status_code, 404)
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def test_review_token_binds_phone_and_rejects_forgery(self):
        _user, member, _event, _registration = self.review_fixture()
        path = f'/community/{member.id}/permission'
        data = self.review_form(path)
        self.assertEqual(self.client.post(path, data={**data, 'review_token': 'a' * 32 + '.' + 'b' * 64}).status_code, 409)
        member.phone = '+17205550999'
        self.db.session.commit()
        self.assertEqual(self.client.post(path, data=data).status_code, 409)
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)

    def test_review_post_requires_valid_csrf_token(self):
        _user, member, _event, _registration = self.review_fixture()
        path = f'/community/{member.id}/permission'
        self.app.config['WTF_CSRF_ENABLED'] = True
        data = self.review_form(path)
        self.assertEqual(self.client.post(path, data=data).status_code, 400)
        self.assertEqual(self.client.post(path, data={**data, 'csrf_token': 'forged'}).status_code, 400)
        html = self.client.get(path).get_data(as_text=True)
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        self.assertEqual(self.client.post(path, data={**data, 'csrf_token': csrf_token}).status_code, 302)
        self.assertTrue(self.status().allowed)

    def test_directory_saves_do_not_grant_and_rendered_states_match_export(self):
        _user, member, event, _registration = self.review_fixture()
        self.assertIn(b'Unknown', self.client.get('/community').data)
        self.assertIn(b'Review permission', self.client.get('/community').data)
        self.assertEqual(self.client.post(f'/community/{member.id}/edit', data={'name': 'Renamed', 'phone': self.phone}).status_code, 302)
        self.assertEqual(self.models.RecipientPermissionEvidence.query.count(), 0)
        detail = self.client.get(f'/events/{event.id}').get_data(as_text=True)
        self.assertIn('Unknown', detail)
        self.assertIn('Not eligible', detail)
        export = self.client.get(f'/events/{event.id}/export').get_data(as_text=True)
        self.assertIn('blocked', export)
        self.assertNotIn('sendable', export)
        self.record(purpose='event', event_id=event.id)
        self.db.session.commit()
        self.assertIn('sendable', self.client.get(f'/events/{event.id}/export').get_data(as_text=True))

    def test_owner_review_does_not_clear_stop_or_transport_suppression(self):
        _user, member, _event, _registration = self.review_fixture()
        self.record(purpose='all', decision='denied', source='inbound_stop', at=-1)
        self.db.session.add(self.models.UnsubscribedContact(organization_id=self.org.id, phone=self.phone))
        self.db.session.commit()
        path = f'/community/{member.id}/permission'
        data = self.review_form(path)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.status().reason, 'permission_revoked')
        response = self.client.get(path)
        self.assertIn(b'Unsubscribed', response.data)
        self.assertIn(b'does not remove an opt-out', response.data)
        self.assertEqual(self.models.UnsubscribedContact.query.count(), 1)

    def test_additive_migration_preserves_unreviewed_contacts_without_grant(self):
        from sqlalchemy import create_engine, inspect, text
        from app.saas_migrations.runner import run_pending_saas_migrations
        engine = create_engine(
            self.create_postgres_database('migration') if os.environ.get('AUDIT_PERMISSION_DATABASE_ROOT')
            else 'sqlite:///' + self.tmp.name + '/migration.db'
        )
        logger = logging.getLogger('permission-migration')
        try:
            run_pending_saas_migrations(engine, logger, target_version='019')
            with engine.begin() as connection:
                connection.execute(self.models.Organization.__table__.insert().values(id=1, name='Existing', slug='existing', status='active'))
                connection.execute(self.models.CommunityMember.__table__.insert().values(organization_id=1, name='Retained', phone='+17205550145'))
            self.assertIn('020', run_pending_saas_migrations(engine, logger, target_version='020'))
            self.assertEqual(run_pending_saas_migrations(engine, logger, target_version='020'), [])
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SELECT count(*) FROM community_members')).scalar(), 1)
                self.assertEqual(connection.execute(text('SELECT count(*) FROM recipient_permission_evidence')).scalar(), 0)
                self.assertIn('recipient_permission_evidence', inspect(connection).get_table_names())
        finally:
            engine.dispose()

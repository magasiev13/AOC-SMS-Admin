import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, tuple[str, int | None]] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._values:
            return False
        normalized = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        self._values[key] = (normalized, ex)
        return True

    def get(self, key):
        entry = self._values.get(key)
        if entry is None:
            return None
        return entry[0]

    def ttl(self, key):
        entry = self._values.get(key)
        if entry is None:
            return -2
        return entry[1] if entry[1] is not None else -1

    def delete(self, key):
        self._values.pop(key, None)
        return 1


class TestLogsRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        self._original_saas_mode = os.environ.get("SAAS_MODE")
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["SAAS_MODE"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import (
            AppUser,
            CommunityMember,
            MessageLog,
            Organization,
            OrganizationMembership,
            OrganizationMessagingProfile,
            OrganizationSubscription,
            OrganizationTestRecipient,
            ScheduledMessage,
            SuppressedContact,
        )

        self.db = db
        self.AppUser = AppUser
        self.CommunityMember = CommunityMember
        self.MessageLog = MessageLog
        self.Organization = Organization
        self.OrganizationMembership = OrganizationMembership
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationSubscription = OrganizationSubscription
        self.OrganizationTestRecipient = OrganizationTestRecipient
        self.ScheduledMessage = ScheduledMessage
        self.SuppressedContact = SuppressedContact

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        organization = self.Organization(name="Acme", slug="acme", status="active")
        subscription = self.OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status="complimentary",
        )
        messaging_profile = self.OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACsub_acme",
            messaging_service_sid="MGacme0001",
            phone_number_sid="PNacme0001",
            from_number="+15550009999",
            inbound_identity="+15550009999",
            status="active",
            provider_status="active",
            sender_review_status="approved",
        )
        admin = self.AppUser(
            username="admin",
            email="admin@acme.test",
            phone="+15550000022",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("admin-pass")
        self.db.session.add_all([organization, subscription, messaging_profile, admin])
        self.db.session.flush()
        self.organization = organization
        self.admin = admin
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=organization.id,
                user_id=admin.id,
                role="owner",
            )
        )
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._app_context.pop()
        self._temp_dir.cleanup()
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug
        if self._original_saas_mode is None:
            os.environ.pop("SAAS_MODE", None)
        else:
            os.environ["SAAS_MODE"] = self._original_saas_mode
        os.environ.pop("DATABASE_URL", None)

    def _login(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def _create_log(self, details_payload: str) -> int:
        log = self.MessageLog(
            organization_id=self.organization.id,
            message_body="Test log body",
            target="community",
            status="failed",
            total_recipients=1,
            success_count=0,
            failure_count=1,
            details=details_payload,
        )
        self.db.session.add(log)
        self.db.session.commit()
        return log.id

    def test_log_detail_supports_legacy_object_payload(self) -> None:
        self._login()
        log_id = self._create_log(
            json.dumps(
                {
                    "details": [
                        {
                            "phone": "+15551234567",
                            "success": False,
                            "error": "Carrier rejection",
                        }
                    ]
                }
            )
        )

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("+15551234567", html)
        self.assertIn("Carrier rejection", html)


    def test_log_detail_handles_numeric_phone_values(self) -> None:
        self._login()
        log_id = self._create_log(
            json.dumps(
                [
                    {
                        "phone": 15557654321,
                        "success": False,
                        "error": "Carrier rejection",
                    }
                ]
            )
        )

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Carrier rejection", html)

    def test_log_detail_ignores_non_dict_detail_entries(self) -> None:
        self._login()
        log_id = self._create_log(
            json.dumps(
                [
                    "bad",
                    {
                        "phone": "+15557654321",
                        "success": False,
                        "error": "Temporary failure",
                    },
                    42,
                ]
            )
        )

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("+15557654321", html)
        self.assertIn("Temporary failure", html)

    def test_log_detail_renders_when_details_json_array_is_empty(self) -> None:
        self._login()
        log_id = self._create_log("[]")

        response = self.client.get(f"/logs/{log_id}", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Message Log Details", html)

    def test_log_detail_does_not_warn_for_empty_details_array(self) -> None:
        self._login()
        log_id = self._create_log("[]")

        with patch.object(self.app.logger, "warning") as warning_mock:
            response = self.client.get(f"/logs/{log_id}", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        warning_mock.assert_not_called()

    def test_log_detail_warns_for_unusable_details_payload(self) -> None:
        self._login()
        log_id = self._create_log(json.dumps({"unexpected": True}))

        with patch.object(self.app.logger, "warning") as warning_mock:
            response = self.client.get(f"/logs/{log_id}", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        warning_mock.assert_called_once()
        self.assertIn("MessageLog details payload unusable", warning_mock.call_args[0][0])

    def test_dashboard_send_redirects_to_log_detail_for_general_blast(self) -> None:
        self._grant_broadcast_permission('+15551234567')
        self._login()
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Member",
                phone="+15551234567",
            )
        )
        self.db.session.commit()

        mock_queue = MagicMock()
        fake_redis = FakeRedis()
        with (
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.services.outbound_idempotency_service.get_redis_connection", return_value=fake_redis),
        ):
            response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Hello everyone",
                    "target": "community",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Message Log Details", response.get_data(as_text=True))
        mock_queue.enqueue.assert_called_once()
        self.assertEqual(mock_queue.enqueue.call_args.kwargs["job_timeout"], 1800)
        retry = mock_queue.enqueue.call_args.kwargs["retry"]
        self.assertEqual(retry.max, 3)
        self.assertEqual(retry.intervals, [30, 120, 300])

    def test_dashboard_duplicate_post_reuses_existing_log_and_does_not_enqueue_twice(self) -> None:
        self._grant_broadcast_permission('+15551234568')
        self._login()
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Member",
                phone="+15551234568",
            )
        )
        self.db.session.commit()

        mock_queue = MagicMock()
        fake_redis = FakeRedis()
        with (
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.services.outbound_idempotency_service.get_redis_connection", return_value=fake_redis),
        ):
            first_response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Hello everyone",
                    "target": "community",
                },
                follow_redirects=False,
            )
            second_response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Hello everyone",
                    "target": "community",
                },
                follow_redirects=False,
            )

        logs = self.MessageLog.query.order_by(self.MessageLog.id.asc()).all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(mock_queue.enqueue.call_count, 1)
        self.assertTrue(first_response.location.endswith(f"/logs/{logs[0].id}"))
        self.assertTrue(second_response.location.endswith(f"/logs/{logs[0].id}"))

    def test_dashboard_stale_cross_tenant_idempotency_log_reference_queues_new_log(self) -> None:
        self._grant_broadcast_permission('+15551234568')
        self._login()
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Member",
                phone="+15551234568",
            )
        )
        foreign_org = self.Organization(name="Foreign", slug="foreign", status="active")
        foreign_log = self.MessageLog(
            organization=foreign_org,
            message_body="Hello everyone",
            target="community",
            status="processing",
            total_recipients=1,
            details="[]",
        )
        self.db.session.add_all([foreign_org, foreign_log])
        self.db.session.commit()

        from app.services.outbound_idempotency_service import (
            BLAST_IDEMPOTENCY_TTL_SECONDS,
            bind_idempotency_log_id,
            claim_outbound_idempotency,
        )

        mock_queue = MagicMock()
        fake_redis = FakeRedis()
        with (
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.services.outbound_idempotency_service.get_redis_connection", return_value=fake_redis),
        ):
            claim = claim_outbound_idempotency(
                "dashboard-blast",
                {
                    "organization_id": self.organization.id,
                    "target": "community",
                    "event_id": None,
                    "test_mode": False,
                    "message_body": "Hello everyone",
                    "phones": ["+15551234568"],
                },
                ttl_seconds=BLAST_IDEMPOTENCY_TTL_SECONDS,
            )
            bind_idempotency_log_id(
                claim.redis_key,
                foreign_log.id,
                ttl_seconds=BLAST_IDEMPOTENCY_TTL_SECONDS,
            )
            response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Hello everyone",
                    "target": "community",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Message Log Details", response.get_data(as_text=True))
        self.assertEqual(mock_queue.enqueue.call_count, 1)
        current_org_logs = self.MessageLog.query.filter_by(organization_id=self.organization.id).all()
        self.assertEqual(len(current_org_logs), 1)

    def test_dashboard_send_redirects_to_log_detail_for_test_mode(self) -> None:
        self._login()
        self.db.session.add(
            self.OrganizationTestRecipient(
                organization_id=self.organization.id,
                phone="+15550009999",
                label="Board Test",
            )
        )
        self.db.session.commit()

        mock_queue = MagicMock()
        fake_redis = FakeRedis()
        with (
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.services.outbound_idempotency_service.get_redis_connection", return_value=fake_redis),
        ):
            response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "Test mode message",
                    "target": "community",
                    "test_mode": "on",
                    "test_recipient_selection_mode": "one",
                    "test_recipient_phone": "+15550009999",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Message Log Details", response.get_data(as_text=True))
        mock_queue.enqueue.assert_called_once()

    def test_dashboard_send_normalizes_message_body_before_persisting_and_queueing(self) -> None:
        self._grant_broadcast_permission('+15551234569')
        self._login()
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Member",
                phone="+15551234569",
            )
        )
        self.db.session.commit()

        mock_queue = MagicMock()
        fake_redis = FakeRedis()
        with (
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.services.outbound_idempotency_service.get_redis_connection", return_value=fake_redis),
        ):
            response = self.client.post(
                "/dashboard",
                data={
                    "message_body": "“Hello” — team…",
                    "target": "community",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        log = self.MessageLog.query.order_by(self.MessageLog.id.desc()).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.message_body, '"Hello" - team...')
        self.assertEqual(mock_queue.enqueue.call_args.args[4], '"Hello" - team...')
        self.assertIn("Estimated billing", response.get_data(as_text=True))

    def test_dashboard_schedule_stores_normalized_message_body(self) -> None:
        self._grant_broadcast_permission('+15551234570')
        from datetime import datetime, timedelta

        self._login()
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Member",
                phone="+15551234570",
            )
        )
        self.db.session.commit()

        scheduled_at = datetime.utcnow() + timedelta(days=1)
        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Board — update…",
                "target": "community",
                "schedule_later": "on",
                "schedule_date": scheduled_at.strftime("%Y-%m-%d"),
                "schedule_time": scheduled_at.strftime("%H:%M"),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        scheduled = self.ScheduledMessage.query.order_by(self.ScheduledMessage.id.desc()).first()
        self.assertIsNotNone(scheduled)
        self.assertEqual(scheduled.message_body, "Board - update...")

    def test_dashboard_rejects_empty_message_body(self) -> None:
        self._login()

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "   ",
                "target": "community",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Message body is required.", response.get_data(as_text=True))
        self.assertEqual(self.MessageLog.query.count(), 0)

    def test_dashboard_rejects_invalid_personalization_tokens(self) -> None:
        self._login()

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Hello {nickname}",
                "target": "community",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Invalid personalization token(s): {nickname}.", html)
        self.assertEqual(self.MessageLog.query.count(), 0)

    def test_dashboard_requires_event_id_for_event_target(self) -> None:
        self._login()

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Event reminder",
                "target": "event",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Please select an event.", response.get_data(as_text=True))
        self.assertEqual(self.MessageLog.query.count(), 0)

    def test_dashboard_rejects_past_scheduled_time(self) -> None:
        from datetime import datetime, timedelta

        self._login()
        scheduled_at = datetime.utcnow() - timedelta(minutes=5)

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Past blast",
                "target": "community",
                "schedule_later": "on",
                "schedule_date": scheduled_at.strftime("%Y-%m-%d"),
                "schedule_time": scheduled_at.strftime("%H:%M"),
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Scheduled time must be in the future.", response.get_data(as_text=True))
        self.assertEqual(self.ScheduledMessage.query.count(), 0)

    def test_dashboard_rejects_when_all_recipients_are_filtered_out(self) -> None:
        self._login()
        phone = "+15551234571"
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Suppressed Member",
                phone=phone,
            )
        )
        self.db.session.add(
            self.SuppressedContact(
                organization_id=self.organization.id,
                phone=phone,
                reason="Unknown subscriber",
                category="hard_fail",
                source="usage_reconciliation",
            )
        )
        self.db.session.commit()

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Hello everyone",
                "target": "community",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("No eligible recipients were found. Review permission, unsubscribe, and suppression records.", response.get_data(as_text=True))
        self.assertEqual(self.MessageLog.query.count(), 0)

    def test_dashboard_test_mode_requires_saved_test_recipient(self) -> None:
        self._login()

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Internal test blast",
                "target": "community",
                "test_mode": "on",
                "test_recipient_selection_mode": "one",
                "test_recipient_phone": "+15550009999",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Add at least one internal test recipient before using test mode.",
            response.get_data(as_text=True),
        )
        self.assertEqual(self.MessageLog.query.count(), 0)


    def _grant_broadcast_permission(self, phone):
        from app.services.recipient_permission_service import record_permission_evidence
        from app.models import utc_now
        record_permission_evidence(
            self.organization.id, phone, purpose='community', decision='granted',
            source='test_verified_opt_in', evidence_key='fixture-community-' + phone,
            occurred_at=utc_now(), metadata={'proof': 'Explicit synthetic positive permission fixture'}, verified=True,
        )
        self.db.session.commit()

    def test_audience_preview_and_saved_send_use_the_same_permission_boundary(self):
        self._login()
        self.db.session.add_all([
            self.CommunityMember(organization_id=self.organization.id, name='Permitted', phone='+17205550141'),
            self.CommunityMember(organization_id=self.organization.id, name='Unverified', phone='+17205550142'),
        ])
        self.db.session.commit()
        self._grant_broadcast_permission('+17205550141')
        preview = self.client.get('/dashboard/audience-preview?target=community')
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json['eligible'], 1)
        self.assertEqual(preview.json['excluded'], 1)
        queue = MagicMock()
        with patch('app.queue.get_queue', return_value=queue):
            response = self.client.post('/dashboard', data={
                'message_body': 'Permission boundary test', 'target': 'community', 'request_idempotency_key': 'preview-test',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(queue.enqueue.call_args.args[3], [{'name': 'Permitted', 'phone': '+17205550141'}])

    def test_audience_preview_rejects_foreign_event_and_invalid_target(self):
        from app.models import Event
        foreign = self.Organization(name='Foreign preview', slug='foreign-preview')
        event = Event(organization=foreign, title='Private event')
        self.db.session.add_all([foreign, event])
        self.db.session.commit()
        event_id = event.id
        self._login()
        self.assertEqual(self.client.get(f'/dashboard/audience-preview?target=event&event_id={event_id}').status_code, 404)
        self.assertEqual(self.client.get('/dashboard/audience-preview?target=unrecognized').status_code, 400)

    def test_clear_logs_preserves_saved_intent_and_dispatch_identity(self):
        from app.models import OutboundRequest
        self._login()
        self._grant_broadcast_permission('+17205550141')
        self.db.session.add(self.CommunityMember(organization_id=self.organization.id, phone='+17205550141'))
        self.db.session.commit()
        with patch('app.queue.get_queue', return_value=MagicMock()):
            self.client.post('/dashboard', data={'message_body': 'Keep evidence', 'target': 'community', 'request_idempotency_key': 'keep-log'})
        saved = OutboundRequest.query.one()
        log = self.db.session.get(self.MessageLog, saved.message_log_id)
        log.status = 'sent'
        self.db.session.commit()
        response = self.client.post('/logs/clear', data={'admin_password': 'admin-pass'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'logs with retained send evidence', response.data)
        self.assertIsNotNone(self.db.session.get(self.MessageLog, saved.message_log_id))
        self.assertEqual(OutboundRequest.query.count(), 1)

    def test_clear_logs_keeps_schedule_log_before_first_dispatch_attempt(self):
        from app.models import utc_now
        self._login()
        log_id = self._create_log('[]')
        self.db.session.add(self.ScheduledMessage(
            organization_id=self.organization.id, message_body='Scheduled history', target='community',
            scheduled_at=utc_now().replace(tzinfo=None), status='cancelled', message_log_id=log_id,
        ))
        self.db.session.commit()
        response = self.client.post('/logs/clear', data={'admin_password': 'admin-pass'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'logs with retained send evidence', response.data)
        self.assertIsNotNone(self.db.session.get(self.MessageLog, log_id))

    def test_clear_logs_preserves_suppression_source_evidence(self):
        self._login()
        log_id = self._create_log('[]')
        self.db.session.add(self.SuppressedContact(
            organization_id=self.organization.id, phone='+17205550143',
            source_message_log_id=log_id, reason='Known provider rejection', category='hard_failure',
        ))
        self.db.session.commit()
        response = self.client.post('/logs/clear', data={'admin_password': 'admin-pass'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.db.session.get(self.MessageLog, log_id))


if __name__ == "__main__":
    unittest.main()

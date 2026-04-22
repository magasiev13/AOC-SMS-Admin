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
        self.assertIn("All recipients are unsubscribed or no recipients were found.", response.get_data(as_text=True))
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


if __name__ == "__main__":
    unittest.main()

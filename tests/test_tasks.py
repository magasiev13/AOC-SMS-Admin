import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestSendBulkJob(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "sms.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import MessageLog
        from app.tasks import _should_mark_failed, send_bulk_job

        self.db = db
        self.MessageLog = MessageLog
        self._should_mark_failed = _should_mark_failed
        self.send_bulk_job = send_bulk_job
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def _create_log(self, *, details: list | None = None, organization_id: int | None = None) -> int:
        log = self.MessageLog(
            organization_id=organization_id,
            message_body="Test message",
            target="community",
            status="processing",
            total_recipients=0,
            success_count=0,
            failure_count=0,
            details=json.dumps(details or []),
        )
        self.db.session.add(log)
        self.db.session.commit()
        return log.id

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_generic_send_error_preserves_existing_details(self, mock_get_twilio, mock_process_failure_details) -> None:
        log_id = self._create_log(details=[{"phone": "+15550000001", "success": True, "error": None}])
        recipients = [
            {"phone": "+15550000001", "name": "Already Processed"},
            {"phone": "+15550000002", "name": "Will Fail"},
        ]

        mock_service = MagicMock()
        mock_service.send_bulk.side_effect = ValueError("provider down")
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.return_value = {}

        self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "failed")
        details = json.loads(log.details or "[]")
        self.assertGreaterEqual(len(details), 2)
        self.assertEqual(details[0].get("phone"), "+15550000001")
        self.assertTrue(any(detail.get("error") == "provider down" for detail in details))

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_post_processing_failure_does_not_mark_successful_send_failed(
        self,
        mock_get_twilio,
        mock_process_failure_details,
    ) -> None:
        log_id = self._create_log(details=[])
        recipients = [{"phone": "+15550000003", "name": "Success"}]

        mock_service = MagicMock()
        mock_service.send_bulk.return_value = {
            "total": 1,
            "success_count": 1,
            "failure_count": 0,
            "details": [{"phone": "+15550000003", "success": True, "error": None}],
        }
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.side_effect = RuntimeError("post-processing failed")

        self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "sent")
        details = json.loads(log.details or "[]")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].get("phone"), "+15550000003")
        self.assertTrue(details[0].get("success"))

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    @patch("app.tasks.create_app")
    def test_send_bulk_job_fails_closed_when_saas_org_loses_billing_access(
        self,
        mock_create_app,
        mock_get_twilio,
        mock_process_failure_details,
    ) -> None:
        from app.models import Organization, OrganizationMessagingProfile, OrganizationSubscription

        self.app.config["SAAS_MODE"] = True
        mock_create_app.return_value = self.app

        organization = Organization(name="Blocked Org", slug="blocked-org", status="active")
        subscription = OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status="incomplete",
        )
        profile = OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            provider_status="active",
            status="active",
            sender_review_status="approved",
            messaging_service_sid="MGblocked0001",
            from_number="+15550001111",
            phone_number_sid="PNblocked0001",
        )
        self.db.session.add_all([organization, subscription, profile])
        self.db.session.commit()

        log_id = self._create_log(organization_id=organization.id, details=[])
        recipients = [{"phone": "+15550000007", "name": "Blocked Recipient"}]

        self.send_bulk_job(log_id, organization.id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.failure_count, 1)
        details = json.loads(log.details or "[]")
        self.assertTrue(any("billing is not active" in (detail.get("error") or "") for detail in details))
        mock_get_twilio.assert_not_called()
        mock_process_failure_details.assert_not_called()

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    @patch("app.tasks.create_app")
    def test_send_bulk_job_fails_closed_when_saas_org_is_suspended(
        self,
        mock_create_app,
        mock_get_twilio,
        mock_process_failure_details,
    ) -> None:
        from app.models import Organization, OrganizationMessagingProfile, OrganizationSubscription

        self.app.config["SAAS_MODE"] = True
        mock_create_app.return_value = self.app

        organization = Organization(name="Suspended Org", slug="suspended-org", status="suspended")
        subscription = OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status="active",
        )
        profile = OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            provider_status="active",
            status="active",
            sender_review_status="approved",
            messaging_service_sid="MGsuspended0001",
            from_number="+15550001111",
            phone_number_sid="PNsuspended0001",
        )
        self.db.session.add_all([organization, subscription, profile])
        self.db.session.commit()

        log_id = self._create_log(organization_id=organization.id, details=[])
        recipients = [{"phone": "+15550000009", "name": "Suspended Recipient"}]

        self.send_bulk_job(log_id, organization.id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.failure_count, 1)
        details = json.loads(log.details or "[]")
        self.assertTrue(any("not active for message sending" in (detail.get("error") or "") for detail in details))
        mock_get_twilio.assert_not_called()
        mock_process_failure_details.assert_not_called()

    @patch("app.tasks.record_usage_candidates")
    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    @patch("app.tasks.create_app")
    def test_send_bulk_job_allows_active_saas_org_with_active_provider(
        self,
        mock_create_app,
        mock_get_twilio,
        mock_process_failure_details,
        mock_record_usage_candidates,
    ) -> None:
        from app.models import Organization, OrganizationMessagingProfile, OrganizationSubscription

        self.app.config["SAAS_MODE"] = True
        mock_create_app.return_value = self.app

        organization = Organization(name="Active Org", slug="active-org", status="active")
        subscription = OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status="active",
        )
        profile = OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            provider_status="active",
            status="active",
            sender_review_status="approved",
            messaging_service_sid="MGactive0001",
            from_number="+15550001111",
            phone_number_sid="PNactive0001",
        )
        self.db.session.add_all([organization, subscription, profile])
        self.db.session.commit()

        log_id = self._create_log(organization_id=organization.id, details=[])
        recipients = [{"phone": "+15550000008", "name": "Active Recipient"}]
        mock_service = MagicMock()
        mock_service.send_bulk.return_value = {
            "total": 1,
            "success_count": 1,
            "failure_count": 0,
            "details": [{"phone": "+15550000008", "success": True, "error": None, "sid": "SMactive-1"}],
        }
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.return_value = {}

        self.send_bulk_job(log_id, organization.id, recipients, "Hello", delay=0)

        mock_get_twilio.assert_called_once_with(organization.id)
        mock_service.send_bulk.assert_called_once()
        mock_record_usage_candidates.assert_called_once()
        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.failure_count, 0)
        details = json.loads(log.details or "[]")
        self.assertFalse(
            any("Messaging provider is not active" in (detail.get("error") or "") for detail in details)
        )

    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_send_bulk_job_dedupes_normalized_duplicate_phones_before_send(
        self,
        mock_get_twilio,
        mock_process_failure_details,
    ) -> None:
        log_id = self._create_log(details=[])
        recipients = [
            {"phone": "(555) 000-0007", "name": "First"},
            {"phone": "+15550000007", "name": "Duplicate"},
        ]

        mock_service = MagicMock()
        mock_service.send_bulk.return_value = {
            "total": 1,
            "success_count": 1,
            "failure_count": 0,
            "details": [{"phone": "+15550000007", "success": True, "error": None}],
        }
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.return_value = {}

        self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        send_args = mock_service.send_bulk.call_args.args[0]
        self.assertEqual(send_args, [{"phone": "+15550000007", "name": "First"}])
        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.total_recipients, 1)
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.failure_count, 0)

    @patch("app.tasks.record_usage_candidates")
    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_usage_recording_failure_does_not_mark_successful_send_failed(
        self,
        mock_get_twilio,
        mock_process_failure_details,
        mock_record_usage_candidates,
    ) -> None:
        log_id = self._create_log(details=[])
        recipients = [{"phone": "+15550000004", "name": "Success"}]

        mock_service = MagicMock()
        mock_service.send_bulk.return_value = {
            "total": 1,
            "success_count": 1,
            "failure_count": 0,
            "details": [{"phone": "+15550000004", "success": True, "error": None, "sid": "SMusage-1"}],
        }
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.return_value = {}
        mock_record_usage_candidates.side_effect = RuntimeError("usage ledger down")

        self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.failure_count, 0)

    @patch("app.tasks.get_current_job")
    @patch("app.tasks.record_usage_candidates")
    @patch("app.tasks.process_failure_details")
    @patch("app.tasks.get_twilio_service")
    def test_usage_recording_failure_does_not_mark_transient_retry_failed(
        self,
        mock_get_twilio,
        mock_process_failure_details,
        mock_record_usage_candidates,
        mock_get_current_job,
    ) -> None:
        from app.services.twilio_service import TwilioTransientError

        log_id = self._create_log(details=[])
        recipients = [
            {"phone": "+15550000005", "name": "First"},
            {"phone": "+15550000006", "name": "Second"},
        ]

        mock_service = MagicMock()
        mock_service.send_bulk.side_effect = TwilioTransientError(
            "provider retry",
            results={
                "total": 2,
                "success_count": 1,
                "failure_count": 0,
                "details": [{"phone": "+15550000005", "success": True, "error": None, "sid": "SMusage-2"}],
            },
            failed_index=1,
        )
        mock_get_twilio.return_value = mock_service
        mock_process_failure_details.return_value = {}
        mock_record_usage_candidates.side_effect = RuntimeError("usage ledger down")
        mock_get_current_job.return_value = type("Job", (), {"retries_left": 1})()

        with self.assertRaises(TwilioTransientError):
            self.send_bulk_job(log_id, recipients, "Hello", delay=0)

        self.db.session.expire_all()
        log = self.db.session.get(self.MessageLog, log_id)
        self.assertEqual(log.status, "processing")
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.failure_count, 0)
        details = json.loads(log.details or "[]")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].get("phone"), "+15550000005")

    @patch("app.tasks.get_current_job", return_value=None)
    def test_should_mark_failed_when_no_job_context(self, _mock_get_job) -> None:
        self.assertTrue(self._should_mark_failed())

    @patch("app.tasks.get_current_job")
    def test_should_mark_failed_when_retries_left_is_zero(self, mock_get_job) -> None:
        mock_get_job.return_value = type("Job", (), {"retries_left": 0})()
        self.assertTrue(self._should_mark_failed())

    @patch("app.tasks.get_current_job")
    def test_should_not_mark_failed_when_retries_remaining(self, mock_get_job) -> None:
        mock_get_job.return_value = type("Job", (), {"retries_left": 2})()
        self.assertFalse(self._should_mark_failed())

    @patch("app.tasks.get_current_job")
    def test_should_mark_failed_when_retry_metadata_missing(self, mock_get_job) -> None:
        mock_get_job.return_value = type("Job", (), {"retries_left": None})()
        self.assertTrue(self._should_mark_failed())

    @patch("app.tasks.get_current_job")
    def test_should_mark_failed_when_retry_metadata_unavailable(self, mock_get_job) -> None:
        mock_get_job.return_value = object()
        self.assertTrue(self._should_mark_failed())

class TestA2PTaskJobs(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "sms.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SAAS_MODE": "1",
                "STRIPE_SECRET_KEY": "sk_test_123",
                "STRIPE_WEBHOOK_SECRET": "whsec_test_123",
                "STRIPE_PRICE_ID": "price_test_123",
                "SAAS_BASE_URL": "https://app.example.com",
                "TWILIO_CREDENTIAL_ENCRYPTION_KEY": "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=",
                "TWILIO_A2P_ONBOARDING_ENABLED": "1",
                "TWILIO_PRIMARY_CUSTOMER_PROFILE_SID": "BUprimary123",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.tasks import process_a2p_onboarding_job, reconcile_a2p_onboardings_job, sync_a2p_onboarding_status_job

        self.db = db
        self.process_a2p_onboarding_job = process_a2p_onboarding_job
        self.reconcile_a2p_onboardings_job = reconcile_a2p_onboardings_job
        self.sync_a2p_onboarding_status_job = sync_a2p_onboarding_status_job
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    @patch("app.tasks.process_a2p_onboarding")
    def test_process_a2p_onboarding_job_returns_status_summary(self, mock_process) -> None:
        mock_process.return_value = type(
            "Onboarding",
            (),
            {"onboarding_status": "approved", "brand_status": "approved", "campaign_status": "approved"},
        )()

        result = self.process_a2p_onboarding_job(12, 99)

        self.assertEqual(
            result,
            {
                "organization_id": 12,
                "onboarding_status": "approved",
                "brand_status": "approved",
                "campaign_status": "approved",
            },
        )
        mock_process.assert_called_once_with(12, actor_user_id=99)

    @patch("app.tasks.sync_a2p_onboarding_status")
    def test_sync_a2p_onboarding_status_job_returns_status_summary(self, mock_sync) -> None:
        mock_sync.return_value = type(
            "Onboarding",
            (),
            {"onboarding_status": "pending", "brand_status": "approved", "campaign_status": "in_progress"},
        )()

        result = self.sync_a2p_onboarding_status_job(21, 77)

        self.assertEqual(
            result,
            {
                "organization_id": 21,
                "onboarding_status": "pending",
                "brand_status": "approved",
                "campaign_status": "in_progress",
            },
        )
        mock_sync.assert_called_once_with(21, actor_user_id=77)

    @patch("app.tasks.reconcile_pending_a2p_onboardings")
    def test_reconcile_a2p_onboardings_job_returns_service_summary(self, mock_reconcile) -> None:
        mock_reconcile.return_value = {"records_seen": 3, "records_processed": 2, "records_failed": 1}

        result = self.reconcile_a2p_onboardings_job()

        self.assertEqual(result["records_seen"], 3)
        mock_reconcile.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

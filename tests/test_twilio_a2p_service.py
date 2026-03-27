import importlib
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestTwilioA2PService(unittest.TestCase):
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
                "SCHEDULER_ENABLED": "0",
                "STRIPE_SECRET_KEY": "sk_test_123",
                "STRIPE_WEBHOOK_SECRET": "whsec_test_123",
                "STRIPE_PRICE_ID": "price_test_123",
                "SAAS_BASE_URL": "https://beta.example.com",
                "TWILIO_CREDENTIAL_ENCRYPTION_KEY": "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=",
                "TWILIO_ACCOUNT_SID": "ACmaster123",
                "TWILIO_AUTH_TOKEN": "master-token",
                "TWILIO_A2P_ONBOARDING_ENABLED": "1",
                "TWILIO_PRIMARY_CUSTOMER_PROFILE_SID": "BUprimary123",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import Organization, OrganizationA2POnboarding, OrganizationMessagingProfile
        from app.services.twilio_a2p_service import (
            _upsert_a2p_resources,
            ensure_a2p_onboarding,
            process_a2p_onboarding,
            ProviderProvisioningError,
            submit_a2p_onboarding,
        )

        self.db = db
        self.Organization = Organization
        self.OrganizationA2POnboarding = OrganizationA2POnboarding
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.ProviderProvisioningError = ProviderProvisioningError
        self._upsert_a2p_resources = _upsert_a2p_resources
        self.ensure_a2p_onboarding = ensure_a2p_onboarding
        self.process_a2p_onboarding = process_a2p_onboarding
        self.submit_a2p_onboarding = submit_a2p_onboarding

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.messaging_profile = self.OrganizationMessagingProfile(
            organization=self.organization,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACsub0001",
            messaging_service_sid="MGsub0001",
            status="pending",
            provider_status="pending",
        )
        self.db.session.add_all([self.organization, self.messaging_profile])
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_persists_and_queues_job(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            {
                "registration_path": "nonprofit",
                "number_strategy": "auto_buy",
                "business_name": "Acme Nonprofit",
                "business_type": "Nonprofit",
                "email": "ops@acme.test",
                "phone_number": "+15550000001",
                "mobile_number": "+15550000002",
                "first_name": "Jane",
                "last_name": "Doe",
                "job_position": "Director",
                "campaign_description": "Community updates",
                "message_flow": "Users opt in on the website and reply STOP to opt out.",
                "message_samples": "Acme reminder 1\nAcme reminder 2",
                "campaign_use_case": "MIXED",
                "has_embedded_links": "on",
                "has_embedded_phone": "on",
            },
            actor_user_id=42,
        )

        self.assertEqual(onboarding.onboarding_status, "queued")
        self.assertEqual(onboarding.registration_path, "nonprofit")
        self.assertEqual(onboarding.number_strategy, "auto_buy")
        self.assertEqual(onboarding.campaign_use_case, "MIXED")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 42)

    def test_submit_a2p_onboarding_rejects_invalid_number_strategy_payload(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "Choose a valid Twilio A2P number strategy."):
            self.submit_a2p_onboarding(
                self.organization.id,
                {
                    "registration_path": "standard",
                    "number_strategy": "shared_parent_number",
                    "business_name": "Acme",
                    "email": "ops@acme.test",
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "campaign_description": "Community updates",
                    "message_flow": "Users opt in.",
                    "message_samples": "Sample message",
                },
                actor_user_id=7,
            )

    def test_submit_a2p_onboarding_requires_phone_number_sid_for_existing_number_strategy(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "phone number SID is required"):
            self.submit_a2p_onboarding(
                self.organization.id,
                {
                    "registration_path": "standard",
                    "number_strategy": "existing_subaccount_number",
                    "business_name": "Acme",
                    "email": "ops@acme.test",
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "campaign_description": "Community updates",
                    "message_flow": "Users opt in.",
                    "message_samples": "Sample message",
                },
                actor_user_id=7,
            )

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_marks_record_error_when_queueing_fails(self, mock_get_queue) -> None:
        queue = MagicMock()
        queue.enqueue.side_effect = RuntimeError("redis is unavailable")
        mock_get_queue.return_value = queue

        with self.assertRaisesRegex(self.ProviderProvisioningError, "could not be queued"):
            self.submit_a2p_onboarding(
                self.organization.id,
                {
                    "registration_path": "standard",
                    "number_strategy": "auto_buy",
                    "business_name": "Acme",
                    "email": "ops@acme.test",
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "campaign_description": "Community updates",
                    "message_flow": "Users opt in.",
                    "message_samples": "Sample message",
                },
                actor_user_id=21,
            )

        onboarding = self.OrganizationA2POnboarding.query.filter_by(organization_id=self.organization.id).first()
        self.assertIsNotNone(onboarding)
        self.assertEqual(onboarding.onboarding_status, "error")
        self.assertIn("could not be queued", onboarding.last_error or "")
        self.assertEqual(self.messaging_profile.provider_status, "error")

    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_upsert_a2p_resources_assigns_primary_customer_profile_and_submits_reviews(self, mock_build_subaccount_client) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client
        mock_client.trusthub.v1.customer_profiles.create.return_value.sid = "BUcustomer123"
        mock_client.trusthub.v1.trust_products.create.return_value.sid = "BUtrust123"
        mock_client.trusthub.v1.end_users.create.side_effect = [
            MagicMock(sid="ITbusiness123"),
            MagicMock(sid="ITauthorized123"),
            MagicMock(sid="ITprofile123"),
        ]
        mock_client.trusthub.v1.customer_profiles.return_value.customer_profiles_evaluations.create.return_value.sid = (
            "ELcustomer123"
        )
        mock_client.trusthub.v1.trust_products.return_value.trust_products_evaluations.create.return_value.sid = (
            "ELtrust123"
        )
        mock_client.messaging.v1.brand_registrations.create.return_value.sid = "BNbrand123"
        mock_client.messaging.v1.services.return_value.us_app_to_person.create.return_value.sid = "QEcampaign123"

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample"]'
        onboarding.raw_submission_json = "{}"

        self._upsert_a2p_resources(onboarding, self.messaging_profile)

        customer_assignment_calls = (
            mock_client.trusthub.v1.customer_profiles.return_value.customer_profiles_entity_assignments.create.call_args_list
        )
        trust_assignment_calls = (
            mock_client.trusthub.v1.trust_products.return_value.trust_products_entity_assignments.create.call_args_list
        )

        self.assertIn(((), {"object_sid": "BUprimary123"}), customer_assignment_calls)
        self.assertIn(((), {"object_sid": "ITprofile123"}), trust_assignment_calls)
        self.assertIn(((), {"object_sid": "BUcustomer123"}), trust_assignment_calls)
        mock_client.trusthub.v1.customer_profiles.return_value.update.assert_called_once_with(status="pending-review")
        mock_client.trusthub.v1.trust_products.return_value.update.assert_called_once_with(status="pending-review")
        self.assertEqual(onboarding.customer_profile_sid, "BUcustomer123")
        self.assertEqual(onboarding.trust_product_sid, "BUtrust123")
        self.assertEqual(onboarding.brand_registration_sid, "BNbrand123")
        self.assertEqual(onboarding.campaign_sid, "QEcampaign123")

    @patch("app.services.twilio_a2p_service._complete_number_setup")
    @patch("app.services.twilio_a2p_service._sync_remote_status", return_value=("approved", "approved"))
    @patch("app.services.twilio_a2p_service._upsert_a2p_resources")
    def test_process_a2p_onboarding_marks_profile_active_when_brand_and_campaign_are_ready(
        self,
        mock_upsert,
        _mock_sync,
        mock_complete_number_setup,
    ) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample"]'
        onboarding.onboarding_status = "queued"
        self.db.session.commit()

        result = self.process_a2p_onboarding(self.organization.id, actor_user_id=7)

        self.assertEqual(result.onboarding_status, "approved")
        self.assertEqual(result.brand_status, "approved")
        self.assertEqual(result.campaign_status, "approved")
        self.assertIsNotNone(result.approved_at)
        self.assertEqual(self.messaging_profile.provider_status, "active")
        mock_upsert.assert_called_once()
        mock_complete_number_setup.assert_called_once()


if __name__ == "__main__":
    unittest.main()

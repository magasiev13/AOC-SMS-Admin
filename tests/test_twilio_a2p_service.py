import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestTwilioA2PService(unittest.TestCase):
    MIXED_MESSAGE_SAMPLES = "Sample message 1\nSample message 2"

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
                "SAAS_BASE_URL": "https://app.example.com",
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
        from app.services.provider_secret_service import encrypt_provider_secret
        from app.services.twilio_a2p_service import (
            _create_a2p_campaign,
            _sync_remote_status,
            _upsert_a2p_resources,
            create_missing_a2p_campaign,
            describe_a2p_onboarding,
            ensure_a2p_onboarding,
            ingest_a2p_event_stream_payload,
            process_a2p_onboarding,
            ProviderProvisioningError,
            reconcile_pending_a2p_onboardings,
            reconcile_a2p_twilio_state,
            submit_a2p_onboarding,
            sync_a2p_onboarding_status,
        )

        self.db = db
        self.Organization = Organization
        self.OrganizationA2POnboarding = OrganizationA2POnboarding
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.ProviderProvisioningError = ProviderProvisioningError
        self.encrypt_provider_secret = encrypt_provider_secret
        self._create_a2p_campaign = _create_a2p_campaign
        self._sync_remote_status = _sync_remote_status
        self._upsert_a2p_resources = _upsert_a2p_resources
        self.create_missing_a2p_campaign = create_missing_a2p_campaign
        self.describe_a2p_onboarding = describe_a2p_onboarding
        self.ensure_a2p_onboarding = ensure_a2p_onboarding
        self.ingest_a2p_event_stream_payload = ingest_a2p_event_stream_payload
        self.process_a2p_onboarding = process_a2p_onboarding
        self.reconcile_pending_a2p_onboardings = reconcile_pending_a2p_onboardings
        self.submit_a2p_onboarding = submit_a2p_onboarding
        self.reconcile_a2p_twilio_state = reconcile_a2p_twilio_state
        self.sync_a2p_onboarding_status = sync_a2p_onboarding_status

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.messaging_profile = self.OrganizationMessagingProfile(
            organization=self.organization,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACsub0001",
            twilio_auth_token_encrypted=encrypt_provider_secret("subaccount-token"),
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

    def _valid_submission_payload(self, **overrides):
        payload = {
            "registration_path": "standard",
            "number_strategy": "auto_buy",
            "business_name": "Acme",
            "legal_business_name": "Acme",
            "public_brand_name": "Acme",
            "business_type": "LLC",
            "business_industry": "TECHNOLOGY",
            "has_business_tax_id": "on",
            "business_regions": ["USA_AND_CANADA"],
            "business_registration_identifier": "EIN",
            "business_registration_number": "12-3456789",
            "has_public_website": "on",
            "website_url": "https://acme.test",
            "email": "ops@acme.test",
            "notification_email": "ops@acme.test",
            "phone_number": "+15550000001",
            "mobile_number": "+15550000002",
            "first_name": "Jane",
            "last_name": "Doe",
            "business_title": "Owner",
            "job_position": "Director",
            "address_country": "US",
            "address_line1": "123 Main Street",
            "address_city": "Denver",
            "address_region": "CO",
            "address_postal_code": "80202",
            "campaign_description": "Community updates",
            "message_flow": "Users opt in on the website and reply STOP to opt out.",
            "message_samples": self.MIXED_MESSAGE_SAMPLES,
            "campaign_use_case": "MIXED",
        }
        payload.update(overrides)
        return payload

    def _populate_onboarding_profile(self, onboarding, **overrides):
        onboarding.business_industry = "TECHNOLOGY"
        onboarding.business_regions_json = '["USA_AND_CANADA"]'
        onboarding.website_url = "https://acme.test"
        onboarding.notification_email = "ops@acme.test"
        onboarding.business_title = "Owner"
        onboarding.job_position = "Director"
        onboarding.address_country = "US"
        onboarding.address_line1 = "123 Main Street"
        onboarding.address_city = "Denver"
        onboarding.address_region = "CO"
        onboarding.address_postal_code = "80202"
        for key, value in overrides.items():
            setattr(onboarding, key, value)
        return onboarding

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_persists_and_queues_job(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                registration_path="nonprofit",
                business_name="Acme Nonprofit",
                business_type="Nonprofit",
                business_registration_identifier="EIN",
                business_registration_number="12-3456789",
                message_samples="Acme reminder 1\nAcme reminder 2",
                has_embedded_links="on",
                has_embedded_phone="on",
            ),
            actor_user_id=42,
        )

        self.assertEqual(onboarding.onboarding_status, "queued")
        self.assertEqual(onboarding.registration_path, "nonprofit")
        self.assertEqual(onboarding.number_strategy, "auto_buy")
        self.assertEqual(onboarding.campaign_use_case, "MIXED")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 42)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_promotes_imported_service_address_to_app_input(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue
        self.messaging_profile.service_address_country = "US"
        self.messaging_profile.service_address_line1 = "9 Imported Road"
        self.messaging_profile.service_address_city = "Boulder"
        self.messaging_profile.service_address_region = "CO"
        self.messaging_profile.service_address_postal_code = "80301"
        self.messaging_profile.service_address_source_mode = "twilio_import"
        self.messaging_profile.twilio_address_sid = "ADimported0001"
        self.messaging_profile.twilio_address_json = '{"sid":"ADimported0001"}'
        self.messaging_profile.emergency_address_sid = "ADimported0001"
        self.messaging_profile.emergency_address_status = "synced"
        self.messaging_profile.emergency_address_last_error = "stale error"
        self.db.session.commit()

        self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                address_line1="123 Main Street",
                address_city="Denver",
                address_region="CO",
                address_postal_code="80202",
            ),
            actor_user_id=42,
        )

        self.db.session.refresh(self.messaging_profile)
        self.assertEqual(self.messaging_profile.service_address_line1, "123 Main Street")
        self.assertEqual(self.messaging_profile.service_address_city, "Denver")
        self.assertEqual(self.messaging_profile.service_address_source_mode, "app_input")
        self.assertIsNone(self.messaging_profile.twilio_address_sid)
        self.assertIsNone(self.messaging_profile.twilio_address_json)
        self.assertIsNone(self.messaging_profile.emergency_address_sid)
        self.assertIsNone(self.messaging_profile.emergency_address_status)
        self.assertIsNone(self.messaging_profile.emergency_address_last_error)
        self.assertEqual(self.messaging_profile.sender_finalization_status, "awaiting_a2p_approval")

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_refresh_a2p_onboarding_queues_status_only_job(self, mock_get_queue) -> None:
        from app.services.twilio_a2p_service import refresh_a2p_onboarding

        queue = MagicMock()
        mock_get_queue.return_value = queue
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.campaign_sid = "QEcampaign123"
        self.db.session.commit()

        refresh_a2p_onboarding(self.organization.id, actor_user_id=55)

        queue.enqueue.assert_called_once_with("app.tasks.sync_a2p_onboarding_status_job", self.organization.id, 55)

    @patch("app.services.twilio_a2p_service.process_a2p_onboarding")
    @patch("app.services.twilio_a2p_service.sync_a2p_onboarding_status")
    def test_reconcile_pending_a2p_onboardings_routes_by_status(self, mock_sync, mock_process) -> None:
        queued_org = self.Organization(name="Queued", slug="queued", status="active")
        queued_profile = self.OrganizationMessagingProfile(
            organization=queued_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACqueued",
            twilio_auth_token_encrypted=self.encrypt_provider_secret("queued-token"),
            messaging_service_sid="MGqueued",
            status="pending",
            provider_status="pending",
        )
        queued_onboarding = self.OrganizationA2POnboarding(
            organization=queued_org,
            onboarding_status="queued",
            campaign_use_case="ACCOUNT_NOTIFICATION",
        )
        processing_org = self.Organization(name="Processing", slug="processing", status="active")
        processing_profile = self.OrganizationMessagingProfile(
            organization=processing_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACprocessing",
            twilio_auth_token_encrypted=self.encrypt_provider_secret("processing-token"),
            messaging_service_sid="MGprocessing",
            status="pending",
            provider_status="pending",
        )
        processing_onboarding = self.OrganizationA2POnboarding(
            organization=processing_org,
            onboarding_status="processing",
            campaign_use_case="ACCOUNT_NOTIFICATION",
        )
        pending_org = self.Organization(name="Pending", slug="pending", status="active")
        pending_profile = self.OrganizationMessagingProfile(
            organization=pending_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACpending",
            twilio_auth_token_encrypted=self.encrypt_provider_secret("pending-token"),
            messaging_service_sid="MGpending",
            status="pending",
            provider_status="pending",
        )
        pending_onboarding = self.OrganizationA2POnboarding(
            organization=pending_org,
            onboarding_status="pending",
            campaign_use_case="ACCOUNT_NOTIFICATION",
        )
        needs_action_org = self.Organization(name="Needs Action", slug="needs-action", status="active")
        needs_action_profile = self.OrganizationMessagingProfile(
            organization=needs_action_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACneedsaction",
            twilio_auth_token_encrypted=self.encrypt_provider_secret("needs-action-token"),
            messaging_service_sid="MGneedsaction",
            status="pending",
            provider_status="pending",
        )
        needs_action_onboarding = self.OrganizationA2POnboarding(
            organization=needs_action_org,
            onboarding_status="needs_action",
            campaign_use_case="ACCOUNT_NOTIFICATION",
        )
        self.db.session.add_all(
            [
                queued_org,
                queued_profile,
                queued_onboarding,
                processing_org,
                processing_profile,
                processing_onboarding,
                pending_org,
                pending_profile,
                pending_onboarding,
                needs_action_org,
                needs_action_profile,
                needs_action_onboarding,
            ]
        )
        self.db.session.commit()

        summary = self.reconcile_pending_a2p_onboardings()

        self.assertEqual(summary, {"records_seen": 4, "records_processed": 4, "records_failed": 0})
        mock_process.assert_any_call(queued_org.id)
        mock_process.assert_any_call(processing_org.id)
        self.assertEqual(mock_process.call_count, 2)
        mock_sync.assert_any_call(pending_org.id)
        mock_sync.assert_any_call(needs_action_org.id)
        self.assertEqual(mock_sync.call_count, 2)

    @patch("app.services.twilio_a2p_service.process_a2p_onboarding")
    @patch("app.services.twilio_a2p_service.sync_a2p_onboarding_status")
    def test_reconcile_pending_a2p_onboardings_allows_status_sync_when_automation_disabled(
        self,
        mock_sync,
        mock_process,
    ) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.brand_status = "approved"
        onboarding.campaign_status = "in_progress"
        self.db.session.commit()
        self.app.config["TWILIO_A2P_ONBOARDING_ENABLED"] = False
        mock_sync.return_value = onboarding

        summary = self.reconcile_pending_a2p_onboardings()

        self.assertEqual(summary, {"records_seen": 1, "records_processed": 1, "records_failed": 0})
        mock_sync.assert_called_once_with(self.organization.id)
        mock_process.assert_not_called()

    def test_submit_a2p_onboarding_rejects_invalid_number_strategy_payload(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "Choose a valid Twilio A2P number strategy."):
            payload = self._valid_submission_payload(registration_path="standard")
            payload["number_strategy"] = "shared_parent_number"
            self.submit_a2p_onboarding(
                self.organization.id,
                payload,
                actor_user_id=7,
            )

    def test_submit_a2p_onboarding_requires_phone_number_sid_for_existing_number_strategy(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "phone number SID is required"):
            payload = self._valid_submission_payload(
                registration_path="standard",
                number_strategy="existing_subaccount_number",
            )
            self.submit_a2p_onboarding(
                self.organization.id,
                payload,
                actor_user_id=7,
            )

    def test_submit_a2p_onboarding_requires_business_type_for_standard_paths(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "Business type is required"):
            payload = self._valid_submission_payload(registration_path="standard")
            payload.pop("business_type")
            self.submit_a2p_onboarding(
                self.organization.id,
                payload,
                actor_user_id=7,
            )

    def test_submit_a2p_onboarding_requires_registration_identifier_for_non_sole_paths(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "registration identifier is required"):
            payload = self._valid_submission_payload(
                registration_path="low_volume_standard",
                business_type="LLC",
            )
            payload.pop("business_registration_identifier")
            self.submit_a2p_onboarding(
                self.organization.id,
                payload,
                actor_user_id=7,
            )

    def test_submit_a2p_onboarding_requires_registration_number_for_non_sole_paths(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "registration number is required"):
            payload = self._valid_submission_payload(
                registration_path="low_volume_standard",
                business_type="LLC",
            )
            payload.pop("business_registration_number")
            self.submit_a2p_onboarding(
                self.organization.id,
                payload,
                actor_user_id=7,
            )

    def test_submit_a2p_onboarding_treats_none_literal_as_missing_business_type(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "Business type is required"):
            self.submit_a2p_onboarding(
                self.organization.id,
                self._valid_submission_payload(
                    registration_path="standard",
                    business_type="None",
                ),
                actor_user_id=7,
            )

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_defaults_nonprofit_business_type_from_registration_path(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                registration_path="nonprofit",
                business_name="Acme Nonprofit",
                business_type=None,
                business_registration_identifier="EIN",
                business_registration_number="12-3456789",
            ),
            actor_user_id=11,
        )

        self.assertEqual(onboarding.business_type, "Non-profit Corporation")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 11)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_normalizes_llc_business_type_aliases(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                registration_path="low_volume_standard",
                business_type="Limited Liability Company",
            ),
            actor_user_id=12,
        )

        self.assertEqual(onboarding.business_type, "Limited Liability Corporation")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 12)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_allows_blank_registration_details_for_sole_proprietor(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                registration_path="sole_proprietor",
                business_name="Jane Doe",
                legal_business_name="Jane Doe",
                public_brand_name="Jane Doe",
                has_business_tax_id="",
                business_registration_identifier=None,
                business_registration_number=None,
                message_samples="Sample 1\nSample 2",
                campaign_use_case="SOLE_PROPRIETOR",
            ),
            actor_user_id=13,
        )

        self.assertEqual(onboarding.business_type, "Sole Proprietor")
        self.assertIsNone(onboarding.business_registration_identifier)
        self.assertIsNone(onboarding.business_registration_number_encrypted)
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 13)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_defaults_account_notification_use_case(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                registration_path="standard",
                business_type="LLC",
                campaign_use_case="",
                campaign_description="Account reminders",
                message_samples="Sample message one\nSample message two",
            ),
            actor_user_id=18,
        )

        self.assertEqual(onboarding.campaign_use_case, "ACCOUNT_NOTIFICATION")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 18)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_defaults_ein_backed_businesses_to_low_volume_standard(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        payload = self._valid_submission_payload()
        payload.pop("registration_path")

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            payload,
            actor_user_id=19,
        )

        self.assertEqual(onboarding.registration_path, "low_volume_standard")
        self.assertEqual(onboarding.brand_registration_mode, "low_volume_standard")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 19)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_routes_true_sole_proprietor_without_tax_id(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        payload = self._valid_submission_payload(
            business_name="Jane Doe",
            legal_business_name="Jane Doe",
            public_brand_name="Jane Doe",
            business_type="Sole Proprietor",
            business_registration_identifier=None,
            business_registration_number=None,
            has_business_tax_id="",
            campaign_use_case="SOLE_PROPRIETOR",
            message_samples="Sample 1\nSample 2",
        )
        payload.pop("registration_path")

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            payload,
            actor_user_id=20,
        )

        self.assertEqual(onboarding.registration_path, "sole_proprietor")
        self.assertEqual(onboarding.brand_registration_mode, "sole_proprietor")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 20)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_populates_hosted_compliance_defaults(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        payload = self._valid_submission_payload(
            has_public_website="",
            website_url="",
            privacy_policy_url="",
            terms_and_conditions_url="",
            cta_proof_url="",
        )

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            payload,
            actor_user_id=20,
        )

        self.assertEqual(onboarding.website_url, "https://app.example.com/compliance/acme/sms/opt-in")
        self.assertEqual(onboarding.privacy_policy_url, "https://app.example.com/compliance/acme/sms/privacy")
        self.assertEqual(onboarding.terms_and_conditions_url, "https://app.example.com/compliance/acme/sms/terms")
        self.assertEqual(onboarding.cta_proof_url, "https://app.example.com/compliance/acme/sms/opt-in")
        self.assertEqual(onboarding.submission_source_mode, "hosted_fallback")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 20)

    def test_submit_a2p_onboarding_requires_public_compliance_urls_when_hosted_defaults_unavailable(self) -> None:
        self.app.config["SAAS_BASE_URL"] = ""

        with self.assertRaisesRegex(self.ProviderProvisioningError, "Privacy policy URL is required"):
            self.submit_a2p_onboarding(
                self.organization.id,
                self._valid_submission_payload(
                    privacy_policy_url="",
                    terms_and_conditions_url="",
                    cta_proof_url="",
                ),
                actor_user_id=21,
            )

    def test_submit_a2p_onboarding_requires_two_message_samples(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "at least two real message samples"):
            self.submit_a2p_onboarding(
                self.organization.id,
                self._valid_submission_payload(
                    message_samples="Only one sample",
                ),
                actor_user_id=19,
            )

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_falls_back_to_hosted_pages_when_external_site_is_incomplete(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                has_public_website="on",
                privacy_policy_url="https://acme.test/privacy",
                terms_and_conditions_url="https://acme.test/terms",
                cta_proof_url="https://acme.test/opt-in",
                website_url=None,
            ),
            actor_user_id=22,
        )

        self.assertEqual(onboarding.submission_source_mode, "hosted_fallback")
        self.assertEqual(onboarding.website_url, "https://app.example.com/compliance/acme/sms/opt-in")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 22)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_uses_external_site_when_all_public_urls_validate(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.submit_a2p_onboarding(
            self.organization.id,
            self._valid_submission_payload(
                has_public_website="on",
                external_website_url="https://acme.test/contact",
                external_privacy_policy_url="https://acme.test/privacy",
                external_terms_and_conditions_url="https://acme.test/terms",
                external_cta_proof_url="https://acme.test/opt-in",
            ),
            actor_user_id=23,
        )

        self.assertEqual(onboarding.submission_source_mode, "external_site")
        self.assertEqual(onboarding.website_url, "https://acme.test/contact")
        self.assertEqual(onboarding.privacy_policy_url, "https://acme.test/privacy")
        self.assertEqual(onboarding.external_cta_proof_url, "https://acme.test/opt-in")
        queue.enqueue.assert_called_once_with("app.tasks.process_a2p_onboarding_job", self.organization.id, 23)

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_submit_a2p_onboarding_marks_record_error_when_queueing_fails(self, mock_get_queue) -> None:
        queue = MagicMock()
        queue.enqueue.side_effect = RuntimeError("redis is unavailable")
        mock_get_queue.return_value = queue

        with self.assertRaisesRegex(self.ProviderProvisioningError, "could not be queued"):
            self.submit_a2p_onboarding(
                self.organization.id,
                self._valid_submission_payload(),
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
        mock_client.addresses.create.return_value.sid = "ADaddress123"
        mock_client.trusthub.v1.supporting_documents.create.return_value.sid = "RDsupport123"
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
        mock_client.messaging.v1.services.return_value.us_app_to_person._version.create.return_value = {"sid": "QEcampaign123"}

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.business_type = "LLC"
        onboarding.business_registration_identifier = "EIN"
        onboarding.business_registration_number_encrypted = self.encrypt_provider_secret("12-3456789")
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.raw_submission_json = "{}"
        self._populate_onboarding_profile(onboarding)

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
        first_end_user_call = mock_client.trusthub.v1.end_users.create.call_args_list[0]
        self.assertEqual(
            first_end_user_call.kwargs["attributes"]["business_type"],
            "Limited Liability Corporation",
        )
        self.assertEqual(first_end_user_call.kwargs["attributes"]["business_registration_identifier"], "EIN")
        self.assertEqual(first_end_user_call.kwargs["attributes"]["business_registration_number"], "12-3456789")
        self.assertEqual(onboarding.customer_profile_sid, "BUcustomer123")
        self.assertEqual(onboarding.trust_product_sid, "BUtrust123")
        self.assertEqual(onboarding.brand_registration_sid, "BNbrand123")
        self.assertIsNone(onboarding.campaign_sid)
        mock_client.addresses.create.assert_called_once()
        mock_client.messaging.v1.services.return_value.us_app_to_person._version.create.assert_not_called()

    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_upsert_a2p_resources_rejects_missing_registration_number_before_twilio_calls(self, mock_build_subaccount_client) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.business_type = "LLC"
        onboarding.business_registration_identifier = "EIN"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.raw_submission_json = "{}"
        self._populate_onboarding_profile(onboarding)

        with self.assertRaisesRegex(self.ProviderProvisioningError, "registration number is required"):
            self._upsert_a2p_resources(onboarding, self.messaging_profile)

        mock_client.trusthub.v1.end_users.create.assert_not_called()

    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_upsert_a2p_resources_rejects_single_message_sample_for_mixed_campaign(self, mock_build_subaccount_client) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.business_type = "LLC"
        onboarding.business_registration_identifier = "EIN"
        onboarding.business_registration_number_encrypted = self.encrypt_provider_secret("12-3456789")
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_use_case = "MIXED"
        onboarding.message_samples_json = '["Sample"]'
        onboarding.raw_submission_json = "{}"
        self._populate_onboarding_profile(onboarding)

        with self.assertRaisesRegex(self.ProviderProvisioningError, "at least two real message samples"):
            self._create_a2p_campaign(onboarding, self.messaging_profile)

        mock_client.messaging.v1.services.return_value.us_app_to_person._version.create.assert_not_called()

    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_create_a2p_campaign_persists_campaign_sid(self, mock_build_subaccount_client) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client
        mock_client.messaging.v1.services.return_value.us_app_to_person.list.return_value = []
        mock_client.messaging.v1.services.return_value.us_app_to_person._version.create.return_value = {
            "sid": "QEcampaign123"
        }

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.business_name = "Acme"
        onboarding.business_type = "LLC"
        onboarding.business_registration_identifier = "EIN"
        onboarding.business_registration_number_encrypted = self.encrypt_provider_secret("12-3456789")
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.privacy_policy_url = "https://app.example.com/compliance/acme/sms/privacy"
        onboarding.terms_and_conditions_url = "https://app.example.com/compliance/acme/sms/terms"
        onboarding.raw_submission_json = '{"has_embedded_links": true, "has_embedded_phone": false}'
        self._populate_onboarding_profile(onboarding)

        self._create_a2p_campaign(onboarding, self.messaging_profile)

        mock_client.messaging.v1.services.return_value.us_app_to_person._version.create.assert_called_once()
        self.assertEqual(onboarding.campaign_sid, "QEcampaign123")

    @patch("app.services.twilio_a2p_service.time.sleep")
    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_create_a2p_campaign_recreates_failed_campaign_when_use_case_changes(
        self,
        mock_build_subaccount_client,
        mock_sleep,
    ) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client
        service_context = mock_client.messaging.v1.services.return_value
        existing_campaign = MagicMock(
            sid="QEfailed123",
            campaign_status="FAILED",
            us_app_to_person_usecase="MIXED",
            brand_registration_sid="BNbrand123",
            errors=[
                {
                    "registrationerrorcode": "30909",
                    "registrationerrordescription": "CTA could not be verified.",
                }
            ],
            failure_reason=None,
        )
        service_context.us_app_to_person.list.return_value = [existing_campaign]
        delete_context = service_context.us_app_to_person.return_value
        delete_context.delete = MagicMock(return_value=True)
        service_context.us_app_to_person._version.create.return_value = {"sid": "QEnew123"}

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.business_name = "Acme"
        onboarding.business_type = "Limited Liability Corporation"
        onboarding.business_registration_identifier = "EIN"
        onboarding.business_registration_number_encrypted = self.encrypt_provider_secret("12-3456789")
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = None
        onboarding.campaign_use_case = "ACCOUNT_NOTIFICATION"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.privacy_policy_url = "https://app.example.com/compliance/acme/sms/privacy"
        onboarding.terms_and_conditions_url = "https://app.example.com/compliance/acme/sms/terms"
        onboarding.raw_submission_json = '{"has_embedded_links": false, "has_embedded_phone": false}'
        self._populate_onboarding_profile(onboarding)

        self._create_a2p_campaign(onboarding, self.messaging_profile, actor_user_id=7)

        service_context.us_app_to_person.assert_called_with("QEfailed123")
        delete_context.delete.assert_called_once()
        mock_sleep.assert_called_once_with(5)
        service_context.us_app_to_person._version.create.assert_called_once()
        self.assertEqual(onboarding.campaign_sid, "QEnew123")
        self.assertIn("last_deleted_campaign", onboarding.raw_status_json or "")

    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_create_a2p_campaign_blocks_auto_recreate_for_editable_failed_campaign(
        self,
        mock_build_subaccount_client,
    ) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client
        service_context = mock_client.messaging.v1.services.return_value
        existing_campaign = MagicMock(
            sid="QEfailed123",
            campaign_status="FAILED",
            us_app_to_person_usecase="ACCOUNT_NOTIFICATION",
            brand_registration_sid="BNbrand123",
            errors=[
                {
                    "registrationerrorcode": "30909",
                    "registrationerrordescription": "CTA could not be verified.",
                }
            ],
            failure_reason=None,
        )
        service_context.us_app_to_person.list.return_value = [existing_campaign]

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.business_name = "Acme"
        onboarding.business_type = "Limited Liability Corporation"
        onboarding.business_registration_identifier = "EIN"
        onboarding.business_registration_number_encrypted = self.encrypt_provider_secret("12-3456789")
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = None
        onboarding.campaign_use_case = "ACCOUNT_NOTIFICATION"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.privacy_policy_url = "https://app.example.com/compliance/acme/sms/privacy"
        onboarding.terms_and_conditions_url = "https://app.example.com/compliance/acme/sms/terms"
        onboarding.raw_submission_json = '{"has_embedded_links": false, "has_embedded_phone": false}'
        self._populate_onboarding_profile(onboarding)

        with self.assertRaisesRegex(self.ProviderProvisioningError, "campaign edit and retry flow"):
            self._create_a2p_campaign(onboarding, self.messaging_profile)

        service_context.us_app_to_person._version.create.assert_not_called()

    @patch("app.services.twilio_a2p_service.time.sleep")
    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_create_a2p_campaign_retries_after_twilio_association_conflict(
        self,
        mock_build_subaccount_client,
        mock_sleep,
    ) -> None:
        from twilio.base.exceptions import TwilioRestException

        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client
        service_context = mock_client.messaging.v1.services.return_value
        existing_campaign = MagicMock(
            sid="QEfailed123",
            campaign_status="FAILED",
            us_app_to_person_usecase="MIXED",
            brand_registration_sid="BNbrand123",
            errors=[],
            failure_reason=None,
        )
        service_context.us_app_to_person.list.side_effect = [[], [existing_campaign]]
        delete_context = service_context.us_app_to_person.return_value
        delete_context.delete = MagicMock(return_value=True)
        service_context.us_app_to_person._version.create.side_effect = [
            TwilioRestException(
                409,
                "/v1/Services/MGsub0001/Compliance/Usa2p",
                msg="Unable to create record: There is already a Campaign associated with this Messaging Service.",
            ),
            {"sid": "QEnew123"},
        ]

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.business_name = "Acme"
        onboarding.business_type = "Limited Liability Corporation"
        onboarding.business_registration_identifier = "EIN"
        onboarding.business_registration_number_encrypted = self.encrypt_provider_secret("12-3456789")
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = None
        onboarding.campaign_use_case = "ACCOUNT_NOTIFICATION"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.privacy_policy_url = "https://app.example.com/compliance/acme/sms/privacy"
        onboarding.terms_and_conditions_url = "https://app.example.com/compliance/acme/sms/terms"
        onboarding.raw_submission_json = '{"has_embedded_links": false, "has_embedded_phone": false}'
        self._populate_onboarding_profile(onboarding)

        self._create_a2p_campaign(onboarding, self.messaging_profile, actor_user_id=7)

        self.assertEqual(service_context.us_app_to_person._version.create.call_count, 2)
        service_context.us_app_to_person.assert_called_with("QEfailed123")
        delete_context.delete.assert_called_once()
        mock_sleep.assert_called_once_with(5)
        self.assertEqual(onboarding.campaign_sid, "QEnew123")

    @patch("app.services.twilio_a2p_service._build_subaccount_client_context")
    def test_sync_remote_status_prefers_campaign_status_and_errors(self, mock_build_subaccount_client_context) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client_context.return_value = (
            mock_client,
            {
                "twilio_read_account_sid": "ACsub0001",
                "twilio_subaccount_sid": "ACsub0001",
                "used_subaccount_auth_token": True,
            },
        )
        mock_client.messaging.v1.brand_registrations.return_value.fetch.return_value = MagicMock(
            status="APPROVED",
            failure_reason=None,
            tcr_id="TCR123",
            errors=None,
        )
        mock_client.messaging.v1.services.return_value.us_app_to_person.return_value.fetch.return_value = MagicMock(
            campaign_status="FAILED",
            campaign_id="CMcampaign123",
            errors=[
                {"registrationerrorcode": "30909", "registrationerrordescription": "CTA could not be verified."},
                {"registrationerrorcode": "30891", "registrationerrordescription": "Privacy policy URL is missing."},
            ],
            failure_reason=None,
        )

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = "QEcampaign123"
        self.db.session.commit()

        brand_status, campaign_status = self._sync_remote_status(onboarding, self.messaging_profile)

        self.assertEqual(brand_status, "approved")
        self.assertEqual(campaign_status, "failed")
        self.assertEqual(onboarding.failure_code, "30909")
        self.assertIn("CTA could not be verified", onboarding.raw_status_json or "")
        self.assertIn("CMcampaign123", onboarding.raw_status_json or "")

    @patch("app.services.twilio_a2p_service._inventory_subaccount_resources")
    @patch("app.services.twilio_a2p_service._build_subaccount_client_context")
    def test_sync_remote_status_classifies_stale_ids_as_missing_campaign(self, mock_build_subaccount_client_context, mock_inventory) -> None:
        from twilio.base.exceptions import TwilioRestException

        mock_client = MagicMock()
        mock_build_subaccount_client_context.return_value = (
            mock_client,
            {
                "twilio_read_account_sid": "ACsub0001",
                "twilio_subaccount_sid": "ACsub0001",
                "used_subaccount_auth_token": True,
            },
        )
        mock_client.messaging.v1.brand_registrations.return_value.fetch.side_effect = (
            TwilioRestException(404, "/v1/a2p/BrandRegistrations/BNstale123", msg="Brand not found", code=20404)
        )
        mock_inventory.return_value = {
            "subaccount_sid": "ACsub0001",
            "services": [
                {
                    "sid": "MGlive123",
                    "friendly_name": "SMS",
                    "status": "active",
                    "campaigns": [],
                    "campaign_count": 0,
                }
            ],
            "customer_profiles": [{"sid": "BUcustomer123", "friendly_name": "Acme", "status": "twilio-approved"}],
            "trust_products": [{"sid": "BUtrust123", "friendly_name": "Acme", "status": "twilio-approved"}],
            "brands": [{"sid": "BNlive123", "status": "approved", "identity_status": "verified", "tcr_id": "TCR123"}],
        }

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.customer_profile_sid = "BUstale123"
        onboarding.trust_product_sid = "BUtruststale123"
        onboarding.brand_registration_sid = "BNstale123"
        onboarding.campaign_sid = "QEstale123"
        self.db.session.commit()

        brand_status, campaign_status = self._sync_remote_status(onboarding, self.messaging_profile, actor_user_id=41)

        self.assertEqual(brand_status, "approved")
        self.assertIsNone(campaign_status)
        self.assertEqual(onboarding.onboarding_status, "needs_action")
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        recovery_state = json.loads(onboarding.raw_status_json)["recovery_state"]
        self.assertEqual(recovery_state["type"], "missing_campaign")
        self.assertEqual(recovery_state["recommended_action"], "create_campaign")
        self.assertEqual(recovery_state["selected"]["messaging_service_sid"], "MGlive123")

    @patch("app.services.twilio_a2p_service._create_a2p_campaign")
    @patch("app.services.twilio_a2p_service._upsert_a2p_resources")
    @patch("app.services.twilio_a2p_service._sync_remote_status", return_value=("approved", "in_progress"))
    def test_sync_a2p_onboarding_status_clears_stale_transient_error_without_mutation(
        self,
        _mock_sync_remote_status,
        mock_upsert,
        mock_create_campaign,
    ) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "error"
        onboarding.brand_status = "approved"
        onboarding.campaign_status = "in_progress"
        onboarding.last_error = "Failed to resolve 'messaging.twilio.com'"
        self.messaging_profile.provider_status = "error"
        self.messaging_profile.last_provision_error = "Failed to resolve 'messaging.twilio.com'"
        self.db.session.commit()

        result = self.sync_a2p_onboarding_status(self.organization.id, actor_user_id=81)

        self.assertEqual(result.onboarding_status, "pending")
        self.assertEqual(result.brand_status, "approved")
        self.assertEqual(result.campaign_status, "in_progress")
        self.assertIsNone(result.last_error)
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertIsNone(self.messaging_profile.last_provision_error)
        mock_upsert.assert_not_called()
        mock_create_campaign.assert_not_called()

    def test_sync_a2p_onboarding_status_marks_missing_subaccount_auth_as_needs_action(self) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.brand_status = "approved"
        onboarding.campaign_status = "in_progress"
        self.messaging_profile.twilio_auth_token_encrypted = None
        self.db.session.commit()

        result = self.sync_a2p_onboarding_status(self.organization.id, actor_user_id=82)

        self.assertEqual(result.onboarding_status, "needs_action")
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertIn("Stored Twilio subaccount auth token is required", result.last_error or "")

    @patch("app.services.twilio_a2p_service._complete_number_setup")
    @patch("app.services.twilio_a2p_service._create_a2p_campaign")
    @patch("app.services.twilio_a2p_service._sync_remote_status", return_value=("pending-review", None))
    @patch("app.services.twilio_a2p_service._upsert_a2p_resources")
    def test_process_a2p_onboarding_keeps_pending_brand_without_campaign_creation(
        self,
        mock_upsert,
        _mock_sync,
        mock_create_campaign,
        mock_complete_number_setup,
    ) -> None:
        def seed_resources(onboarding, _profile):
            onboarding.customer_profile_sid = "BUcustomer123"
            onboarding.trust_product_sid = "BUtrust123"
            onboarding.brand_registration_sid = "BNbrand123"

        mock_upsert.side_effect = seed_resources

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.onboarding_status = "queued"
        self.messaging_profile.provider_status = "error"
        self.messaging_profile.last_provision_error = "old failure"
        self.db.session.commit()

        result = self.process_a2p_onboarding(self.organization.id, actor_user_id=7)

        self.db.session.refresh(result)
        self.db.session.refresh(self.messaging_profile)
        self.assertEqual(result.onboarding_status, "pending")
        self.assertEqual(result.brand_status, "pending-review")
        self.assertEqual(result.campaign_status, "pending")
        self.assertEqual(result.brand_registration_sid, "BNbrand123")
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertIsNone(self.messaging_profile.last_provision_error)
        mock_create_campaign.assert_not_called()
        mock_complete_number_setup.assert_not_called()

    @patch("app.services.twilio_a2p_service._complete_number_setup")
    @patch("app.services.twilio_a2p_service._create_a2p_campaign")
    @patch("app.services.twilio_a2p_service._inventory_subaccount_resources")
    @patch("app.services.twilio_a2p_service._sync_remote_status", return_value=("approved", None))
    @patch("app.services.twilio_a2p_service._upsert_a2p_resources")
    def test_process_a2p_onboarding_requires_explicit_campaign_creation_when_brand_ready(
        self,
        mock_upsert,
        _mock_sync,
        mock_inventory,
        mock_create_campaign,
        mock_complete_number_setup,
    ) -> None:
        def seed_resources(onboarding, _profile):
            onboarding.customer_profile_sid = "BUcustomer123"
            onboarding.trust_product_sid = "BUtrust123"
            onboarding.brand_registration_sid = "BNbrand123"

        mock_upsert.side_effect = seed_resources
        mock_inventory.return_value = {
            "subaccount_sid": "ACsub0001",
            "services": [
                {
                    "sid": "MGsub0001",
                    "friendly_name": "SMS",
                    "status": "active",
                    "campaigns": [],
                    "campaign_count": 0,
                }
            ],
            "customer_profiles": [{"sid": "BUcustomer123", "friendly_name": "Acme", "status": "twilio-approved"}],
            "trust_products": [{"sid": "BUtrust123", "friendly_name": "Acme", "status": "twilio-approved"}],
            "brands": [{"sid": "BNbrand123", "status": "approved", "identity_status": "verified", "tcr_id": "TCR123"}],
        }

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.onboarding_status = "queued"
        self.db.session.commit()

        result = self.process_a2p_onboarding(self.organization.id, actor_user_id=7)

        self.assertEqual(result.onboarding_status, "needs_action")
        self.assertEqual(result.brand_status, "approved")
        self.assertIsNone(result.campaign_sid)
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertEqual(json.loads(result.raw_status_json)["recovery_state"]["recommended_action"], "create_campaign")
        mock_create_campaign.assert_not_called()
        mock_complete_number_setup.assert_not_called()

    @patch("app.services.twilio_a2p_service._complete_number_setup")
    @patch("app.services.twilio_a2p_service._create_a2p_campaign")
    @patch("app.services.twilio_a2p_service._sync_remote_status")
    @patch("app.services.twilio_a2p_service._upsert_a2p_resources")
    def test_process_a2p_onboarding_marks_profile_active_when_brand_and_campaign_are_ready(
        self,
        mock_upsert,
        mock_sync,
        mock_create_campaign,
        mock_complete_number_setup,
    ) -> None:
        from app.models import utc_now

        def seed_resources(onboarding, _profile):
            onboarding.customer_profile_sid = "BUcustomer123"
            onboarding.trust_product_sid = "BUtrust123"
            onboarding.brand_registration_sid = "BNbrand123"
            onboarding.campaign_sid = "QEcampaign123"

        mock_upsert.side_effect = seed_resources
        mock_sync.return_value = ("approved", "approved")
        def complete_number_setup(_onboarding, profile, _actor_user_id):
            profile.from_number = "+15550001111"
            profile.phone_number_sid = "PNready123"
            profile.sender_review_status = "approved"
            profile.consent_acknowledged_at = utc_now()
            profile.set_sender_finalization_status("active")

        mock_complete_number_setup.side_effect = complete_number_setup

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.onboarding_status = "queued"
        self.db.session.commit()

        result = self.process_a2p_onboarding(self.organization.id, actor_user_id=7)

        self.assertEqual(result.onboarding_status, "approved")
        self.assertEqual(result.brand_status, "approved")
        self.assertEqual(result.campaign_status, "approved")
        self.assertIsNotNone(result.approved_at)
        self.assertEqual(self.messaging_profile.provider_status, "active")
        mock_upsert.assert_called_once()
        mock_create_campaign.assert_not_called()
        mock_complete_number_setup.assert_called_once()

    @patch("app.services.twilio_a2p_service._sync_remote_status", side_effect=RuntimeError("Failed to resolve 'messaging.twilio.com'"))
    @patch("app.services.twilio_a2p_service._upsert_a2p_resources")
    def test_process_a2p_onboarding_marks_transient_provider_connectivity_without_generic_error(
        self,
        _mock_upsert,
        _mock_sync,
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
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.onboarding_status = "pending"
        onboarding.submitted_at = self.organization.created_at
        self.db.session.commit()

        result = self.process_a2p_onboarding(self.organization.id, actor_user_id=7)

        self.assertEqual(result.onboarding_status, "pending")
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        recovery_state = json.loads(result.raw_status_json)["recovery_state"]
        self.assertEqual(recovery_state["type"], "transient_connectivity")
        self.assertEqual(recovery_state["recommended_action"], "refresh")

    @patch("app.services.twilio_a2p_service._inventory_subaccount_resources")
    def test_reconcile_a2p_twilio_state_preserves_subaccount_and_form_data(self, mock_inventory) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "needs_action"
        onboarding.business_name = "Acme"
        onboarding.legal_business_name = "Acme"
        onboarding.public_brand_name = "Acme"
        onboarding.customer_profile_sid = "BUstale123"
        onboarding.trust_product_sid = "BUtruststale123"
        onboarding.brand_registration_sid = "BNstale123"
        onboarding.campaign_sid = "QEstale123"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        self.db.session.commit()

        mock_inventory.return_value = {
            "subaccount_sid": "ACsub0001",
            "services": [
                {
                    "sid": "MGlive123",
                    "friendly_name": "SMS",
                    "status": "active",
                    "campaigns": [],
                    "campaign_count": 0,
                }
            ],
            "customer_profiles": [{"sid": "BUcustomer123", "friendly_name": "Acme", "status": "twilio-approved"}],
            "trust_products": [{"sid": "BUtrust123", "friendly_name": "Acme", "status": "twilio-approved"}],
            "brands": [{"sid": "BNlive123", "status": "approved", "identity_status": "verified", "tcr_id": "TCR123"}],
        }

        result = self.reconcile_a2p_twilio_state(
            self.organization.id,
            messaging_service_sid="MGlive123",
            customer_profile_sid="BUcustomer123",
            trust_product_sid="BUtrust123",
            brand_registration_sid="BNlive123",
            actor_user_id=9,
        )

        self.assertEqual(self.messaging_profile.twilio_subaccount_sid, "ACsub0001")
        self.assertEqual(self.messaging_profile.messaging_service_sid, "MGlive123")
        self.assertEqual(result.customer_profile_sid, "BUcustomer123")
        self.assertEqual(result.trust_product_sid, "BUtrust123")
        self.assertEqual(result.brand_registration_sid, "BNlive123")
        self.assertEqual(result.business_name, "Acme")
        self.assertEqual(json.loads(result.raw_status_json)["recovery_state"]["type"], "missing_campaign")

    @patch("app.services.twilio_a2p_service._apply_status_snapshot")
    @patch("app.services.twilio_a2p_service._sync_remote_status", return_value=("approved", "submitted"))
    @patch("app.services.twilio_a2p_service._create_a2p_campaign")
    def test_create_missing_a2p_campaign_requires_explicit_service_call(
        self,
        mock_create_campaign,
        _mock_sync,
        mock_apply_status_snapshot,
    ) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "needs_action"
        onboarding.brand_status = "approved"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = None
        self.db.session.commit()

        result = self.create_missing_a2p_campaign(self.organization.id, actor_user_id=12)

        self.assertIs(result, onboarding)
        mock_create_campaign.assert_called_once_with(onboarding, self.messaging_profile, actor_user_id=12)
        mock_apply_status_snapshot.assert_called_once()

    @patch(
        "app.services.twilio_a2p_service._upsert_a2p_resources",
        side_effect=RuntimeError(
            "HTTP 400 error: Unable to create record: Secondary Customer Profile for direct_customer can only be created through Twilio console."
        ),
    )
    def test_process_a2p_onboarding_maps_direct_customer_console_only_error(self, _mock_upsert) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.registration_path = "standard"
        onboarding.number_strategy = "auto_buy"
        onboarding.business_name = "Acme"
        onboarding.business_type = "LLC"
        onboarding.email = "ops@acme.test"
        onboarding.first_name = "Jane"
        onboarding.last_name = "Doe"
        onboarding.campaign_description = "Community updates"
        onboarding.message_flow = "Users opt in."
        onboarding.message_samples_json = '["Sample 1", "Sample 2"]'
        onboarding.onboarding_status = "queued"
        self.db.session.commit()

        with self.assertRaisesRegex(self.ProviderProvisioningError, "ISV Reseller or Partner"):
            self.process_a2p_onboarding(self.organization.id, actor_user_id=7)

        self.db.session.refresh(onboarding)
        self.assertEqual(onboarding.onboarding_status, "error")
        self.assertIn("ISV Reseller or Partner", onboarding.last_error or "")
        self.assertEqual(self.messaging_profile.provider_status, "error")

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_ingest_a2p_event_stream_payload_updates_status_and_dedupes(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = "QEcampaign123"
        self.db.session.commit()

        event = {
            "id": "evt-brand-1",
            "type": "com.twilio.messaging.a2p.brand-registration.brand-verified",
            "data": {
                "brandsid": "BNbrand123",
                "brandstatus": "VERIFIED",
                "updateddate": 200,
            },
        }

        summary = self.ingest_a2p_event_stream_payload(event)
        duplicate_summary = self.ingest_a2p_event_stream_payload(event)
        stale_summary = self.ingest_a2p_event_stream_payload(
            {
                "id": "evt-brand-2",
                "type": "com.twilio.messaging.a2p.brand-registration.brand-unverified",
                "data": {
                    "brandsid": "BNbrand123",
                    "brandstatus": "UNVERIFIED",
                    "updateddate": 100,
                },
            }
        )

        self.db.session.commit()
        self.db.session.refresh(onboarding)
        self.assertEqual(summary["events_applied"], 1)
        self.assertEqual(duplicate_summary["events_duplicate"], 1)
        self.assertEqual(stale_summary["events_out_of_order"], 1)
        self.assertEqual(onboarding.brand_status, "verified")
        queue.enqueue.assert_not_called()

    @patch("app.services.twilio_a2p_service.get_queue")
    def test_ingest_a2p_event_stream_payload_falls_back_to_subaccount_and_records_observed_ids(self, mock_get_queue) -> None:
        queue = MagicMock()
        mock_get_queue.return_value = queue

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.raw_status_json = json.dumps({"brand_tcr_id": "TCR123"})
        self.messaging_profile.messaging_service_sid = "MGstale123"
        self.db.session.commit()

        summary = self.ingest_a2p_event_stream_payload(
            {
                "id": "evt-campaign-fallback",
                "type": "com.twilio.messaging.a2p.campaign-registration.campaign-approved",
                "data": {
                    "accountsid": "ACsub0001",
                    "messageservicesid": "MGlive123",
                    "campaignsid": "QElive123",
                    "campaignregistrationstatus": "APPROVED",
                    "updateddate": 400,
                },
            }
        )

        self.assertEqual(summary["events_applied"], 1)
        recovery_state = json.loads(onboarding.raw_status_json)["recovery_state"]
        self.assertEqual(recovery_state["observed_ids"]["messaging_service_sid"], "MGlive123")
        self.assertEqual(recovery_state["observed_ids"]["campaign_sid"], "QElive123")
        queue.enqueue.assert_not_called()

    def test_ingest_a2p_event_stream_payload_marks_rejected_on_campaign_failure(self) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.brand_registration_sid = "BNbrand123"
        onboarding.campaign_sid = "QEcampaign123"
        self.db.session.commit()

        summary = self.ingest_a2p_event_stream_payload(
            {
                "id": "evt-campaign-1",
                "type": "com.twilio.messaging.a2p.campaign-registration.campaign-failure",
                "data": {
                    "campaignsid": "QEcampaign123",
                    "campaignregistrationstatus": "FAILED",
                    "campaignregistrationerrors": [
                        {
                            "registrationerrorcode": "3001",
                            "registrationerrordescription": "Campaign description is too vague.",
                        }
                    ],
                    "updateddate": 300,
                },
            }
        )

        self.db.session.commit()
        self.db.session.refresh(onboarding)
        self.assertEqual(summary["events_applied"], 1)
        self.assertEqual(onboarding.onboarding_status, "rejected")
        self.assertIn("Campaign description is too vague.", onboarding.last_error or "")
        self.assertEqual(self.messaging_profile.provider_status, "error")

    @patch("app.routes.validate_inbound_signature_detailed")
    @patch("app.routes.ingest_a2p_event_stream_payload", return_value={"events_seen": 1, "events_applied": 1, "events_ignored": 0, "events_duplicate": 0, "events_out_of_order": 0})
    def test_a2p_event_stream_webhook_validates_signed_json_body(self, mock_ingest, mock_validate) -> None:
        self.app.config["TWILIO_A2P_EVENT_STREAMS_ENABLED"] = True
        self.app.config["TWILIO_A2P_EVENT_STREAM_AUTH_TOKEN"] = "secret-token"
        mock_validate.side_effect = [
            MagicMock(is_valid=True, reason="valid"),
            MagicMock(is_valid=False, reason="invalid_signature"),
        ]

        allowed = self.client.post(
            f"/webhooks/twilio/a2p-events?organization_id={self.organization.id}",
            data='{"id":"evt-1","type":"com.twilio.messaging.a2p.brand-registration.brand-verified","data":{}}',
            headers={
                "Content-Type": "application/json",
                "X-Twilio-Signature": "signature-1",
            },
        )
        forbidden = self.client.post(
            f"/webhooks/twilio/a2p-events?organization_id={self.organization.id}",
            data='{"id":"evt-2","type":"com.twilio.messaging.a2p.brand-registration.brand-verified","data":{}}',
            headers={
                "Content-Type": "application/json",
                "X-Twilio-Signature": "signature-2",
            },
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(forbidden.status_code, 403)
        mock_ingest.assert_called_once()

    def test_describe_a2p_onboarding_surfaces_reviewing_wait_state(self) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "pending"
        onboarding.brand_status = "pending-review"
        onboarding.campaign_status = "submitted"

        view = self.describe_a2p_onboarding(onboarding, self.messaging_profile)

        self.assertEqual(view["stage"], "reviewing")
        self.assertTrue(view["show_wait_state"])
        self.assertIn("carrier", view["summary"].lower())

    def test_describe_a2p_onboarding_prefers_submitted_stage_for_queued_resubmission(self) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "queued"
        onboarding.raw_status_json = json.dumps(
            {
                "campaign_failure_reason": "CTA could not be verified.",
                "campaign_failure_code": "30909",
            }
        )

        view = self.describe_a2p_onboarding(onboarding, self.messaging_profile)

        self.assertEqual(view["stage"], "submitted")
        self.assertIn("queued", view["summary"].lower())

    def test_describe_a2p_onboarding_surfaces_provider_drift_recovery_actions(self) -> None:
        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "needs_action"
        onboarding.brand_status = "approved"
        onboarding.raw_status_json = json.dumps(
            {
                "recovery_state": {
                    "type": "provider_drift",
                    "recommended_action": "reconcile",
                    "summary": "Twilio still has approved resources, but the app is bound to stale identifiers.",
                    "stored": {"messaging_service_sid": "MGstale123"},
                    "live": {"services": [{"sid": "MGlive123"}]},
                    "selected": {"messaging_service_sid": "MGlive123"},
                    "missing": {"messaging_service_sid": True},
                    "only_missing_campaign": False,
                    "observed_ids": {},
                }
            }
        )

        view = self.describe_a2p_onboarding(onboarding, self.messaging_profile)

        self.assertEqual(view["stage"], "needs_action")
        self.assertTrue(view["can_reconcile"])
        self.assertFalse(view["can_create_campaign"])
        self.assertIn("reconcile", view["next_step"].lower())

    def test_describe_a2p_onboarding_surfaces_customer_managed_failure(self) -> None:
        self.messaging_profile.provider_mode = "customer_managed"
        self.messaging_profile.provider_status = "error"
        self.messaging_profile.twilio_account_sid = "ACcust0001"
        self.messaging_profile.messaging_service_sid = "MGcust0001"
        self.messaging_profile.phone_number_sid = "PNcust0001"
        self.messaging_profile.from_number = "+15550001111"

        onboarding = self.ensure_a2p_onboarding(self.organization)
        onboarding.onboarding_status = "needs_action"
        onboarding.brand_status = "verified"
        onboarding.campaign_status = "failed"
        onboarding.failure_code = "30909"
        onboarding.last_error = "CTA could not be verified."
        onboarding.raw_status_json = json.dumps(
            {
                "external_managed": True,
                "provider_mode": "customer_managed",
                "campaign_status": "failed",
                "campaign_failure_reason": "CTA could not be verified.",
                "campaign_failure_code": "30909",
                "console_campaign_id": "CMconsole123",
                "brand_status": "verified",
                "messaging_service_sid": "MGcust0001",
                "phone_number_sid": "PNcust0001",
            }
        )

        view = self.describe_a2p_onboarding(onboarding, self.messaging_profile)

        self.assertEqual(view["stage"], "needs_action")
        self.assertEqual(view["failure_code"], "30909")
        self.assertIn("CTA", view["summary"])
        self.assertEqual(view["console_campaign_id"], "CMconsole123")


if __name__ == "__main__":
    unittest.main()

import importlib
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
        from app.services.provider_secret_service import encrypt_provider_secret
        from app.services.twilio_a2p_service import (
            _create_a2p_campaign,
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
        self.encrypt_provider_secret = encrypt_provider_secret
        self._create_a2p_campaign = _create_a2p_campaign
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

    def _valid_submission_payload(self, **overrides):
        payload = {
            "registration_path": "standard",
            "number_strategy": "auto_buy",
            "business_name": "Acme",
            "business_type": "LLC",
            "business_industry": "TECHNOLOGY",
            "business_regions": ["USA_AND_CANADA"],
            "business_registration_identifier": "EIN",
            "business_registration_number": "12-3456789",
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

    def test_submit_a2p_onboarding_requires_two_message_samples_for_mixed_use_case(self) -> None:
        with self.assertRaisesRegex(self.ProviderProvisioningError, "Mixed-use campaigns require at least two message samples."):
            self.submit_a2p_onboarding(
                self.organization.id,
                self._valid_submission_payload(
                    message_samples="Only one sample",
                    campaign_use_case="MIXED",
                ),
                actor_user_id=14,
            )

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
        mock_client.trusthub.v1.addresses.create.return_value.sid = "ADaddress123"
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
        mock_client.messaging.v1.services.return_value.us_app_to_person.create.return_value.sid = "QEcampaign123"

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
        mock_client.messaging.v1.services.return_value.us_app_to_person.create.assert_not_called()

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

        with self.assertRaisesRegex(self.ProviderProvisioningError, "Mixed-use campaigns require at least two message samples."):
            self._create_a2p_campaign(onboarding, self.messaging_profile)

        mock_client.messaging.v1.services.return_value.us_app_to_person.create.assert_not_called()

    @patch("app.services.twilio_a2p_service._build_subaccount_client")
    def test_create_a2p_campaign_persists_campaign_sid(self, mock_build_subaccount_client) -> None:
        mock_client = MagicMock()
        mock_build_subaccount_client.return_value = mock_client
        mock_client.messaging.v1.services.return_value.us_app_to_person.create.return_value.sid = "QEcampaign123"

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
        onboarding.raw_submission_json = '{"has_embedded_links": true, "has_embedded_phone": false}'
        self._populate_onboarding_profile(onboarding)

        self._create_a2p_campaign(onboarding, self.messaging_profile)

        mock_client.messaging.v1.services.return_value.us_app_to_person.create.assert_called_once()
        self.assertEqual(onboarding.campaign_sid, "QEcampaign123")

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
        self.assertIsNone(result.campaign_status)
        self.assertEqual(result.brand_registration_sid, "BNbrand123")
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertIsNone(self.messaging_profile.last_provision_error)
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
        def seed_resources(onboarding, _profile):
            onboarding.customer_profile_sid = "BUcustomer123"
            onboarding.trust_product_sid = "BUtrust123"
            onboarding.brand_registration_sid = "BNbrand123"

        def seed_campaign(onboarding, _profile):
            onboarding.campaign_sid = "QEcampaign123"

        mock_upsert.side_effect = seed_resources
        mock_create_campaign.side_effect = seed_campaign
        mock_sync.side_effect = [("approved", None), ("approved", "approved")]

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
        mock_create_campaign.assert_called_once()
        mock_complete_number_setup.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()

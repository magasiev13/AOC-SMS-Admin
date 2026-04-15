import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch


class TestTwilioInboundSignatureValidation(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "twilio-service.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "TWILIO_AUTH_TOKEN": "test-auth-token",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()

    def tearDown(self) -> None:
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_missing_auth_token_returns_reason(self) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        self.app.config["TWILIO_AUTH_TOKEN"] = None
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "missing_auth_token")

    def test_missing_signature_returns_reason(self) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            None,
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "missing_signature")

    @patch("app.services.twilio_service.RequestValidator")
    def test_validator_exception_returns_reason(self, mock_validator) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        mock_validator.return_value.validate.side_effect = RuntimeError("validator exploded")
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "validator_exception")

    @patch("app.services.twilio_service.RequestValidator")
    def test_invalid_signature_returns_reason(self, mock_validator) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        mock_validator.return_value.validate.return_value = False
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.reason, "invalid_signature")

    @patch("app.services.twilio_service.RequestValidator")
    def test_validate_inbound_signature_accepts_raw_json_body(self, mock_validator) -> None:
        from app.services.twilio_service import validate_inbound_signature_detailed

        mock_validator.return_value.validate.return_value = True
        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/a2p-events?organization_id=3&bodySHA256=test",
            '{"id":"evt-1"}',
            "signature",
        )

        self.assertTrue(result.is_valid)
        mock_validator.return_value.validate.assert_called_once_with(
            "https://example.com/webhooks/twilio/a2p-events?organization_id=3&bodySHA256=test",
            '{"id":"evt-1"}',
            "signature",
        )


if __name__ == "__main__":
    unittest.main()


class TestTwilioProviderLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "provider-lifecycle.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SAAS_MODE": "1",
                "TWILIO_ACCOUNT_SID": "ACmaster123",
                "TWILIO_AUTH_TOKEN": "master-token",
                "TWILIO_CREDENTIAL_ENCRYPTION_KEY": "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import (
            MessagingUsageRecord,
            Organization,
            OrganizationMessagingProfile,
            OrganizationProviderAuditLog,
            OrganizationSubscription,
            OrganizationUsageBillingPeriod,
        )

        self.db = db
        self.Organization = Organization
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationProviderAuditLog = OrganizationProviderAuditLog
        self.OrganizationSubscription = OrganizationSubscription
        self.MessagingUsageRecord = MessagingUsageRecord
        self.OrganizationUsageBillingPeriod = OrganizationUsageBillingPeriod

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            SAAS_BASE_URL="https://beta.example.com",
            BILLING_INCLUDED_OUTBOUND_SEGMENTS=1,
            BILLING_OUTBOUND_SEGMENT_RATE_USD="0.0300",
        )
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        from app.migrations.runner import run_pending_migrations

        run_pending_migrations(self.db.engine, self.app.logger)

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def _create_org_with_profile(self, *, subscription_status="incomplete", **profile_overrides):
        organization = self.Organization(name="Acme", slug="acme", status="active")
        subscription = self.OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status=subscription_status,
        )
        profile_kwargs = {
            "organization": organization,
            "provider_mode": "platform_managed",
            "status": "pending",
            "provider_status": "pending",
        }
        profile_kwargs.update(profile_overrides)
        profile = self.OrganizationMessagingProfile(**profile_kwargs)
        self.db.session.add_all([organization, subscription, profile])
        self.db.session.commit()
        return organization, profile

    @patch("app.services.twilio_service.RequestValidator")
    def test_validate_inbound_signature_prefers_org_specific_auth_token(self, mock_validator) -> None:
        from app.services.provider_secret_service import encrypt_provider_secret
        from app.services.twilio_service import validate_inbound_signature_detailed

        _, profile = self._create_org_with_profile(
            twilio_auth_token_encrypted=encrypt_provider_secret("subaccount-token"),
        )
        mock_validator.return_value.validate.return_value = True

        result = validate_inbound_signature_detailed(
            "https://example.com/webhooks/twilio/inbound",
            {"From": "+15550000001"},
            "signature",
            messaging_profile=profile,
        )

        self.assertTrue(result.is_valid)
        mock_validator.assert_called_once_with("subaccount-token")

    @patch("app.services.twilio_service._build_customer_managed_client")
    def test_save_customer_managed_profile_validates_external_account_and_sender(self, mock_build_customer_client) -> None:
        from app.services.provider_secret_service import decrypt_provider_secret
        from app.services.twilio_service import save_customer_managed_profile

        organization, _ = self._create_org_with_profile()
        customer_client = mock_build_customer_client.return_value
        customer_client.incoming_phone_numbers.list.return_value = [
            SimpleNamespace(sid="PNcust0001", phone_number="+15550001111")
        ]
        customer_client.messaging.v1.services.return_value.us_app_to_person.list.return_value = [
            SimpleNamespace(
                sid="QEcust0001",
                status="VERIFIED",
                brand_registration_sid="BNcust0001",
            )
        ]
        customer_client.messaging.v1.brand_registrations.return_value.fetch.return_value = SimpleNamespace(
            status="VERIFIED"
        )

        profile, validation = save_customer_managed_profile(
            organization.id,
            twilio_account_sid="ACcust0001",
            twilio_auth_token="customer-token",
            from_number="+15550001111",
            messaging_service_sid="MGcust0001",
            business_type="Nonprofit",
            use_case="Announcements",
            actor_user_id=42,
        )

        self.assertEqual(profile.provider_mode, "customer_managed")
        self.assertEqual(profile.provider_status, "active")
        self.assertEqual(profile.twilio_account_sid, "ACCUST0001")
        self.assertIsNone(profile.twilio_subaccount_sid)
        self.assertEqual(profile.phone_number_sid, "PNcust0001")
        self.assertEqual(profile.messaging_service_sid, "MGCUST0001")
        self.assertEqual(profile.from_number, "+15550001111")
        self.assertEqual(profile.sender_review_status, "approved")
        self.assertIsNotNone(profile.consent_acknowledged_at)
        self.assertEqual(decrypt_provider_secret(profile.twilio_auth_token_encrypted), "customer-token")
        self.assertEqual(validation.messaging_service_sid, "MGCUST0001")
        self.assertEqual(validation.campaign_status, "verified")
        customer_client.messaging.v1.services.return_value.update.assert_called_once_with(
            use_inbound_webhook_on_number=True
        )
        customer_client.incoming_phone_numbers.assert_called_once_with("PNcust0001")
        customer_client.incoming_phone_numbers.return_value.update.assert_called_once_with(
            sms_url="https://beta.example.com/webhooks/twilio/inbound",
            sms_method="POST",
        )

    @patch("app.services.twilio_service._build_customer_managed_client")
    def test_save_customer_managed_profile_fails_when_sender_number_missing_from_external_account(
        self,
        mock_build_customer_client,
    ) -> None:
        from app.services.twilio_service import ProviderProvisioningError, save_customer_managed_profile

        organization, _ = self._create_org_with_profile()
        mock_build_customer_client.return_value.incoming_phone_numbers.list.return_value = []

        with self.assertRaises(ProviderProvisioningError) as ctx:
            save_customer_managed_profile(
                organization.id,
                twilio_account_sid="ACcust0001",
                twilio_auth_token="customer-token",
                from_number="+15550001111",
                messaging_service_sid="MGcust0001",
            )

        self.assertIn("does not contain the sender number", str(ctx.exception))
        self.db.session.expire_all()
        profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization.id).one()
        self.assertEqual(profile.provider_mode, "customer_managed")
        self.assertEqual(profile.provider_status, "error")

    @patch("app.services.twilio_service._build_customer_managed_client")
    def test_twilio_service_uses_customer_managed_account_credentials(self, mock_build_customer_client) -> None:
        from app.services.provider_secret_service import encrypt_provider_secret
        from app.services.twilio_service import TwilioService

        _, profile = self._create_org_with_profile(
            provider_mode="customer_managed",
            twilio_account_sid="ACcust0001",
            twilio_auth_token_encrypted=encrypt_provider_secret("customer-token"),
            messaging_service_sid="MGcust0001",
            from_number="+15550001111",
            phone_number_sid="PNcust0001",
            provider_status="active",
            status="active",
        )

        service = TwilioService(organization_id=profile.organization_id)

        self.assertIs(service.client, mock_build_customer_client.return_value)
        self.assertEqual(service.account_sid, "ACCUST0001")
        self.assertEqual(service.auth_token, "customer-token")

    @patch("app.services.twilio_service._build_customer_managed_client")
    def test_save_customer_managed_profile_persists_rejected_external_state(self, mock_build_customer_client) -> None:
        from app.services.twilio_service import save_customer_managed_profile

        organization, _ = self._create_org_with_profile()
        customer_client = mock_build_customer_client.return_value
        customer_client.incoming_phone_numbers.list.return_value = [
            SimpleNamespace(sid="PNcust0001", phone_number="+15550001111")
        ]
        customer_client.messaging.v1.services.return_value.us_app_to_person.list.return_value = [
            SimpleNamespace(
                sid="QEcust0001",
                campaign_status="FAILED",
                errors=[{"error_code": "30909", "description": "CTA could not be verified."}],
                brand_registration_sid="BNcust0001",
            )
        ]
        customer_client.messaging.v1.brand_registrations.return_value.fetch.return_value = SimpleNamespace(
            status="VERIFIED"
        )

        profile, validation = save_customer_managed_profile(
            organization.id,
            twilio_account_sid="ACcust0001",
            twilio_auth_token="customer-token",
            from_number="+15550001111",
            messaging_service_sid="MGcust0001",
        )

        self.assertEqual(profile.provider_status, "error")
        self.assertEqual(profile.sender_review_status, "rejected")
        self.assertEqual(profile.messaging_service_sid, "MGCUST0001")
        self.assertEqual(validation.campaign_status, "failed")
        self.assertEqual(validation.campaign_failure_code, "30909")
        self.assertIn("CTA", validation.campaign_failure_reason or "")

    @patch("app.services.twilio_service._configure_service_webhooks")
    @patch("app.services.twilio_service._build_subaccount_client")
    @patch("app.services.twilio_service._master_client")
    def test_provision_org_encrypts_subaccount_token_and_creates_service(self, mock_master_client, mock_build_subaccount_client, mock_configure_service_webhooks) -> None:
        from app.services.provider_secret_service import decrypt_provider_secret
        from app.services.twilio_service import provision_org

        organization, _ = self._create_org_with_profile()
        master_client = mock_master_client.return_value
        master_client.api.v2010.accounts.create.return_value = SimpleNamespace(
            sid="ACsub0001",
            auth_token="subaccount-token",
        )
        subaccount_client = mock_build_subaccount_client.return_value
        subaccount_client.messaging.v1.services.create.return_value = SimpleNamespace(sid="MGsub0001")

        profile = provision_org(organization.id, actor_user_id=99)

        self.assertEqual(profile.twilio_subaccount_sid, "ACsub0001")
        self.assertEqual(profile.messaging_service_sid, "MGsub0001")
        self.assertEqual(decrypt_provider_secret(profile.twilio_auth_token_encrypted), "subaccount-token")
        self.assertEqual(profile.provider_status, "pending")
        mock_configure_service_webhooks.assert_called_once()
        self.assertEqual(self.OrganizationProviderAuditLog.query.filter_by(organization_id=organization.id).count(), 2)

    @patch("app.services.twilio_service._configure_service_webhooks")
    @patch("app.services.twilio_service._build_subaccount_client")
    @patch("app.services.twilio_service._master_client")
    def test_provision_org_failure_preserves_remote_identifiers_for_retry(
        self,
        mock_master_client,
        mock_build_subaccount_client,
        mock_configure_service_webhooks,
    ) -> None:
        from app.services.provider_secret_service import decrypt_provider_secret
        from app.services.twilio_service import ProviderProvisioningError, provision_org

        organization, _ = self._create_org_with_profile()
        master_client = mock_master_client.return_value
        master_client.api.v2010.accounts.create.return_value = SimpleNamespace(
            sid="ACsub0002",
            auth_token="subaccount-token-2",
        )
        subaccount_client = mock_build_subaccount_client.return_value
        subaccount_client.messaging.v1.services.create.return_value = SimpleNamespace(sid="MGsub0002")
        mock_configure_service_webhooks.side_effect = RuntimeError("webhook bind failed")

        with self.assertRaises(ProviderProvisioningError) as ctx:
            provision_org(organization.id, actor_user_id=99)

        self.assertIn("webhook bind failed", str(ctx.exception))
        profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization.id).one()
        self.assertEqual(profile.provider_status, "error")
        self.assertEqual(profile.twilio_subaccount_sid, "ACsub0002")
        self.assertEqual(profile.messaging_service_sid, "MGsub0002")
        self.assertEqual(decrypt_provider_secret(profile.twilio_auth_token_encrypted), "subaccount-token-2")
        self.assertIn("webhook bind failed", profile.last_provision_error)

    @patch("app.services.twilio_service._build_subaccount_client")
    def test_sync_sender_assignment_attaches_phone_number_and_configures_inbound_webhook(self, mock_build_subaccount_client) -> None:
        from app.models import utc_now
        from app.services.twilio_service import sync_sender_assignment

        organization, _ = self._create_org_with_profile(
            twilio_subaccount_sid="ACsub0001",
            messaging_service_sid="MGsub0001",
            from_number="+15550001111",
            phone_number_sid="PN0001",
            sender_review_status="approved",
            consent_acknowledged_at=utc_now(),
        )
        subaccount_client = mock_build_subaccount_client.return_value
        service_context = subaccount_client.messaging.v1.services.return_value
        service_context.phone_numbers.list.return_value = []

        profile = sync_sender_assignment(organization.id, actor_user_id=99)

        service_context.phone_numbers.create.assert_called_once_with(phone_number_sid="PN0001")
        service_context.update.assert_called_once_with(
            inbound_request_url="https://beta.example.com/webhooks/twilio/inbound",
            inbound_method="POST",
            use_inbound_webhook_on_number=False,
        )
        self.assertEqual(profile.provider_status, "active")
        self.assertEqual(profile.inbound_identity, "+15550001111")

    @patch("app.services.twilio_service._build_subaccount_client")
    def test_sync_sender_assignment_surfaces_phone_sid_subaccount_mismatch(self, mock_build_subaccount_client) -> None:
        from twilio.base.exceptions import TwilioRestException

        from app.models import utc_now
        from app.services.twilio_service import ProviderProvisioningError, sync_sender_assignment

        organization, _ = self._create_org_with_profile(
            twilio_subaccount_sid="ACsub0001",
            messaging_service_sid="MGsub0001",
            from_number="+15550001111",
            phone_number_sid="PN0001",
            sender_review_status="approved",
            consent_acknowledged_at=utc_now(),
        )
        subaccount_client = mock_build_subaccount_client.return_value
        service_context = subaccount_client.messaging.v1.services.return_value
        service_context.phone_numbers.list.return_value = []
        service_context.phone_numbers.create.side_effect = TwilioRestException(
            404,
            "/v1/Services/MGsub0001/PhoneNumbers",
            msg="Unable to create record: The requested resource /v1/Services/MGsub0001/PhoneNumbers was not found",
        )

        with self.assertRaises(ProviderProvisioningError) as ctx:
            sync_sender_assignment(organization.id, actor_user_id=99)

        self.assertIn("belongs to this organization's Twilio subaccount", str(ctx.exception))
        profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization.id).one()
        self.assertEqual(profile.provider_status, "error")
        self.assertIn("belongs to this organization's Twilio subaccount", profile.last_provision_error)

    @patch("app.services.twilio_service._build_subaccount_client")
    def test_release_sender_detaches_existing_service_numbers(self, mock_build_subaccount_client) -> None:
        from app.services.twilio_service import release_sender

        organization, _ = self._create_org_with_profile(
            twilio_subaccount_sid="ACsub0001",
            messaging_service_sid="MGsub0001",
            from_number="+15550001111",
            phone_number_sid="PN0001",
            provider_status="active",
            status="active",
        )
        sender_one = SimpleNamespace(delete=lambda: True)
        sender_one.phone_number = "+15550001111"
        sender_two = SimpleNamespace(delete=lambda: True)
        sender_two.phone_number = "+15550002222"
        subaccount_client = mock_build_subaccount_client.return_value
        service_context = subaccount_client.messaging.v1.services.return_value
        service_context.phone_numbers.list.return_value = [sender_one, sender_two]

        profile = release_sender(organization.id, actor_user_id=50)

        service_context.update.assert_called_once_with(
            inbound_request_url="https://beta.example.com/webhooks/twilio/inbound",
            inbound_method="POST",
            use_inbound_webhook_on_number=False,
        )
        self.assertEqual(profile.provider_status, "pending")
        self.assertIsNone(profile.from_number)
        self.assertIsNone(profile.phone_number_sid)

    def test_record_usage_candidates_creates_organization_scoped_ledger_rows(self) -> None:
        from app.services.twilio_service import record_usage_candidates

        organization, _ = self._create_org_with_profile()
        created = record_usage_candidates(
            organization.id,
            [{"sid": "SM123", "status": "sent", "account_sid": "ACsub0001"}],
            source="reply",
        )

        self.assertEqual(created, 1)
        record = self.MessagingUsageRecord.query.filter_by(message_sid="SM123").one()
        self.assertEqual(record.organization_id, organization.id)
        self.assertEqual(record.source, "reply")
        self.assertEqual(record.twilio_subaccount_sid, "ACsub0001")
        self.assertEqual(record.reconciliation_status, "pending")

    @patch("app.services.twilio_service._build_subaccount_client")
    def test_reconcile_messaging_usage_uses_read_only_client_for_suspended_provider(self, mock_build_subaccount_client) -> None:
        from app.services.provider_secret_service import encrypt_provider_secret
        from app.services.twilio_service import reconcile_messaging_usage

        organization, _ = self._create_org_with_profile(
            twilio_subaccount_sid="ACsub0003",
            twilio_auth_token_encrypted=encrypt_provider_secret("subaccount-token-3"),
            provider_status="suspended",
            status="suspended",
        )
        self.db.session.add(
            self.MessagingUsageRecord(
                organization_id=organization.id,
                message_sid="SMusage-read-only",
                source="scheduled",
                reconciliation_status="pending",
            )
        )
        self.db.session.commit()

        mock_client = mock_build_subaccount_client.return_value
        mock_client.messages.return_value.fetch.return_value = SimpleNamespace(
            status="delivered",
            num_segments="2",
            price="-0.0400",
            price_unit="usd",
            date_created=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
            account_sid="ACsub0003",
        )

        summary = reconcile_messaging_usage()

        self.db.session.expire_all()
        record = self.MessagingUsageRecord.query.filter_by(message_sid="SMusage-read-only").one()
        self.assertEqual(summary["records_finalized"], 1)
        self.assertEqual(record.reconciliation_status, "finalized")
        self.assertEqual(record.billable_units, 2)
        self.assertEqual(record.twilio_subaccount_sid, "ACsub0003")

    @patch("app.services.twilio_service._build_subaccount_client")
    def test_reconcile_messaging_usage_zeroes_sell_amount_for_complimentary_org(self, mock_build_subaccount_client) -> None:
        from app.services.provider_secret_service import encrypt_provider_secret
        from app.services.twilio_service import reconcile_messaging_usage

        organization, _ = self._create_org_with_profile(
            subscription_status="complimentary",
            twilio_subaccount_sid="ACsub0004",
            twilio_auth_token_encrypted=encrypt_provider_secret("subaccount-token-4"),
            provider_status="active",
            status="active",
        )
        self.db.session.add(
            self.MessagingUsageRecord(
                organization_id=organization.id,
                message_sid="SMusage-comped",
                source="blast",
                reconciliation_status="pending",
            )
        )
        self.db.session.commit()

        mock_client = mock_build_subaccount_client.return_value
        mock_client.messages.return_value.fetch.return_value = SimpleNamespace(
            status="delivered",
            num_segments="2",
            price="-0.0400",
            price_unit="usd",
            date_created=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
            account_sid="ACsub0004",
        )

        summary = reconcile_messaging_usage()

        self.db.session.expire_all()
        record = self.MessagingUsageRecord.query.filter_by(message_sid="SMusage-comped").one()
        self.assertEqual(summary["records_finalized"], 1)
        self.assertEqual(record.sell_rate, Decimal("0"))
        self.assertEqual(record.sell_amount, Decimal("0"))
        self.assertEqual(record.margin, Decimal("-0.0400"))

    def test_upsert_closed_usage_billing_periods_summarizes_overage(self) -> None:
        from app.services.twilio_service import previous_billing_period_window, upsert_closed_usage_billing_periods

        organization, _ = self._create_org_with_profile()
        period_start, period_end = previous_billing_period_window()
        self.db.session.add_all(
            [
                self.MessagingUsageRecord(
                    organization_id=organization.id,
                    message_sid="SM-1",
                    billable_units=1,
                    billable=True,
                    reconciliation_status="finalized",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                ),
                self.MessagingUsageRecord(
                    organization_id=organization.id,
                    message_sid="SM-2",
                    billable_units=1,
                    billable=True,
                    reconciliation_status="finalized",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                ),
            ]
        )
        self.db.session.commit()

        updated = upsert_closed_usage_billing_periods()

        self.assertEqual(updated, 1)
        period = self.OrganizationUsageBillingPeriod.query.filter_by(organization_id=organization.id).one()
        self.assertEqual(period.used_units, 2)
        self.assertEqual(period.included_units, 1)
        self.assertEqual(period.overage_units, 1)
        self.assertEqual(period.status, "pending")
        self.assertEqual(period.sell_amount, Decimal("0.0300"))

    def test_upsert_closed_usage_billing_periods_marks_complimentary_org_as_included(self) -> None:
        from app.services.twilio_service import previous_billing_period_window, upsert_closed_usage_billing_periods

        organization, _ = self._create_org_with_profile(subscription_status="complimentary")
        period_start, period_end = previous_billing_period_window()
        self.db.session.add_all(
            [
                self.MessagingUsageRecord(
                    organization_id=organization.id,
                    message_sid="SM-comp-1",
                    billable_units=1,
                    billable=True,
                    reconciliation_status="finalized",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                ),
                self.MessagingUsageRecord(
                    organization_id=organization.id,
                    message_sid="SM-comp-2",
                    billable_units=2,
                    billable=True,
                    reconciliation_status="finalized",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                ),
            ]
        )
        self.db.session.commit()

        updated = upsert_closed_usage_billing_periods()

        self.assertEqual(updated, 1)
        period = self.OrganizationUsageBillingPeriod.query.filter_by(organization_id=organization.id).one()
        self.assertEqual(period.used_units, 3)
        self.assertEqual(period.overage_units, 0)
        self.assertEqual(period.sell_amount, Decimal("0"))
        self.assertEqual(period.status, "included")

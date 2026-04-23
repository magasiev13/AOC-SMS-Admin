import importlib
import os
import tempfile
import unittest


class TestBillingSendReadiness(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "billing.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import Organization, OrganizationMessagingProfile, OrganizationSubscription
        from app.services.billing_service import (
            organization_can_transmit_messages,
            organization_transmit_block_reason,
        )

        self.db = db
        self.Organization = Organization
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationSubscription = OrganizationSubscription
        self.organization_can_transmit_messages = organization_can_transmit_messages
        self.organization_transmit_block_reason = organization_transmit_block_reason
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

    def _create_organization(
        self,
        *,
        subscription_status: str = "active",
        provider_status: str = "active",
        with_sender: bool = True,
    ):
        organization = self.Organization(name="Acme", slug="acme", status="active")
        subscription = self.OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status=subscription_status,
        )
        profile = self.OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            provider_status=provider_status,
            status=provider_status,
            sender_review_status="approved",
            messaging_service_sid="MGactive0001" if with_sender else None,
            from_number="+15550001111" if with_sender else None,
            phone_number_sid="PNactive0001" if with_sender else None,
        )
        self.db.session.add_all([organization, subscription, profile])
        self.db.session.commit()
        return organization

    def test_active_billing_and_active_provider_has_no_block_reason(self) -> None:
        organization = self._create_organization()

        self.assertTrue(self.organization_can_transmit_messages(organization))
        self.assertIsNone(self.organization_transmit_block_reason(organization))

    def test_missing_organization_blocks_sending(self) -> None:
        self.assertEqual(
            self.organization_transmit_block_reason(None),
            "Organization context is missing for message sending.",
        )

    def test_inactive_billing_blocks_before_provider_state(self) -> None:
        organization = self._create_organization(subscription_status="incomplete")

        self.assertFalse(self.organization_can_transmit_messages(organization))
        self.assertEqual(
            self.organization_transmit_block_reason(organization),
            "Organization billing is not active for message sending.",
        )

    def test_inactive_provider_blocks_after_billing_passes(self) -> None:
        organization = self._create_organization(provider_status="pending")

        self.assertFalse(self.organization_can_transmit_messages(organization))
        self.assertEqual(
            self.organization_transmit_block_reason(organization),
            "Messaging provider is not active for this organization.",
        )

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


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
            organization_can_send,
            organization_can_transmit_messages,
            organization_transmit_block_reason,
        )

        self.db = db
        self.Organization = Organization
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationSubscription = OrganizationSubscription
        self.organization_can_send = organization_can_send
        self.organization_can_transmit_messages = organization_can_transmit_messages
        self.organization_transmit_block_reason = organization_transmit_block_reason
        self._organization_counter = 0
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
        organization_status: str = "active",
        subscription_status: str = "active",
        provider_status: str = "active",
        with_sender: bool = True,
    ):
        self._organization_counter += 1
        organization = self.Organization(
            name=f"Acme {self._organization_counter}",
            slug=f"acme-{self._organization_counter}",
            status=organization_status,
        )
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
            messaging_service_sid=f"MGactive{self._organization_counter:04d}" if with_sender else None,
            from_number=f"+15550001{self._organization_counter:03d}" if with_sender else None,
            phone_number_sid=f"PNactive{self._organization_counter:04d}" if with_sender else None,
        )
        self.db.session.add_all([organization, subscription, profile])
        self.db.session.commit()
        return organization

    def test_active_billing_and_active_provider_has_no_block_reason(self) -> None:
        organization = self._create_organization()

        self.assertTrue(self.organization_can_transmit_messages(organization))
        self.assertIsNone(self.organization_transmit_block_reason(organization))

    def test_transmit_readiness_matrix_matches_block_reason(self) -> None:
        cases = [
            (
                "active org ready to send",
                {},
                True,
                None,
            ),
            (
                "suspended org",
                {"organization_status": "suspended"},
                False,
                "Organization is not active for message sending.",
            ),
            (
                "inactive billing",
                {"subscription_status": "incomplete"},
                False,
                "Organization billing is not active for message sending.",
            ),
            (
                "inactive provider",
                {"provider_status": "pending"},
                False,
                "Messaging provider is not active for this organization.",
            ),
        ]

        for label, kwargs, expected_can_transmit, expected_reason in cases:
            with self.subTest(label=label):
                organization = self._create_organization(**kwargs)

                self.assertEqual(
                    self.organization_can_transmit_messages(organization),
                    expected_can_transmit,
                )
                self.assertEqual(
                    self.organization_transmit_block_reason(organization),
                    expected_reason,
                )

    def test_organization_can_send_remains_billing_only(self) -> None:
        organization = self._create_organization(organization_status="suspended")

        self.assertTrue(self.organization_can_send(organization))
        self.assertFalse(self.organization_can_transmit_messages(organization))

    def test_current_offer_requires_verified_setup_payment(self) -> None:
        organization = self._create_organization()
        organization.billing_offer_version = "pilot-offer-v1:standard"
        organization.subscription.offer_version = "pilot-offer-v1:standard"
        self.db.session.commit()

        self.assertFalse(self.organization_can_send(organization))
        self.assertFalse(self.organization_can_transmit_messages(organization))

        organization.subscription.activation_fee_paid_at = datetime.now(timezone.utc)
        organization.subscription.activation_price_id = "price_activation"
        organization.subscription.activation_payment_intent_id = "pi_setup_verified"
        organization.subscription.activation_invoice_id = "in_setup_verified"
        self.db.session.commit()

        self.assertTrue(self.organization_can_send(organization))
        self.assertTrue(self.organization_can_transmit_messages(organization))

    def test_missing_organization_blocks_sending(self) -> None:
        self.assertFalse(self.organization_can_transmit_messages(None))
        self.assertEqual(
            self.organization_transmit_block_reason(None),
            "Organization context is missing for message sending.",
        )

    def test_suspended_organization_blocks_before_billing_and_provider_state(self) -> None:
        organization = self._create_organization(organization_status="suspended")

        self.assertFalse(self.organization_can_transmit_messages(organization))
        self.assertEqual(
            self.organization_transmit_block_reason(organization),
            "Organization is not active for message sending.",
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


class TestBillingPlanCatalogAndCheckout(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "billing-catalog.db")
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
        from app.models import Organization, OrganizationSubscription
        from app.services.billing_plans import (
            billing_plan_catalog,
            included_segments_for_subscription,
        )
        from app.services.billing_service import create_checkout_session

        self.db = db
        self.Organization = Organization
        self.OrganizationSubscription = OrganizationSubscription
        self.billing_plan_catalog = billing_plan_catalog
        self.included_segments_for_subscription = included_segments_for_subscription
        self.create_checkout_session = create_checkout_session
        self._organization_counter = 0
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_PRICE_ID="price_monthly",
            STRIPE_MONTHLY_PRICE_ID="price_monthly",
            STRIPE_ANNUAL_PRICE_ID="price_annual",
            STRIPE_ACTIVATION_PRICE_ID="price_activation",
            STRIPE_GROWTH_PRICE_ID="price_growth",
            STRIPE_SCALE_PRICE_ID="price_scale",
            BILLING_TRIAL_DAYS=0,
            BILLING_INCLUDED_OUTBOUND_SEGMENTS=42,
            BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS=1000,
            BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS=1000,
            BILLING_GROWTH_INCLUDED_OUTBOUND_SEGMENTS=3000,
            BILLING_SCALE_INCLUDED_OUTBOUND_SEGMENTS=10000,
        )
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

    def _create_subscription(
        self,
        *,
        price_id: str = "price_monthly",
        status: str = "incomplete",
        customer_id: str | None = None,
        subscription_id: str | None = None,
    ):
        self._organization_counter += 1
        organization = self.Organization(
            name=f"Plan Org {self._organization_counter}",
            slug=f"plan-org-{self._organization_counter}",
            status="active",
        )
        subscription = self.OrganizationSubscription(
            organization=organization,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            stripe_price_id=price_id,
            status=status,
        )
        self.db.session.add_all([organization, subscription])
        self.db.session.commit()
        return organization, subscription

    def test_plan_catalog_maps_price_ids_to_segment_allowances_with_legacy_fallback(self) -> None:
        _, annual_subscription = self._create_subscription(price_id="price_annual")
        _, growth_subscription = self._create_subscription(price_id="price_growth")
        _, unknown_subscription = self._create_subscription(price_id="price_unknown")

        plans = {plan.code: plan for plan in self.billing_plan_catalog()}

        self.assertEqual(plans["monthly"].price_id, "price_monthly")
        self.assertEqual(plans["monthly"].price_label, "$59.99/mo")
        self.assertEqual(plans["monthly"].included_segments, 1000)
        self.assertEqual(plans["annual"].price_id, "price_annual")
        self.assertEqual(plans["annual"].price_label, "$600/yr")
        self.assertEqual(self.included_segments_for_subscription(annual_subscription), 1000)
        self.assertEqual(plans["growth"].included_segments, 3000)
        self.assertEqual(plans["scale"].included_segments, 10000)
        self.assertEqual(self.included_segments_for_subscription(growth_subscription), 3000)
        self.assertEqual(self.included_segments_for_subscription(unknown_subscription), 42)

    @patch("app.services.billing_service._stripe_module")
    def test_checkout_charges_activation_and_recurring_plan_without_default_trial(self, mock_stripe_module) -> None:
        organization, _subscription = self._create_subscription()
        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.create.return_value = SimpleNamespace(
            id="cs_test_123",
            url="https://checkout.stripe.test/session",
        )
        mock_stripe_module.return_value = mock_stripe

        session = self.create_checkout_session(
            organization,
            "owner@example.com",
            "https://app.example.com/success",
            "https://app.example.com/cancel",
        )

        self.assertEqual(session.id, "cs_test_123")
        params = mock_stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(
            params["line_items"],
            [
                {"price": "price_activation", "quantity": 1},
                {"price": "price_monthly", "quantity": 1},
            ],
        )
        self.assertNotIn("trial_period_days", params["subscription_data"])

    @patch("app.services.billing_service._stripe_module")
    def test_checkout_can_select_annual_upfront_plan_with_setup_fee(self, mock_stripe_module) -> None:
        organization, _subscription = self._create_subscription()
        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.create.return_value = SimpleNamespace(
            id="cs_test_annual",
            url="https://checkout.stripe.test/annual",
        )
        mock_stripe_module.return_value = mock_stripe

        self.create_checkout_session(
            organization,
            "owner@example.com",
            "https://app.example.com/success",
            "https://app.example.com/cancel",
            plan_code="annual",
        )

        params = mock_stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(
            params["line_items"],
            [
                {"price": "price_activation", "quantity": 1},
                {"price": "price_annual", "quantity": 1},
            ],
        )
        self.assertEqual(organization.subscription.stripe_price_id, "price_annual")
        self.assertEqual(params["metadata"]["billing_plan_code"], "annual")
        self.assertEqual(params["subscription_data"]["metadata"]["billing_plan_code"], "annual")

    def test_checkout_rejects_unknown_plan_code(self) -> None:
        organization, _subscription = self._create_subscription()

        with self.assertRaisesRegex(RuntimeError, "valid billing option"):
            self.create_checkout_session(
                organization,
                "owner@example.com",
                "https://app.example.com/success",
                "https://app.example.com/cancel",
                plan_code="enterprise",
            )

    @patch("app.services.billing_service._stripe_module")
    def test_annual_only_organization_defaults_to_annual_and_blocks_monthly(self, mock_stripe_module) -> None:
        organization, _subscription = self._create_subscription()
        organization.billing_offer = "annual_only"
        self.db.session.commit()
        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.create.return_value = SimpleNamespace(
            id="cs_test_first_client",
            url="https://checkout.stripe.test/first-client",
        )
        mock_stripe_module.return_value = mock_stripe

        self.create_checkout_session(
            organization,
            "owner@example.com",
            "https://app.example.com/success",
            "https://app.example.com/cancel",
        )

        params = mock_stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(
            params["line_items"],
            [
                {"price": "price_activation", "quantity": 1},
                {"price": "price_annual", "quantity": 1},
            ],
        )
        self.assertEqual(organization.subscription.stripe_price_id, "price_annual")

        with self.assertRaisesRegex(RuntimeError, "valid billing option"):
            self.create_checkout_session(
                organization,
                "owner@example.com",
                "https://app.example.com/success",
                "https://app.example.com/cancel",
                plan_code="monthly",
            )

    @patch("app.services.billing_service._stripe_module")
    def test_annual_only_config_override_still_defaults_to_annual(self, mock_stripe_module) -> None:
        organization, _subscription = self._create_subscription()
        self.app.config["BILLING_ANNUAL_ONLY_ORG_SLUGS"] = organization.slug
        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.create.return_value = SimpleNamespace(
            id="cs_test_first_client_env",
            url="https://checkout.stripe.test/first-client-env",
        )
        mock_stripe_module.return_value = mock_stripe

        self.create_checkout_session(
            organization,
            "owner@example.com",
            "https://app.example.com/success",
            "https://app.example.com/cancel",
        )

        params = mock_stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(
            params["line_items"],
            [
                {"price": "price_activation", "quantity": 1},
                {"price": "price_annual", "quantity": 1},
            ],
        )
        self.assertEqual(organization.subscription.stripe_price_id, "price_annual")

        with self.assertRaisesRegex(RuntimeError, "valid billing option"):
            self.create_checkout_session(
                organization,
                "owner@example.com",
                "https://app.example.com/success",
                "https://app.example.com/cancel",
                plan_code="monthly",
            )

    @patch("app.services.billing_service._stripe_module")
    def test_checkout_omits_activation_only_after_verified_setup_payment(self, mock_stripe_module) -> None:
        organization, subscription = self._create_subscription(
            status="canceled",
            customer_id="cus_test_123",
            subscription_id="sub_old_123",
        )
        subscription.activation_fee_paid_at = datetime.now(timezone.utc)
        subscription.activation_price_id = "price_activation"
        subscription.activation_payment_intent_id = "pi_test_setup"
        subscription.activation_invoice_id = "in_test_setup"
        self.db.session.commit()
        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.create.return_value = SimpleNamespace(
            id="cs_test_resubscribe",
            url="https://checkout.stripe.test/resubscribe",
        )
        mock_stripe_module.return_value = mock_stripe

        self.create_checkout_session(
            organization,
            "owner@example.com",
            "https://app.example.com/success",
            "https://app.example.com/cancel",
        )

        params = mock_stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(params["line_items"], [{"price": "price_monthly", "quantity": 1}])
        self.assertEqual(params["customer"], "cus_test_123")
        self.assertNotIn("customer_email", params)

    def test_fake_checkout_remains_available_without_stripe_activation_call(self) -> None:
        organization, _subscription = self._create_subscription()
        self.app.config["STRIPE_FAKE_CHECKOUT_ENABLED"] = True

        with self.app.test_request_context("/"):
            session = self.create_checkout_session(
                organization,
                "owner@example.com",
                "https://app.example.com/success?session_id={CHECKOUT_SESSION_ID}",
                "https://app.example.com/cancel",
            )

        self.assertEqual(session.id, f"cs_fake_org_{organization.id}")
        self.assertIn(f"/_test/stripe/checkout/cs_fake_org_{organization.id}", session.url)

import importlib
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask


class TestStripeWebhookHardening(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
            "TWILIO_CREDENTIAL_ENCRYPTION_KEY": os.environ.get("TWILIO_CREDENTIAL_ENCRYPTION_KEY"),
        }
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["SAAS_MODE"] = "1"
        os.environ["TWILIO_CREDENTIAL_ENCRYPTION_KEY"] = "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o="
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.__init__ import _validate_saas_billing_config
        from app.models import (
            AppUser,
            Organization,
            OrganizationMembership,
            OrganizationSubscription,
            OrganizationUsageBillingPeriod,
            StripeCheckoutSession,
            StripeWebhookEvent,
            utc_now,
        )
        from app.services.billing_service import (
            process_stripe_webhook_event,
            reconcile_billing_subscriptions,
        )

        self.db = db
        self.AppUser = AppUser
        self.Organization = Organization
        self.OrganizationMembership = OrganizationMembership
        self.OrganizationSubscription = OrganizationSubscription
        self.OrganizationUsageBillingPeriod = OrganizationUsageBillingPeriod
        self.StripeCheckoutSession = StripeCheckoutSession
        self.StripeWebhookEvent = StripeWebhookEvent
        self.utc_now = utc_now
        self.process_stripe_webhook_event = process_stripe_webhook_event
        self.reconcile_billing_subscriptions = reconcile_billing_subscriptions
        self.validate_saas_billing_config = _validate_saas_billing_config

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_ANNUAL_PRICE_ID="price_annual_123",
            STRIPE_ACTIVATION_PRICE_ID="price_activation_123",
            STRIPE_STAGED_ACTIVATION_PRICE_ID="price_staged_activation_123",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            SAAS_BASE_URL="https://app.example.com",
        )
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        from app.migrations.runner import run_pending_migrations

        run_pending_migrations(self.db.engine, self.app.logger)
        self.client = self.app.test_client()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.subscription = self.OrganizationSubscription(
            organization=self.organization,
            stripe_price_id="price_test_123",
            status="incomplete",
        )
        self.owner = self.AppUser(
            username="owner",
            email="owner@acme.test",
            full_name="Owner User",
            phone="+15550000001",
            role="admin",
            must_change_password=False,
        )
        self.owner.set_password("Owner-pass1!")
        self.db.session.add_all([self.organization, self.subscription, self.owner])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=self.owner.id,
                role="owner",
            )
        )
        self.db.session.commit()

    def _issue_checkout_session(self, session_id: str) -> str:
        offer_version = f"{self.app.config['BILLING_OFFER_VERSION']}:standard"
        self.organization.billing_offer_version = offer_version
        self.subscription.offer_version = offer_version
        self.db.session.add(
            self.StripeCheckoutSession(
                organization_id=self.organization.id,
                stripe_checkout_session_id=session_id,
                billing_plan_code="monthly",
                recurring_price_id="price_test_123",
                activation_price_id="price_activation_123",
                offer_version=offer_version,
                status="open",
            )
        )
        self.db.session.commit()
        return offer_version

    def _paid_checkout_session(self, session_id: str, offer_version: str) -> dict:
        return {
            "id": session_id,
            "created": int((self.subscription.created_at + timedelta(minutes=1)).timestamp()),
            "status": "complete",
            "payment_status": "paid",
            "customer_email": "owner@acme.test",
            "customer": "cus_test_123",
            "subscription": "sub_test_123",
            "invoice": "in_test_setup",
            "client_reference_id": str(self.organization.id),
            "metadata": {
                "organization_id": str(self.organization.id),
                "billing_offer_version": offer_version,
            },
        }

    def _configure_paid_checkout_mocks(self, mock_stripe: MagicMock, session: dict) -> None:
        mock_stripe.checkout.Session.retrieve.return_value = session
        mock_stripe.checkout.Session.list_line_items.return_value = SimpleNamespace(
            data=[
                {"price": {"id": "price_activation_123"}, "quantity": 1},
                {"price": {"id": "price_test_123"}, "quantity": 1},
            ]
        )
        mock_stripe.Invoice.retrieve.return_value = {
            "id": "in_test_setup",
            "paid": True,
            "status": "paid",
            "amount_paid": 20899,
            "payment_intent": {
                "id": "pi_test_setup",
                "status": "succeeded",
                "amount_received": 20899,
            },
            "lines": {
                "data": [
                    {"price": {"id": "price_activation_123"}, "quantity": 1},
                    {"price": {"id": "price_test_123"}, "quantity": 1},
                ]
            },
        }

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._ctx.pop()
        self._temp_dir.cleanup()
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _subscription_event(self, *, event_id: str, status: str = "trialing") -> dict:
        return {
            "id": event_id,
            "type": "customer.subscription.updated",
            "created": 1773898071,
            "data": {
                "object": {
                    "id": "sub_test_123",
                    "customer": "cus_test_123",
                    "status": status,
                    "metadata": {"organization_id": str(self.organization.id)},
                    "current_period_end": 1775107599,
                    "items": {"data": [{"price": {"id": "price_test_123"}}]},
                }
            },
        }

    @patch("app.services.billing_service._stripe_module")
    def test_process_stripe_webhook_event_is_idempotent(self, mock_stripe_module) -> None:
        event = self._subscription_event(event_id="evt_test_duplicate")
        mock_stripe = MagicMock()
        mock_stripe.Subscription.retrieve.return_value = event["data"]["object"]
        mock_stripe_module.return_value = mock_stripe

        self.process_stripe_webhook_event(event)
        self.process_stripe_webhook_event(event)

        record = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_duplicate").first()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "processed")
        self.assertEqual(record.attempt_count, 2)
        self.assertEqual(self.StripeWebhookEvent.query.count(), 1)
        self.assertEqual(self.subscription.status, "trialing")
        self.assertEqual(self.subscription.stripe_customer_id, "cus_test_123")
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_test_123")
        mock_stripe.Subscription.retrieve.assert_called_once_with("sub_test_123")

    @patch("app.services.billing_service._stripe_module")
    def test_staged_setup_checkout_records_payment_without_activating_subscription(self, mock_stripe_module) -> None:
        offer_version = f"{self.app.config['BILLING_OFFER_VERSION']}:staged_annual"
        self.organization.billing_offer = "staged_annual"
        self.organization.billing_offer_version = offer_version
        self.subscription.offer_version = offer_version
        self.subscription.stripe_price_id = "price_annual_123"
        self.db.session.add(
            self.StripeCheckoutSession(
                organization_id=self.organization.id,
                stripe_checkout_session_id="cs_test_staged_setup",
                billing_plan_code="setup_only",
                recurring_price_id="price_annual_123",
                activation_price_id="price_staged_activation_123",
                offer_version=offer_version,
                status="open",
            )
        )
        self.db.session.commit()
        checkout_session = {
            "id": "cs_test_staged_setup",
            "status": "complete",
            "payment_status": "paid",
            "mode": "payment",
            "currency": "usd",
            "amount_total": 15000,
            "customer": "cus_test_staged",
            "payment_intent": "pi_test_staged_setup",
            "subscription": None,
            "client_reference_id": str(self.organization.id),
            "metadata": {
                "organization_id": str(self.organization.id),
                "billing_offer_version": offer_version,
                "billing_checkout_kind": "setup_only",
            },
        }
        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.retrieve.return_value = checkout_session
        mock_stripe.checkout.Session.list_line_items.return_value = SimpleNamespace(
            data=[
                {
                    "price": {"id": "price_staged_activation_123"},
                    "quantity": 1,
                }
            ]
        )
        mock_stripe.PaymentIntent.retrieve.return_value = {
            "id": "pi_test_staged_setup",
            "status": "succeeded",
            "currency": "usd",
            "amount_received": 15000,
        }
        mock_stripe_module.return_value = mock_stripe
        event = {
            "id": "evt_test_staged_setup",
            "type": "checkout.session.completed",
            "created": 1773898071,
            "data": {"object": checkout_session},
        }

        self.process_stripe_webhook_event(event)

        self.assertEqual(self.subscription.status, "incomplete")
        self.assertEqual(self.subscription.stripe_customer_id, "cus_test_staged")
        self.assertIsNone(self.subscription.stripe_subscription_id)
        self.assertEqual(
            self.subscription.activation_price_id,
            "price_staged_activation_123",
        )
        self.assertEqual(
            self.subscription.activation_payment_intent_id,
            "pi_test_staged_setup",
        )
        self.assertIsNotNone(self.subscription.activation_fee_paid_at)
        mock_stripe.Subscription.retrieve.assert_not_called()

    @patch("app.services.billing_service._stripe_module")
    def test_paid_stripe_event_fails_for_complimentary_organization(self, mock_stripe_module) -> None:
        self.subscription.status = "complimentary"
        self.db.session.commit()
        event = self._subscription_event(
            event_id="evt_test_complimentary_conflict",
            status="active",
        )
        mock_stripe = MagicMock()
        mock_stripe.Subscription.retrieve.return_value = event["data"]["object"]
        mock_stripe_module.return_value = mock_stripe

        with self.assertRaisesRegex(
            RuntimeError,
            "Stripe reported chargeable billing for complimentary organization",
        ):
            self.process_stripe_webhook_event(event)

        record = self.StripeWebhookEvent.query.filter_by(
            stripe_event_id="evt_test_complimentary_conflict"
        ).one()
        self.assertEqual(record.status, "failed")
        self.assertIn("customer_id=cus_test_123", record.last_error)
        self.assertIn("subscription_id=sub_test_123", record.last_error)
        self.assertEqual(self.subscription.status, "complimentary")
        self.assertIsNone(self.subscription.stripe_customer_id)
        self.assertIsNone(self.subscription.stripe_subscription_id)

    @patch("app.services.billing_service._stripe_module")
    def test_invoice_webhook_uses_nested_subscription_details(self, mock_stripe_module) -> None:
        mock_stripe = MagicMock()
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_test_nested",
            "customer": "cus_test_nested",
            "status": "active",
            "metadata": {"organization_id": str(self.organization.id)},
            "current_period_end": 1775107599,
            "items": {"data": [{"price": {"id": "price_test_123"}}]},
        }
        mock_stripe_module.return_value = mock_stripe
        event = {
            "id": "evt_test_invoice_nested",
            "type": "invoice.payment_succeeded",
            "created": 1773898071,
            "data": {
                "object": {
                    "id": "in_test_nested",
                    "customer": "cus_test_nested",
                    "parent": {
                        "type": "subscription_details",
                        "subscription_details": {
                            "subscription": "sub_test_nested",
                            "metadata": {"organization_id": str(self.organization.id)},
                        },
                    },
                    "lines": {
                        "data": [
                            {
                                "metadata": {"organization_id": str(self.organization.id)},
                                "parent": {
                                    "type": "subscription_item_details",
                                    "subscription_item_details": {
                                        "subscription": "sub_test_nested",
                                    },
                                },
                            }
                        ]
                    },
                }
            },
        }

        self.process_stripe_webhook_event(event)

        record = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_invoice_nested").first()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "processed")
        self.assertEqual(record.organization_id, self.organization.id)
        self.assertEqual(record.stripe_subscription_id, "sub_test_nested")
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(self.subscription.stripe_customer_id, "cus_test_nested")
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_test_nested")
        mock_stripe.Subscription.retrieve.assert_called_once_with("sub_test_nested")

    @patch("app.services.billing_service._stripe_module")
    def test_orphaned_invoice_webhook_is_ignored_without_retry(self, mock_stripe_module) -> None:
        mock_stripe = MagicMock()
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_test_orphaned",
            "customer": "cus_test_orphaned",
            "status": "active",
            "metadata": {"organization_id": "9999"},
            "current_period_end": 1775107599,
            "items": {"data": [{"price": {"id": "price_test_123"}}]},
        }
        mock_stripe_module.return_value = mock_stripe
        event = {
            "id": "evt_test_invoice_orphaned",
            "type": "invoice.payment_succeeded",
            "created": 1773898071,
            "data": {
                "object": {
                    "id": "in_test_orphaned",
                    "customer": "cus_test_orphaned",
                    "parent": {
                        "type": "subscription_details",
                        "subscription_details": {
                            "subscription": "sub_test_orphaned",
                            "metadata": {"organization_id": "9999"},
                        },
                    },
                }
            },
        }

        self.process_stripe_webhook_event(event)

        record = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_invoice_orphaned").first()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "ignored")
        self.assertIsNone(record.organization_id)
        self.assertEqual(record.stripe_subscription_id, "sub_test_orphaned")
        self.assertIn("No local subscription matched", record.last_error)
        mock_stripe.Subscription.retrieve.assert_called_once_with("sub_test_orphaned")

    @patch("app.services.billing_service._apply_stripe_event_to_billing_state")
    def test_failed_webhook_event_retries_on_redelivery(self, mock_apply) -> None:
        state = {"calls": 0}

        def _side_effect(*_args, **_kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("boom")
            self.subscription.status = "trialing"
            self.subscription.stripe_customer_id = "cus_test_123"
            self.subscription.stripe_subscription_id = "sub_test_123"
            self.db.session.commit()
            return self.subscription

        mock_apply.side_effect = _side_effect
        event = self._subscription_event(event_id="evt_test_retry")

        with self.assertRaises(RuntimeError):
            self.process_stripe_webhook_event(event)

        failed = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_retry").first()
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.attempt_count, 1)

        self.process_stripe_webhook_event(event)

        processed = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_retry").first()
        self.assertEqual(processed.status, "processed")
        self.assertEqual(processed.attempt_count, 2)

    @patch("app.services.billing_service._apply_stripe_event_to_billing_state")
    def test_processing_webhook_event_is_skipped_when_fresh(self, mock_apply) -> None:
        now = self.utc_now()
        record = self.StripeWebhookEvent(
            stripe_event_id="evt_test_fresh",
            event_type="customer.subscription.updated",
            stripe_subscription_id="sub_test_123",
            organization_id=self.organization.id,
            signature_verified=True,
            received_at=now,
            last_seen_at=now,
            status="processing",
            attempt_count=1,
        )
        self.db.session.add(record)
        self.db.session.commit()

        self.process_stripe_webhook_event(self._subscription_event(event_id="evt_test_fresh"))

        refreshed = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_fresh").first()
        self.assertEqual(refreshed.status, "processing")
        self.assertEqual(refreshed.attempt_count, 2)
        mock_apply.assert_not_called()

    @patch("app.services.billing_service._apply_stripe_event_to_billing_state")
    def test_processing_webhook_event_retries_when_stale(self, mock_apply) -> None:
        stale_time = self.utc_now() - timedelta(minutes=6)
        record = self.StripeWebhookEvent(
            stripe_event_id="evt_test_stale",
            event_type="customer.subscription.updated",
            stripe_subscription_id="sub_test_123",
            organization_id=self.organization.id,
            signature_verified=True,
            received_at=stale_time,
            last_seen_at=stale_time,
            status="processing",
            attempt_count=1,
        )
        self.db.session.add(record)
        self.db.session.commit()

        def _side_effect(*_args, **_kwargs):
            self.subscription.status = "trialing"
            self.subscription.stripe_customer_id = "cus_test_123"
            self.subscription.stripe_subscription_id = "sub_test_123"
            self.db.session.commit()
            return self.subscription

        mock_apply.side_effect = _side_effect
        self.process_stripe_webhook_event(self._subscription_event(event_id="evt_test_stale"))

        refreshed = self.StripeWebhookEvent.query.filter_by(stripe_event_id="evt_test_stale").first()
        self.assertEqual(refreshed.status, "processed")
        self.assertEqual(refreshed.attempt_count, 2)
        mock_apply.assert_called_once()

    def test_webhook_route_rejects_invalid_signature(self) -> None:
        fake_stripe = SimpleNamespace(
            api_key=None,
            Webhook=SimpleNamespace(
                construct_event=staticmethod(lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad signature")))
            ),
        )
        with patch.dict(sys.modules, {"stripe": fake_stripe}):
            response = self.client.post(
                "/webhooks/stripe",
                data="{}",
                headers={"Stripe-Signature": "bad"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.StripeWebhookEvent.query.count(), 0)

    @patch("app.services.billing_service._stripe_module")
    def test_reconcile_billing_subscriptions_repairs_incomplete_subscription(self, mock_stripe_module) -> None:
        mock_stripe = MagicMock()
        offer_version = self._issue_checkout_session("cs_test_123")
        checkout_session = self._paid_checkout_session("cs_test_123", offer_version)
        mock_stripe.checkout.Session.list.return_value.data = [checkout_session]
        self._configure_paid_checkout_mocks(mock_stripe, checkout_session)
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_test_123",
            "customer": "cus_test_123",
            "status": "trialing",
            "current_period_end": 1775107599,
            "items": {"data": [{"price": {"id": "price_test_123"}}]},
        }
        mock_stripe_module.return_value = mock_stripe

        summary = self.reconcile_billing_subscriptions()

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(self.subscription.status, "trialing")
        self.assertEqual(self.subscription.stripe_customer_id, "cus_test_123")
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_test_123")

    @patch("app.services.billing_service._stripe_module")
    def test_refresh_subscription_from_stripe_paginates_checkout_sessions_until_match(self, mock_stripe_module) -> None:
        from app.services.billing_service import refresh_subscription_from_stripe

        offer_version = self._issue_checkout_session("cs_target")

        first_page = SimpleNamespace(
            data=[
                {
                    "id": "cs_page_1",
                    "created": int((self.subscription.created_at + timedelta(minutes=1)).timestamp()),
                    "status": "complete",
                    "customer_email": "other@acme.test",
                    "customer": "cus_other",
                    "subscription": "sub_other",
                    "client_reference_id": str(self.organization.id),
                    "metadata": {"organization_id": str(self.organization.id)},
                }
            ],
            has_more=True,
        )
        second_page = SimpleNamespace(
            data=[
                {
                    **self._paid_checkout_session("cs_target", offer_version),
                    "created": int((self.subscription.created_at + timedelta(minutes=2)).timestamp()),
                }
            ],
            has_more=False,
        )

        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.list.side_effect = [first_page, second_page]
        self._configure_paid_checkout_mocks(
            mock_stripe,
            self._paid_checkout_session("cs_target", offer_version),
        )
        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_test_123",
            "customer": "cus_test_123",
            "status": "trialing",
            "current_period_end": 1775107599,
            "items": {"data": [{"price": {"id": "price_test_123"}}]},
        }
        mock_stripe_module.return_value = mock_stripe

        subscription = refresh_subscription_from_stripe(self.organization, "owner@acme.test")

        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.status, "trialing")
        self.assertEqual(mock_stripe.checkout.Session.list.call_count, 2)
        first_call = mock_stripe.checkout.Session.list.call_args_list[0].kwargs
        second_call = mock_stripe.checkout.Session.list.call_args_list[1].kwargs
        self.assertEqual(first_call["status"], "complete")
        self.assertEqual(first_call["limit"], 100)
        self.assertIn("created", first_call)
        self.assertNotIn("starting_after", first_call)
        self.assertEqual(second_call["starting_after"], "cs_page_1")

    @patch("app.services.billing_service._stripe_module")
    def test_reconcile_billing_subscriptions_ignores_stale_completed_checkout_sessions(self, mock_stripe_module) -> None:
        from datetime import timedelta

        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.list.return_value.data = [
            {
                "id": "cs_test_stale",
                "created": int((self.subscription.created_at - timedelta(days=2)).timestamp()),
                "status": "complete",
                "customer_email": "owner@acme.test",
                "customer": "cus_test_old",
                "subscription": "sub_test_old",
                "client_reference_id": str(self.organization.id),
                "metadata": {"organization_id": str(self.organization.id)},
            }
        ]
        mock_stripe_module.return_value = mock_stripe

        summary = self.reconcile_billing_subscriptions()

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(self.subscription.status, "incomplete")
        self.assertIsNone(self.subscription.stripe_customer_id)
        self.assertIsNone(self.subscription.stripe_subscription_id)
        mock_stripe.Subscription.retrieve.assert_not_called()

    @patch("app.services.billing_service._stripe_module")
    def test_usage_invoice_posting_retries_with_stable_idempotency_key(self, mock_stripe_module) -> None:
        from app.services.billing_service import _post_closed_usage_invoice_items
        from app.services.twilio_service import previous_billing_period_window

        self.subscription.stripe_customer_id = "cus_test_123"
        period_start, period_end = previous_billing_period_window()
        period = self.OrganizationUsageBillingPeriod(
            organization_id=self.organization.id,
            period_start=period_start,
            period_end=period_end,
            included_units=1000,
            used_units=1005,
            overage_units=5,
            sell_amount=Decimal("0.1500"),
            currency="usd",
            status="pending",
        )
        self.db.session.add(period)
        self.db.session.commit()

        mock_stripe = MagicMock()
        mock_stripe.Invoice.create.return_value = SimpleNamespace(id="in_overage_123")
        mock_stripe.Invoice.retrieve.return_value = {
            "id": "in_overage_123",
            "status": "draft",
            "metadata": {
                "organization_id": str(self.organization.id),
                "period_start": period_start.date().isoformat(),
                "period_end": period_end.date().isoformat(),
                "overage_units": "5",
                "settlement_version": "1",
            },
        }
        mock_stripe.InvoiceItem.create.return_value = SimpleNamespace(id="ii_test_123")
        mock_stripe.Invoice.finalize_invoice.return_value = SimpleNamespace(
            id="in_overage_123",
            status="open",
        )
        mock_stripe_module.return_value = mock_stripe

        real_commit = self.db.session.commit
        state = {"calls": 0}

        def flaky_commit():
            state["calls"] += 1
            if state["calls"] == 3:
                raise RuntimeError("db write failed")
            return real_commit()

        with patch("app.services.billing_service.db.session.commit", side_effect=flaky_commit):
            first_summary = _post_closed_usage_invoice_items()
            self.assertEqual(first_summary["periods_failed"], 1)
            self.db.session.rollback()
            self.db.session.expire_all()
            second_summary = _post_closed_usage_invoice_items()

        self.db.session.expire_all()
        period = self.OrganizationUsageBillingPeriod.query.filter_by(organization_id=self.organization.id).one()
        self.assertEqual(second_summary["periods_posted"], 1)
        self.assertEqual(period.status, "posted")
        self.assertEqual(period.stripe_invoice_item_id, "ii_test_123")
        self.assertEqual(period.stripe_invoice_id, "in_overage_123")
        self.assertEqual(period.invoiced_units, 5)
        self.assertEqual(mock_stripe.InvoiceItem.create.call_count, 2)
        first_call = mock_stripe.InvoiceItem.create.call_args_list[0].kwargs
        second_call = mock_stripe.InvoiceItem.create.call_args_list[1].kwargs
        self.assertEqual(first_call["idempotency_key"], second_call["idempotency_key"])
        self.assertIn(f"sms-overage:{self.organization.id}:", first_call["idempotency_key"])
        self.assertEqual(first_call["invoice"], "in_overage_123")
        self.assertEqual(first_call["amount"], 15)
        mock_stripe.Invoice.finalize_invoice.assert_called()


class TestSaasBillingConfigValidation(unittest.TestCase):
    def test_missing_billing_settings_fail_validation(self) -> None:
        from app.__init__ import _validate_saas_billing_config

        app = Flask(__name__)
        app.config.update(
            SAAS_MODE=True,
            STRIPE_SECRET_KEY="",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_ANNUAL_PRICE_ID="price_annual_123",
            STRIPE_ACTIVATION_PRICE_ID="price_activation_123",
            SAAS_BASE_URL="https://app.example.com",
            TWILIO_CREDENTIAL_ENCRYPTION_KEY="4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=",
        )

        with self.assertRaises(RuntimeError):
            _validate_saas_billing_config(app)

    def test_missing_encryption_key_fails_validation(self) -> None:
        from app.__init__ import _validate_saas_billing_config

        app = Flask(__name__)
        app.config.update(
            SAAS_MODE=True,
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_ANNUAL_PRICE_ID="price_annual_123",
            STRIPE_ACTIVATION_PRICE_ID="price_activation_123",
            SAAS_BASE_URL="https://app.example.com",
            TWILIO_CREDENTIAL_ENCRYPTION_KEY="",
        )

        with self.assertRaises(RuntimeError):
            _validate_saas_billing_config(app)

    def test_missing_activation_price_fails_validation(self) -> None:
        from app.__init__ import _validate_saas_billing_config

        app = Flask(__name__)
        app.config.update(
            SAAS_MODE=True,
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_ANNUAL_PRICE_ID="price_annual_123",
            STRIPE_ACTIVATION_PRICE_ID="",
            SAAS_BASE_URL="https://app.example.com",
            TWILIO_CREDENTIAL_ENCRYPTION_KEY="4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=",
        )

        with self.assertRaises(RuntimeError):
            _validate_saas_billing_config(app)

    def test_missing_annual_price_fails_validation(self) -> None:
        from app.__init__ import _validate_saas_billing_config

        app = Flask(__name__)
        app.config.update(
            SAAS_MODE=True,
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_ANNUAL_PRICE_ID="",
            STRIPE_ACTIVATION_PRICE_ID="price_activation_123",
            SAAS_BASE_URL="https://app.example.com",
            TWILIO_CREDENTIAL_ENCRYPTION_KEY="4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=",
        )

        with self.assertRaises(RuntimeError) as ctx:
            _validate_saas_billing_config(app)

        self.assertIn("STRIPE_ANNUAL_PRICE_ID", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

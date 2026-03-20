import importlib
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask


class TestStripeWebhookHardening(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
        }
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["SAAS_MODE"] = "1"
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
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            SAAS_BASE_URL="https://beta.example.com",
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

    def test_process_stripe_webhook_event_is_idempotent(self) -> None:
        event = self._subscription_event(event_id="evt_test_duplicate")

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
        mock_stripe.checkout.Session.list.return_value.data = [
            {
                "id": "cs_test_123",
                "status": "complete",
                "customer_email": "owner@acme.test",
                "customer": "cus_test_123",
                "subscription": "sub_test_123",
                "client_reference_id": str(self.organization.id),
                "metadata": {"organization_id": str(self.organization.id)},
            }
        ]
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


class TestSaasBillingConfigValidation(unittest.TestCase):
    def test_missing_billing_settings_fail_validation(self) -> None:
        from app.__init__ import _validate_saas_billing_config

        app = Flask(__name__)
        app.config.update(
            SAAS_MODE=True,
            STRIPE_SECRET_KEY="",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            STRIPE_PRICE_ID="price_test_123",
            SAAS_BASE_URL="https://beta.example.com",
        )

        with self.assertRaises(RuntimeError):
            _validate_saas_billing_config(app)


if __name__ == "__main__":
    unittest.main()

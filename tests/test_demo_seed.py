import os
import tempfile
import unittest


class TestDemoSeed(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
            "SCHEDULER_ENABLED": os.environ.get("SCHEDULER_ENABLED"),
            "SAAS_BASE_URL": os.environ.get("SAAS_BASE_URL"),
            "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY"),
            "STRIPE_WEBHOOK_SECRET": os.environ.get("STRIPE_WEBHOOK_SECRET"),
            "STRIPE_PRICE_ID": os.environ.get("STRIPE_PRICE_ID"),
            "STRIPE_ACTIVATION_PRICE_ID": os.environ.get("STRIPE_ACTIVATION_PRICE_ID"),
            "SECRET_KEY": os.environ.get("SECRET_KEY"),
        }
        self._temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{os.path.join(self._temp_dir.name, 'demo-seed.db')}"

    def tearDown(self) -> None:
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._temp_dir.cleanup()

    def test_demo_seed_creates_production_like_multi_org_dataset(self) -> None:
        from app.demo_seed import seed_demo_database
        from app import create_app, db
        from app.models import (
            AppUser,
            CommunityMember,
            Event,
            InboxThread,
            Organization,
            OrganizationInvitation,
            OrganizationSubscription,
            ScheduledMessage,
        )

        summary = seed_demo_database(
            reset=True,
            database_url=self.database_url,
            base_url="http://127.0.0.1:5000",
        )

        self.assertEqual(len(summary["organizations"]), 4)
        self.assertIn("No live Twilio sender assigned.", summary["live_sender_note"])

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        with app.app_context():
            self.assertEqual(Organization.query.count(), 4)
            self.assertGreaterEqual(AppUser.query.count(), 6)
            self.assertGreaterEqual(CommunityMember.query.count(), 10)
            self.assertGreaterEqual(Event.query.count(), 3)
            self.assertGreaterEqual(InboxThread.query.count(), 3)
            self.assertGreaterEqual(ScheduledMessage.query.count(), 3)

            internal_org = Organization.query.filter_by(slug="twinevia-internal").first()
            self.assertIsNotNone(internal_org)
            self.assertEqual(internal_org.subscription.status, "trialing")
            self.assertEqual(internal_org.messaging_profile.status, "pending")
            self.assertEqual(internal_org.messaging_profile.provider_status, "pending")

            harbor_org = Organization.query.filter_by(slug="harbor-events-co").first()
            self.assertIsNotNone(harbor_org)
            pending_owner_invite = OrganizationInvitation.query.filter_by(
                organization_id=harbor_org.id,
                role="owner",
                status="pending",
            ).first()
            self.assertIsNotNone(pending_owner_invite)

            sunset_org = Organization.query.filter_by(slug="sunset-realty-group").first()
            self.assertIsNotNone(sunset_org)
            self.assertEqual(sunset_org.status, "suspended")

            subscriptions = OrganizationSubscription.query.all()
            subscription_statuses = {subscription.status for subscription in subscriptions}
            self.assertIn("trialing", subscription_statuses)
            self.assertIn("active", subscription_statuses)
            self.assertIn("incomplete", subscription_statuses)
            self.assertIn("past_due", subscription_statuses)
            db.session.remove()

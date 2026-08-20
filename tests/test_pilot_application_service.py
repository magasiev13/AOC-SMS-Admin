import importlib
import os
import tempfile
import unittest


class TestPilotApplicationService(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        database_path = os.path.join(self._temp_dir.name, "pilot-applications.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{database_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "pilot-test-secret",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
                "STRIPE_SECRET_KEY": "sk_test_pilot",
                "STRIPE_WEBHOOK_SECRET": "whsec_pilot",
                "STRIPE_MONTHLY_PRICE_ID": "price_monthly_pilot",
                "STRIPE_ANNUAL_PRICE_ID": "price_annual_pilot",
                "STRIPE_ACTIVATION_PRICE_ID": "price_activation_pilot",
                "SAAS_BASE_URL": "https://app.example.com",
            }
        )

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import (
            AppUser,
            Organization,
            OrganizationInvitation,
            PilotApplication,
            PilotApplicationStatusHistory,
        )
        from app.services.pilot_application_service import (
            PilotApplicationError,
            PilotApplicationRateLimitError,
            PilotApplicationSubmission,
            approve_pilot_application,
            create_pilot_application,
            decline_pilot_application,
        )

        self.db = db
        self.AppUser = AppUser
        self.Organization = Organization
        self.OrganizationInvitation = OrganizationInvitation
        self.PilotApplication = PilotApplication
        self.PilotApplicationStatusHistory = PilotApplicationStatusHistory
        self.PilotApplicationError = PilotApplicationError
        self.PilotApplicationRateLimitError = PilotApplicationRateLimitError
        self.PilotApplicationSubmission = PilotApplicationSubmission
        self.approve_pilot_application = approve_pilot_application
        self.create_pilot_application = create_pilot_application
        self.decline_pilot_application = decline_pilot_application

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            PILOT_APPLICATION_RATE_LIMIT_COUNT=2,
            PILOT_APPLICATION_RATE_LIMIT_WINDOW_SECONDS=3600,
            BILLING_OFFER_VERSION="pilot-offer-v1",
        )
        self._context = self.app.app_context()
        self._context.push()
        self.db.create_all()

        self.reviewer = self.AppUser(
            username="pilot-reviewer",
            email="reviewer@example.com",
            phone="+15550000001",
            role="admin",
            is_platform_admin=True,
            must_change_password=False,
        )
        self.reviewer.set_password("Reviewer-pass1!")
        self.db.session.add(self.reviewer)
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._context.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def _submission(self, email: str) -> object:
        return self.PilotApplicationSubmission(
            business_name="Northstar Community",
            contact_name="Jordan Smith",
            email=email,
            phone="+15550000002",
            website_url="https://northstar.example.com",
            use_case="We send operational community notices to opted-in members.",
            expected_monthly_segments="700",
            twilio_account_status="needs_guidance",
            honeypot="",
        )

    def test_submission_stores_only_bounded_application_and_history(self) -> None:
        organization_count = self.Organization.query.count()
        user_count = self.AppUser.query.count()

        application = self.create_pilot_application(
            self._submission("owner@northstar.example"),
            "203.0.113.10",
            "Test Browser",
        )

        self.assertEqual(application.status, "new")
        self.assertEqual(application.expected_monthly_segments, 700)
        self.assertNotEqual(application.source_ip_hash, "203.0.113.10")
        self.assertEqual(self.Organization.query.count(), organization_count)
        self.assertEqual(self.AppUser.query.count(), user_count)
        history = self.PilotApplicationStatusHistory.query.filter_by(
            pilot_application_id=application.id
        ).all()
        self.assertEqual([entry.to_status for entry in history], ["new"])

    def test_honeypot_and_source_rate_limit_allocate_no_extra_resources(self) -> None:
        trapped = self._submission("trap@example.com")
        trapped = self.PilotApplicationSubmission(
            business_name=trapped.business_name,
            contact_name=trapped.contact_name,
            email=trapped.email,
            phone=trapped.phone,
            website_url=trapped.website_url,
            use_case=trapped.use_case,
            expected_monthly_segments=trapped.expected_monthly_segments,
            twilio_account_status=trapped.twilio_account_status,
            honeypot="filled-by-bot",
        )
        with self.assertRaises(self.PilotApplicationError):
            self.create_pilot_application(trapped, "203.0.113.20", "Bot")

        self.create_pilot_application(
            self._submission("first@example.com"),
            "203.0.113.21",
            "Browser",
        )
        self.create_pilot_application(
            self._submission("second@example.com"),
            "203.0.113.21",
            "Browser",
        )
        with self.assertRaises(self.PilotApplicationRateLimitError):
            self.create_pilot_application(
                self._submission("third@example.com"),
                "203.0.113.21",
                "Browser",
            )
        self.assertEqual(self.PilotApplication.query.count(), 2)

    def test_approval_is_idempotent_and_issues_one_owner_invitation(self) -> None:
        application = self.create_pilot_application(
            self._submission("approved@example.com"),
            "203.0.113.30",
            "Browser",
        )

        first_result = self.approve_pilot_application(
            application.id,
            self.reviewer,
            "Approved for the managed pilot.",
        )
        second_result = self.approve_pilot_application(
            application.id,
            self.reviewer,
            "Repeated approval should not allocate resources.",
        )

        self.assertEqual(first_result.organization.id, second_result.organization.id)
        self.assertEqual(first_result.invitation.id, second_result.invitation.id)
        self.assertEqual(first_result.application.status, "approved")
        self.assertEqual(first_result.organization.billing_offer_version, "pilot-offer-v1")
        self.assertEqual(first_result.organization.subscription.status, "incomplete")
        self.assertEqual(first_result.organization.messaging_profile.provider_status, "pending")
        self.assertEqual(first_result.organization.a2p_onboarding.onboarding_status, "draft")
        self.assertEqual(
            self.OrganizationInvitation.query.filter_by(
                organization_id=first_result.organization.id,
                role="owner",
            ).count(),
            1,
        )
        transitions = self.PilotApplicationStatusHistory.query.filter_by(
            pilot_application_id=application.id,
            to_status="approved",
        ).count()
        self.assertEqual(transitions, 1)

    def test_declined_application_cannot_be_approved(self) -> None:
        application = self.create_pilot_application(
            self._submission("declined@example.com"),
            "203.0.113.40",
            "Browser",
        )
        declined = self.decline_pilot_application(
            application.id,
            self.reviewer,
            "Use case is outside the pilot scope.",
        )

        self.assertEqual(declined.status, "declined")
        with self.assertRaisesRegex(self.PilotApplicationError, "cannot be approved"):
            self.approve_pilot_application(
                application.id,
                self.reviewer,
                None,
            )
        self.assertIsNone(declined.organization_id)


if __name__ == "__main__":
    unittest.main()

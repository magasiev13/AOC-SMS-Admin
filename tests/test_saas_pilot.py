import importlib
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestSaasPilotFoundation(unittest.TestCase):
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
        from app.models import (
            AppUser,
            InboxThread,
            KeywordAutomationRule,
            Organization,
            OrganizationInvitation,
            OrganizationMembership,
            OrganizationMessagingProfile,
            OrganizationSubscription,
        )
        from app.services.inbox_service import process_inbound_sms
        from app.tenant import organization_context

        self.db = db
        self.AppUser = AppUser
        self.InboxThread = InboxThread
        self.KeywordAutomationRule = KeywordAutomationRule
        self.Organization = Organization
        self.OrganizationInvitation = OrganizationInvitation
        self.OrganizationMembership = OrganizationMembership
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationSubscription = OrganizationSubscription
        self.organization_context = organization_context
        self.process_inbound_sms = process_inbound_sms

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            TWILIO_VALIDATE_INBOUND_SIGNATURE=False,
            INBOUND_AUTO_REPLY_ENABLED=True,
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_WEBHOOK_SECRET="whsec_test_123",
            SAAS_BASE_URL="https://beta.example.com",
        )
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.subscription = self.OrganizationSubscription(
            organization=self.organization,
            stripe_price_id="price_test_123",
            status="incomplete",
        )
        self.messaging_profile = self.OrganizationMessagingProfile(
            organization=self.organization,
            from_number="+15550009999",
            inbound_identity="+15550009999",
            status="active",
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
        self.db.session.add_all([self.organization, self.subscription, self.messaging_profile, self.owner])
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

    def _login_owner(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "owner@acme.test", "password": "Owner-pass1!"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_dashboard_send_requires_active_subscription(self) -> None:
        self._login_owner()

        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Hello world",
                "target": "community",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/billing/checkout", response.headers.get("Location", ""))

    def test_users_list_is_scoped_to_current_organization(self) -> None:
        other_org = self.Organization(name="Other Co", slug="other-co", status="active")
        other_user = self.AppUser(
            username="other-owner",
            email="other@org.test",
            phone="+15550000002",
            role="admin",
            must_change_password=False,
        )
        other_user.set_password("Other-pass1!")
        self.db.session.add_all([other_org, other_user])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=other_org.id,
                user_id=other_user.id,
                role="owner",
            )
        )
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"owner@acme.test", response.data)
        self.assertNotIn(b"other@org.test", response.data)

    def test_invitation_accept_creates_membership_and_redirects_owner_to_billing(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="new-owner@acme.test",
            role="owner",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        response = self.client.post(
            f"/invites/{invitation.token}",
            data={
                "username": "new-owner",
                "full_name": "New Owner",
                "phone": "+15550000003",
                "password": "Stronger-pass1!",
                "confirm_password": "Stronger-pass1!",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/billing/checkout", response.headers.get("Location", ""))
        user = self.AppUser.query.filter_by(email="new-owner@acme.test").first()
        self.assertIsNotNone(user)
        membership = self.OrganizationMembership.query.filter_by(user_id=user.id).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.organization_id, self.organization.id)
        self.assertEqual(membership.role, "owner")

    @patch("app.services.inbox_service.get_twilio_service")
    def test_inbound_sms_routes_by_destination_number(self, mock_get_twilio) -> None:
        with self.organization_context(self.organization.id):
            rule = self.KeywordAutomationRule(
                keyword="HELP",
                response_body="We are on it.",
                is_active=True,
            )
            self.db.session.add(rule)
            self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM-OUT-1",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        result = self.process_inbound_sms(
            {
                "From": "+15551234567",
                "To": "+15550009999",
                "Body": "help",
                "MessageSid": "SM-IN-ORG-1",
            }
        )

        self.assertEqual(result["status"], "keyword_reply")
        self.assertEqual(result["organization_id"], self.organization.id)
        thread = self.InboxThread.query.filter_by(phone="+15551234567").first()
        self.assertIsNotNone(thread)
        self.assertEqual(thread.organization_id, self.organization.id)


if __name__ == "__main__":
    unittest.main()

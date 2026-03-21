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
        from app.migrations.runner import run_pending_migrations

        run_pending_migrations(self.db.engine, self.app.logger)
        self.client = self.app.test_client()

        self.organization = self.Organization(name="Acme", slug="acme", status="active")
        self.subscription = self.OrganizationSubscription(
            organization=self.organization,
            stripe_price_id="price_test_123",
            status="incomplete",
        )
        self.messaging_profile = self.OrganizationMessagingProfile(
            organization=self.organization,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACsub_acme",
            messaging_service_sid="MGacme0001",
            phone_number_sid="PNacme0001",
            from_number="+15550009999",
            inbound_identity="+15550009999",
            status="active",
            provider_status="active",
            sender_review_status="approved",
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
        self.platform_admin = self.AppUser(
            username="platform-admin",
            email="platform@acme.test",
            full_name="Platform Admin",
            phone="+15550000009",
            role="admin",
            is_platform_admin=True,
            must_change_password=False,
        )
        self.platform_admin.set_password("Platform-pass1!")
        self.db.session.add_all([
            self.organization,
            self.subscription,
            self.messaging_profile,
            self.owner,
            self.platform_admin,
        ])
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

    def _login_owner(self):
        response = self.client.post(
            "/login",
            data={"username": "owner@acme.test", "password": "Owner-pass1!"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return response

    def _login_platform_admin(self):
        response = self.client.post(
            "/login",
            data={"username": "platform@acme.test", "password": "Platform-pass1!"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return response

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

    def test_platform_admin_login_redirects_to_platform_home(self) -> None:
        response = self._login_platform_admin()

        self.assertIn("/platform", response.headers.get("Location", ""))

    def test_owner_login_redirects_to_workspace_dashboard(self) -> None:
        response = self._login_owner()

        self.assertIn("/dashboard", response.headers.get("Location", ""))

    def test_platform_admin_sees_organizations_nav_link(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Platform Admin", response.data)
        self.assertIn(b"/platform/organizations", response.data)
        self.assertIn(b"bi-buildings", response.data)
        self.assertNotIn(b'href="/community"', response.data)
        self.assertNotIn(b"Search contacts", response.data)

    def test_owner_does_not_see_platform_organizations_nav_link(self) -> None:
        self._login_owner()

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"/platform/organizations", response.data)
        self.assertIn(b"Workspace", response.data)

    def test_platform_admin_is_redirected_from_workspace_dashboard(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/dashboard", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/platform", response.headers.get("Location", ""))

    def test_platform_admin_cannot_post_workspace_dashboard_actions(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            "/dashboard",
            data={"message_body": "Hello world", "target": "community"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

    def test_platform_admin_is_redirected_from_billing(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/billing", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/platform", response.headers.get("Location", ""))

    @patch("app.routes.refresh_subscription_from_stripe")
    def test_billing_overview_refreshes_incomplete_subscription(self, mock_refresh) -> None:
        def _refresh(organization, user_email):
            self.subscription.status = "trialing"
            self.subscription.stripe_customer_id = "cus_test_123"
            self.subscription.stripe_subscription_id = "sub_test_123"
            self.db.session.commit()
            return self.subscription

        mock_refresh.side_effect = _refresh

        self._login_owner()
        response = self.client.get("/billing")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"trialing", response.data)
        mock_refresh.assert_called_once_with(self.organization, "owner@acme.test")

    @patch("app.routes.sync_checkout_session_by_id")
    def test_billing_overview_syncs_checkout_session_from_success_redirect(self, mock_sync) -> None:
        def _sync(session_id, organization):
            self.subscription.status = "trialing"
            self.subscription.stripe_customer_id = "cus_test_456"
            self.subscription.stripe_subscription_id = "sub_test_456"
            self.db.session.commit()
            return self.subscription

        mock_sync.side_effect = _sync

        self._login_owner()
        response = self.client.get("/billing?session_id=cs_test_123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"trialing", response.data)
        mock_sync.assert_called_once_with("cs_test_123", self.organization)

    def test_billing_overview_shows_human_readable_status_and_onboarding(self) -> None:
        from datetime import datetime, timezone

        self.subscription.status = "trialing"
        self.subscription.current_period_end = datetime(2026, 4, 2, 12, 30, tzinfo=timezone.utc)
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/billing")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Trial active", response.data)
        self.assertIn(b"Sending access:", response.data)
        self.assertIn(b"Ready for owner testing", response.data)
        self.assertIn(b"Messaging configured", response.data)

    def test_staff_cannot_access_billing_routes(self) -> None:
        staff_user = self.AppUser(
            username="staff-user",
            email="staff@acme.test",
            full_name="Staff User",
            phone="+15550000008",
            role="social_manager",
            must_change_password=False,
        )
        staff_user.set_password("Staff-pass1!")
        self.db.session.add(staff_user)
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=staff_user.id,
                role="staff",
            )
        )
        self.db.session.commit()

        response = self.client.post(
            "/login",
            data={"username": "staff@acme.test", "password": "Staff-pass1!"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(self.client.get("/billing").status_code, 403)
        self.assertEqual(self.client.get("/billing/checkout").status_code, 403)

    @patch("app.services.billing_service._stripe_module")
    def test_refresh_subscription_from_stripe_uses_subscription_status(self, mock_stripe_module) -> None:
        from app.services.billing_service import refresh_subscription_from_stripe

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

        subscription = refresh_subscription_from_stripe(self.organization, "owner@acme.test")

        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.status, "trialing")
        self.assertEqual(subscription.stripe_customer_id, "cus_test_123")
        self.assertEqual(subscription.stripe_subscription_id, "sub_test_123")

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

    def test_users_list_shows_pending_invitation_accept_link(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="staff-invite@acme.test",
            role="staff",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/users")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Open invite", response.data)
        self.assertIn(f"https://beta.example.com/invites/{invitation.token}".encode(), response.data)

    def test_platform_organizations_list_shows_owner_invite_link_and_progress(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="new-owner@acme.test",
            role="owner",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/platform/organizations")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"core steps complete", response.data)
        self.assertIn(b"Open invite", response.data)
        self.assertIn(f"https://beta.example.com/invites/{invitation.token}".encode(), response.data)

    def test_platform_organizations_add_page_shows_admin_setup_guidance(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/platform/organizations/add")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create Business Account", response.data)
        self.assertIn(b"Platform-managed Twilio by default", response.data)
        self.assertIn(b"Twilio subaccounts and messaging services are provisioned later", response.data)

    def test_platform_organizations_add_allows_pending_messaging_profile(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            "/platform/organizations/add",
            data={
                "name": "Pending Org",
                "slug": "pending-org",
                "owner_email": "pending-owner@acme.test",
                "owner_role": "owner",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        organization = self.Organization.query.filter_by(slug="pending-org").first()
        self.assertIsNotNone(organization)
        self.assertIsNotNone(organization.messaging_profile)
        self.assertEqual(organization.messaging_profile.status, "pending")
        self.assertEqual(organization.messaging_profile.provider_mode, "platform_managed")
        self.assertEqual(organization.messaging_profile.provider_status, "pending")
        self.assertEqual(organization.messaging_profile.sender_review_status, "pending")
        self.assertIsNone(organization.messaging_profile.from_number)
        self.assertIsNone(organization.messaging_profile.messaging_service_sid)

    def test_platform_organizations_messaging_edit_requires_provider_provisioning_before_sender_assignment(self) -> None:
        self._login_platform_admin()

        self.messaging_profile.messaging_service_sid = None
        self.messaging_profile.provider_status = "pending"
        self.db.session.commit()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "save",
                "sender_number": "+15550001234",
                "phone_number_sid": "PNpending123",
                "sender_review_status": "approved",
                "consent_acknowledged": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Provision the Twilio provider before assigning a sender.",
            response.data,
        )
        self.db.session.refresh(self.messaging_profile)
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertEqual(self.messaging_profile.from_number, "+15550009999")

    @patch("app.routes.sync_sender_assignment")
    @patch("app.routes.release_sender")
    def test_platform_organizations_messaging_edit_can_release_and_activate_sender(self, mock_release_sender, mock_sync_sender_assignment) -> None:
        other_org = self.Organization(name="Other Co", slug="other-co", status="active")
        other_subscription = self.OrganizationSubscription(
            organization=other_org,
            stripe_price_id="price_test_123",
            status="incomplete",
        )
        other_messaging_profile = self.OrganizationMessagingProfile(
            organization=other_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACsub_other",
            messaging_service_sid="MGother0001",
            status="pending",
            provider_status="pending",
        )
        self.db.session.add_all([other_org, other_subscription, other_messaging_profile])
        self.db.session.commit()

        def _release_side_effect(organization_id, actor_user_id=None):
            profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).first()
            profile.from_number = None
            profile.phone_number_sid = None
            profile.inbound_identity = profile.messaging_service_sid
            profile.set_provider_status("pending")
            self.db.session.commit()
            return profile

        def _sync_side_effect(organization_id, actor_user_id=None):
            profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).first()
            profile.inbound_identity = profile.from_number
            profile.set_provider_status("active")
            self.db.session.commit()
            return profile

        mock_release_sender.side_effect = _release_side_effect
        mock_sync_sender_assignment.side_effect = _sync_side_effect

        self._login_platform_admin()

        clear_response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "release_sender",
            },
            follow_redirects=False,
        )
        self.assertEqual(clear_response.status_code, 302)

        self.db.session.refresh(self.messaging_profile)
        self.assertEqual(self.messaging_profile.status, "pending")
        self.assertEqual(self.messaging_profile.provider_status, "pending")
        self.assertIsNone(self.messaging_profile.from_number)
        self.assertEqual(self.messaging_profile.inbound_identity, "MGacme0001")

        assign_response = self.client.post(
            f"/platform/organizations/{other_org.id}/messaging",
            data={
                "action": "save",
                "sender_number": "+15550009999",
                "phone_number_sid": "PN1234567890ABCDE",
                "sender_review_status": "approved",
                "consent_acknowledged": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(assign_response.status_code, 302)

        self.db.session.refresh(other_messaging_profile)
        self.assertEqual(other_messaging_profile.status, "active")
        self.assertEqual(other_messaging_profile.provider_status, "active")
        self.assertEqual(other_messaging_profile.from_number, "+15550009999")
        self.assertEqual(other_messaging_profile.messaging_service_sid, "MGother0001")
        self.assertEqual(other_messaging_profile.phone_number_sid, "PN1234567890ABCDE")
        self.assertEqual(other_messaging_profile.inbound_identity, "+15550009999")

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

    def test_invitation_accept_allows_phone_reuse_after_schema_migration(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="phone-reuse@acme.test",
            role="staff",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        existing_user = self.AppUser(
            username="existing-user",
            email="existing@acme.test",
            full_name="Existing User",
            phone="+15550000003",
            role="social_manager",
            must_change_password=False,
        )
        existing_user.set_password("Existing-pass1!")
        self.db.session.add(existing_user)
        self.db.session.commit()

        response = self.client.post(
            f"/invites/{invitation.token}",
            data={
                "username": "new-staff",
                "full_name": "New Staff",
                "phone": "+15550000003",
                "password": "Stronger-pass1!",
                "confirm_password": "Stronger-pass1!",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers.get("Location", ""))
        user = self.AppUser.query.filter_by(email="phone-reuse@acme.test").first()
        self.assertIsNotNone(user)
        self.assertEqual(user.phone, "+15550000003")
        self.assertEqual(
            self.OrganizationMembership.query.filter_by(user_id=user.id).count(),
            1,
        )

    def test_invitation_accept_rejects_phone_reuse_within_same_organization(self) -> None:
        existing_user = self.AppUser(
            username="existing-staff",
            email="existing-staff@acme.test",
            full_name="Existing Staff",
            phone="+15550000006",
            role="social_manager",
            must_change_password=False,
        )
        existing_user.set_password("Existing-pass1!")
        self.db.session.add(existing_user)
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=existing_user.id,
                role="staff",
            )
        )
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="duplicate-phone@acme.test",
            role="staff",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        response = self.client.post(
            f"/invites/{invitation.token}",
            data={
                "username": "duplicate-phone",
                "full_name": "Duplicate Phone",
                "phone": "+15550000006",
                "password": "Stronger-pass1!",
                "confirm_password": "Stronger-pass1!",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"That phone number is already assigned to another user in this organization.",
            response.data,
        )
        self.assertIsNone(self.AppUser.query.filter_by(email="duplicate-phone@acme.test").first())

    def test_invitation_accept_handles_naive_expiration_timestamps(self) -> None:
        from datetime import datetime, timedelta, timezone

        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="expired@acme.test",
            role="staff",
            status="pending",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        response = self.client.post(
            f"/invites/{invitation.token}",
            data={
                "username": "expired-user",
                "full_name": "Expired User",
                "phone": "+15550000004",
                "password": "Stronger-pass1!",
                "confirm_password": "Stronger-pass1!",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        refreshed = self.OrganizationInvitation.query.filter_by(token=invitation.token).first()
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.status, "expired")

    def test_team_invite_accepts_owner_staff_language(self) -> None:
        self.subscription.status = "trialing"
        self.db.session.commit()

        self._login_owner()
        response = self.client.post(
            "/team/invite",
            data={
                "email": "new-staff@acme.test",
                "role": "staff",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        invite = self.OrganizationInvitation.query.filter_by(email="new-staff@acme.test").first()
        self.assertIsNotNone(invite)
        self.assertEqual(invite.role, "staff")

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

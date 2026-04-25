import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, tuple[str, int | None]] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._values:
            return False
        normalized = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        self._values[key] = (normalized, ex)
        return True

    def get(self, key):
        entry = self._values.get(key)
        if entry is None:
            return None
        return entry[0]

    def ttl(self, key):
        entry = self._values.get(key)
        if entry is None:
            return -2
        return entry[1] if entry[1] is not None else -1

    def delete(self, key):
        self._values.pop(key, None)
        return 1


class TestSaasPilotFoundation(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = {
            "FLASK_DEBUG": os.environ.get("FLASK_DEBUG"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
            "SAAS_MODE": os.environ.get("SAAS_MODE"),
            "STRIPE_FAKE_CHECKOUT_ENABLED": os.environ.get("STRIPE_FAKE_CHECKOUT_ENABLED"),
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
            AuthEvent,
            CommunityMember,
            Event,
            InboxThread,
            KeywordAutomationRule,
            MessageLog,
            MessagingUsageRecord,
            Organization,
            OrganizationA2POnboarding,
            OrganizationInvitation,
            OrganizationMembership,
            OrganizationMessagingProfile,
            OrganizationProviderAuditLog,
            OrganizationTestRecipient,
            PlatformServiceRestartRequest,
            OrganizationSubscription,
        )
        from app.services.inbox_service import process_inbound_sms
        from app.tenant import organization_context

        self.db = db
        self.AppUser = AppUser
        self.AuthEvent = AuthEvent
        self.CommunityMember = CommunityMember
        self.Event = Event
        self.InboxThread = InboxThread
        self.KeywordAutomationRule = KeywordAutomationRule
        self.MessageLog = MessageLog
        self.MessagingUsageRecord = MessagingUsageRecord
        self.Organization = Organization
        self.OrganizationA2POnboarding = OrganizationA2POnboarding
        self.OrganizationInvitation = OrganizationInvitation
        self.OrganizationMembership = OrganizationMembership
        self.OrganizationMessagingProfile = OrganizationMessagingProfile
        self.OrganizationProviderAuditLog = OrganizationProviderAuditLog
        self.OrganizationTestRecipient = OrganizationTestRecipient
        self.PlatformServiceRestartRequest = PlatformServiceRestartRequest
        self.OrganizationSubscription = OrganizationSubscription
        self.organization_context = organization_context
        self.process_inbound_sms = process_inbound_sms

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            TWILIO_VALIDATE_INBOUND_SIGNATURE=False,
            INBOUND_AUTO_REPLY_ENABLED=True,
            TWILIO_A2P_ONBOARDING_ENABLED=True,
            TWILIO_PRIMARY_CUSTOMER_PROFILE_SID="BUprimary123",
            STRIPE_SECRET_KEY="sk_test_123",
            STRIPE_PRICE_ID="price_test_123",
            STRIPE_ACTIVATION_PRICE_ID="price_activation_123",
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
        return self._login_with_credentials("owner@acme.test", "Owner-pass1!")

    def _login_platform_admin(self):
        return self._login_with_credentials("platform@acme.test", "Platform-pass1!")

    def _login_with_credentials(self, username: str, password: str):
        response = self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return response

    def _logout(self):
        response = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        return response

    def _create_support_organization(
        self,
        *,
        name: str = "Recovery Co",
        slug: str = "recovery-co",
        owner_email: str = "pending-owner@acme.test",
    ):
        organization = self.Organization(name=name, slug=slug, status="active")
        subscription = self.OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status="incomplete",
        )
        messaging_profile = self.OrganizationMessagingProfile(
            organization=organization,
            provider_mode="platform_managed",
            status="pending",
            provider_status="pending",
            sender_review_status="pending",
        )
        invitation = self.OrganizationInvitation(
            organization=organization,
            email=owner_email,
            role="owner",
            status="pending",
            invited_by_user_id=self.platform_admin.id,
        )
        self.db.session.add_all([organization, subscription, messaging_profile, invitation])
        self.db.session.commit()
        return organization, invitation

    def _create_customer_managed_workspace(
        self,
        *,
        name: str = "Customer Managed Co",
        slug: str = "customer-managed-co",
        username: str = "customer-managed-owner",
        email: str = "customer-managed-owner@acme.test",
        password: str = "CustomerManaged-pass1!",
        role: str = "owner",
        subscription_status: str = "complimentary",
        provider_status: str = "pending",
        can_send: bool = False,
    ):
        organization = self.Organization(name=name, slug=slug, status="active")
        subscription = self.OrganizationSubscription(
            organization=organization,
            stripe_price_id="price_test_123",
            status=subscription_status,
        )
        messaging_profile = self.OrganizationMessagingProfile(
            organization=organization,
            provider_mode="customer_managed",
            twilio_account_sid="ACcust0001" if can_send else None,
            messaging_service_sid="MGcust0001" if can_send else None,
            phone_number_sid="PNcust0001" if can_send else None,
            from_number="+15550001111" if can_send else None,
            inbound_identity="+15550001111" if can_send else None,
            status=provider_status,
            provider_status=provider_status,
            sender_review_status="approved" if can_send else "pending",
            consent_acknowledged_at=datetime.utcnow() if can_send else None,
        )
        user = self.AppUser(
            username=username,
            email=email,
            full_name="Customer Managed User",
            phone="+15550001234",
            role="admin",
            must_change_password=False,
        )
        user.set_password(password)
        self.db.session.add_all([organization, subscription, messaging_profile, user])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role=role,
            )
        )
        self.db.session.commit()
        return organization, subscription, messaging_profile, user

    def test_validate_org_messaging_profile_input_rejects_case_insensitive_duplicate_service_sid(self) -> None:
        from app.routes import _validate_org_messaging_profile_input

        _, _, messaging_profile, _ = self._create_customer_managed_workspace(
            slug="customer-managed-duplicate-service",
            username="customer-managed-duplicate-service",
            email="customer-managed-duplicate-service@acme.test",
        )
        messaging_profile.messaging_service_sid = "MGcustAbc123"
        messaging_profile.inbound_identity = "MGcustAbc123"
        self.db.session.commit()

        error, normalized_sender, normalized_service_sid = _validate_org_messaging_profile_input(
            None,
            "MGCUSTABC123",
        )

        self.assertEqual(
            error,
            "That Twilio Messaging Service SID is already assigned to another organization.",
        )
        self.assertIsNone(normalized_sender)
        self.assertIsNone(normalized_service_sid)

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
        self.assertIn("/setup", response.headers.get("Location", ""))
        self.assertNotIn("/billing/checkout", response.headers.get("Location", ""))

    def test_platform_admin_login_redirects_to_platform_home(self) -> None:
        response = self._login_platform_admin()

        self.assertIn("/platform", response.headers.get("Location", ""))

    def test_owner_login_redirects_to_setup_when_workspace_is_incomplete(self) -> None:
        response = self._login_owner()

        self.assertIn("/setup", response.headers.get("Location", ""))

    def test_customer_managed_owner_login_redirects_to_dashboard_when_workspace_is_ready(self) -> None:
        _, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-ready",
            username="customer-managed-ready",
            email="customer-managed-ready@acme.test",
            provider_status="active",
            can_send=True,
        )

        response = self._login_with_credentials(user.email, "CustomerManaged-pass1!")

        self.assertIn("/dashboard", response.headers.get("Location", ""))

    def test_customer_managed_owner_login_redirects_to_setup_when_provider_is_pending(self) -> None:
        _, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-pending",
            username="customer-managed-pending",
            email="customer-managed-pending@acme.test",
        )

        response = self._login_with_credentials(user.email, "CustomerManaged-pass1!")

        self.assertIn("/setup", response.headers.get("Location", ""))

    def test_customer_managed_staff_login_redirects_to_setup_pending_when_provider_is_pending(self) -> None:
        _, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-staff",
            username="customer-managed-staff",
            email="customer-managed-staff@acme.test",
            role="staff",
        )

        response = self._login_with_credentials(user.email, "CustomerManaged-pass1!")

        self.assertIn("/setup/pending", response.headers.get("Location", ""))

    def test_platform_login_rejects_workspace_owner_credentials(self) -> None:
        response = self.client.post(
            "/platform/login",
            data={"username": "owner@acme.test", "password": "Owner-pass1!"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Use the workspace login to access your organization account.", response.data)

    def test_signup_creates_workspace_owner_and_redirects_to_setup(self) -> None:
        response = self.client.post(
            "/signup",
            data={
                "organization_name": "Beta Bakery",
                "full_name": "Beta Owner",
                "email": "beta-owner@acme.test",
                "username": "beta-owner",
                "phone": "+15550000077",
                "password": "Signup-pass1!",
                "confirm_password": "Signup-pass1!",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/setup", response.headers.get("Location", ""))
        organization = self.Organization.query.filter_by(slug="beta-bakery").first()
        self.assertIsNotNone(organization)
        self.assertIsNotNone(organization.subscription)
        self.assertEqual(organization.subscription.status, "incomplete")
        self.assertIsNotNone(organization.messaging_profile)
        self.assertEqual(organization.messaging_profile.provider_mode, "platform_managed")
        self.assertIsNotNone(organization.a2p_onboarding)
        self.assertEqual(organization.a2p_onboarding.onboarding_status, "draft")
        self.assertEqual(organization.a2p_onboarding.number_strategy, "auto_buy")
        recipients = self.OrganizationTestRecipient.query.filter_by(
            organization_id=organization.id
        ).all()
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].phone, "+15550000077")
        self.assertEqual(recipients[0].label, "Beta Owner")

    def test_owner_setup_defaults_new_org_number_strategy_to_auto_buy(self) -> None:
        self._login_owner()

        response = self.client.get("/setup?step=compliance")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="number_strategy" value="auto_buy"', response.data)

    def test_customer_managed_owner_setup_is_read_only_and_does_not_create_a2p_draft(self) -> None:
        organization, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-read-only",
            username="customer-managed-read-only",
            email="customer-managed-read-only@acme.test",
        )
        self.assertIsNone(
            self.OrganizationA2POnboarding.query.filter_by(organization_id=organization.id).first()
        )

        self._login_with_credentials(user.email, "CustomerManaged-pass1!")
        response = self.client.get("/setup")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"External Twilio activation", response.data)
        self.assertIn(b"Workspace owners are read-only here", response.data)
        self.assertNotIn(b"Legal business name", response.data)
        self.assertNotIn(b"Submit for Twilio review", response.data)
        self.assertIsNone(
            self.OrganizationA2POnboarding.query.filter_by(organization_id=organization.id).first()
        )

    def test_owner_setup_review_surfaces_rejected_a2p_failure_details(self) -> None:
        self.subscription.status = "active"
        self.messaging_profile.status = "error"
        self.messaging_profile.provider_status = "error"
        self.messaging_profile.sender_review_status = "pending"
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="rejected",
            brand_status="approved",
            campaign_status="failed",
            business_name="Acme LLC",
            business_type="Limited Liability Corporation",
            business_industry="Technology",
            business_registration_identifier="EIN",
            business_registration_number_encrypted="encrypted-ein",
            business_regions_json='["USA_AND_CANADA"]',
            website_url="https://app.example.com/acme",
            email="owner@acme.test",
            notification_email="owner@acme.test",
            first_name="Owner",
            last_name="User",
            business_title="Owner",
            job_position="CEO",
            address_country="US",
            address_line1="1 Main Street",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Transactional reminders and support updates.",
            message_flow="Customers opt in on the Acme website before receiving reminders and support updates. Reply STOP to opt out and HELP for help.",
            message_samples_json='["Acme: Your reminder is ready. Reply STOP to opt out."]',
            privacy_policy_url="https://app.example.com/compliance/acme/sms/privacy",
            terms_and_conditions_url="https://app.example.com/compliance/acme/sms/terms",
            cta_proof_url="https://app.example.com/compliance/acme/sms/opt-in",
            failure_code="30909",
            last_error="CTA could not be verified.",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/setup")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Latest provider issue", response.data)
        self.assertIn(b"Twilio code:", response.data)
        self.assertIn(b"30909", response.data)
        self.assertIn(b"CTA could not be verified.", response.data)
        self.assertIn(b"Resubmit for Twilio review", response.data)

        launch_response = self.client.get("/setup?step=launch")

        self.assertEqual(launch_response.status_code, 200)
        self.assertIn(b"Needs packet correction", launch_response.data)
        self.assertIn(b"Fix the CTA and compliance details before resubmitting.", launch_response.data)
        self.assertNotIn(b"Twilio review is in progress.", launch_response.data)

    def test_owner_setup_launch_shows_recent_twilio_activity_and_sender_assignment_guidance(self) -> None:
        self.subscription.status = "active"
        self.messaging_profile.status = "pending"
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_review_status = "pending"
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        approved_at = datetime.utcnow()
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            approved_at=approved_at,
            brand_status="approved",
            campaign_status="approved",
            campaign_sid="QEapproved123",
            brand_registration_sid="BNapproved123",
            registration_path="low_volume_standard",
            number_strategy="platform_assign",
            business_name="Acme LLC",
            email="owner@acme.test",
            first_name="Owner",
            last_name="User",
            campaign_description="Transactional reminders and support updates.",
            message_flow="Customers opt in on the Acme website before receiving reminders and support updates. Reply STOP to opt out and HELP for help.",
            message_samples_json='["Acme: Your reminder is ready.", "Acme: Your appointment is confirmed."]',
            privacy_policy_url="https://app.example.com/compliance/acme/sms/privacy",
            terms_and_conditions_url="https://app.example.com/compliance/acme/sms/terms",
            cta_proof_url="https://app.example.com/compliance/acme/sms/opt-in",
        )
        self.db.session.add(onboarding)
        self.db.session.add(
            self.OrganizationProviderAuditLog(
                organization_id=self.organization.id,
                action="a2p_submit",
                status="success",
                message="Queued Twilio A2P onboarding (low_volume_standard).",
                metadata_json=json.dumps(
                    {
                        "campaign_use_case": "ACCOUNT_NOTIFICATION",
                        "messaging_service_sid": self.messaging_profile.messaging_service_sid,
                        "submission_source_mode": "hosted_fallback",
                    }
                ),
            )
        )
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/setup?step=launch")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Await sender assignment", response.data)
        self.assertIn(b"Save the target PN SID from the org Twilio subaccount", response.data)
        self.assertIn(b"Recent Twilio activity", response.data)
        self.assertIn(b"Submitted to Twilio", response.data)
        self.assertIn(b"Hosted fallback", response.data)

    def test_owner_setup_launch_marks_first_smoke_test_passed_after_successful_send(self) -> None:
        self.subscription.status = "active"
        approved_at = datetime.utcnow() - timedelta(hours=1)
        self.messaging_profile.status = "active"
        self.messaging_profile.provider_status = "active"
        self.messaging_profile.sender_review_status = "approved"
        self.messaging_profile.from_number = "+15550009999"
        self.messaging_profile.phone_number_sid = "PNacme0001"
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            approved_at=approved_at,
            brand_status="approved",
            campaign_status="approved",
            business_name="Acme LLC",
            email="owner@acme.test",
            first_name="Owner",
            last_name="User",
            campaign_description="Transactional reminders and support updates.",
            message_flow="Customers opt in on the Acme website before receiving reminders and support updates. Reply STOP to opt out and HELP for help.",
            message_samples_json='["Acme: Your reminder is ready.", "Acme: Your appointment is confirmed."]',
            privacy_policy_url="https://app.example.com/compliance/acme/sms/privacy",
            terms_and_conditions_url="https://app.example.com/compliance/acme/sms/terms",
            cta_proof_url="https://app.example.com/compliance/acme/sms/opt-in",
        )
        self.db.session.add(onboarding)
        self.db.session.add(
            self.MessageLog(
                organization_id=self.organization.id,
                created_at=approved_at + timedelta(minutes=5),
                message_body="Internal launch smoke test",
                target="community",
                status="sent",
                total_recipients=1,
                success_count=1,
                failure_count=0,
            )
        )
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/setup?step=launch")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Workspace is live", response.data)
        self.assertIn(b"First smoke test", response.data)
        self.assertIn(b"Passed", response.data)

    def test_owner_setup_compliance_step_surfaces_rejected_a2p_failure_details(self) -> None:
        self.subscription.status = "active"
        self.messaging_profile.status = "error"
        self.messaging_profile.provider_status = "error"
        self.messaging_profile.sender_review_status = "pending"
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="rejected",
            brand_status="approved",
            campaign_status="failed",
            business_name="Acme LLC",
            business_type="Limited Liability Corporation",
            business_industry="Technology",
            business_registration_identifier="EIN",
            website_url="https://app.example.com/acme",
            email="owner@acme.test",
            notification_email="owner@acme.test",
            first_name="Owner",
            last_name="User",
            business_title="Owner",
            job_position="CEO",
            address_country="US",
            address_line1="1 Main Street",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Transactional reminders and support updates.",
            message_flow="Customers opt in on the Acme website before receiving reminders and support updates. Reply STOP to opt out and HELP for help.",
            message_samples_json='["Acme: Your reminder is ready. Reply STOP to opt out."]',
            failure_code="30909",
            last_error="CTA could not be verified.",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/setup")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Latest provider issue", response.data)
        self.assertIn(b"Twilio code:", response.data)
        self.assertIn(b"30909", response.data)
        self.assertIn(b"CTA could not be verified.", response.data)
        self.assertIn(b"https://app.example.com/compliance/acme/sms/privacy", response.data)
        self.assertIn(b"https://app.example.com/compliance/acme/sms/terms", response.data)
        self.assertIn(b"https://app.example.com/compliance/acme/sms/opt-in", response.data)

    def test_customer_managed_setup_rejects_platform_managed_a2p_submission_actions(self) -> None:
        organization, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-guardrail",
            username="customer-managed-guardrail",
            email="customer-managed-guardrail@acme.test",
        )

        self._login_with_credentials(user.email, "CustomerManaged-pass1!")
        response = self.client.post(
            "/setup",
            data={"action": "save_compliance"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Customer-managed Twilio activation is handled by your platform admin.", response.data)
        self.assertIn(b"External Twilio activation", response.data)
        self.assertIsNone(
            self.OrganizationA2POnboarding.query.filter_by(organization_id=organization.id).first()
        )

    def test_customer_managed_staff_pending_setup_mentions_external_activation(self) -> None:
        _, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-pending-copy",
            username="customer-managed-pending-copy",
            email="customer-managed-pending-copy@acme.test",
            role="staff",
        )

        self._login_with_credentials(user.email, "CustomerManaged-pass1!")
        response = self.client.get("/setup/pending")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"customer-managed Twilio connection", response.data)
        self.assertIn(b"External messaging:", response.data)
        self.assertNotIn(b"Legal business name", response.data)
        self.assertNotIn(b"Submit for Twilio review", response.data)

    def test_setup_status_payload_for_platform_managed_owner_exposes_billing_step_contract(self) -> None:
        self._login_owner()

        response = self.client.get("/setup/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["current_step"], "billing")
        self.assertFalse(payload["setup_complete"])
        self.assertEqual(payload["subscription"]["status"], "incomplete")
        self.assertFalse(payload["subscription"]["can_send"])
        self.assertEqual(payload["messaging"]["provider_mode"], "platform_managed")
        self.assertIn("heading", payload["launch_readiness"])
        self.assertIn("summary", payload["onboarding"])

    def test_setup_status_payload_for_customer_managed_workspace_exposes_provider_state(self) -> None:
        _, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-status",
            username="customer-managed-status",
            email="customer-managed-status@acme.test",
        )

        self._login_with_credentials(user.email, "CustomerManaged-pass1!")
        response = self.client.get("/setup/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["current_step"], "provider")
        self.assertFalse(payload["setup_complete"])
        self.assertTrue(payload["onboarding"]["external_managed"])
        self.assertEqual(payload["messaging"]["provider_mode"], "customer_managed")
        self.assertEqual(payload["messaging"]["provider_status"], "pending")

    def test_setup_status_payload_is_available_to_platform_managed_staff_pending_setup(self) -> None:
        staff_user = self.AppUser(
            username="pending-staff",
            email="pending-staff@acme.test",
            full_name="Pending Staff",
            phone="+15550000055",
            role="social_manager",
            must_change_password=False,
        )
        staff_user.set_password("PendingStaff-pass1!")
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

        self._login_with_credentials(staff_user.email, "PendingStaff-pass1!")
        response = self.client.get("/setup/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["current_step"], "billing")
        self.assertFalse(payload["setup_complete"])
        self.assertEqual(payload["messaging"]["provider_mode"], "platform_managed")

    def test_platform_managed_invalid_setup_step_falls_back_to_current_step(self) -> None:
        self._login_owner()

        response = self.client.get("/setup?step=provider")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-current-step="billing"', response.data)
        self.assertIn(b"Activate billing", response.data)

    def test_customer_managed_invalid_setup_step_falls_back_to_provider(self) -> None:
        _, _, _, user = self._create_customer_managed_workspace(
            slug="customer-managed-invalid-step",
            username="customer-managed-invalid-step",
            email="customer-managed-invalid-step@acme.test",
        )

        self._login_with_credentials(user.email, "CustomerManaged-pass1!")
        response = self.client.get("/setup?step=compliance")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-current-step="provider"', response.data)
        self.assertIn(b"External Twilio activation", response.data)

    def test_setup_pending_redirects_owner_back_to_setup(self) -> None:
        self._login_owner()

        response = self.client.get("/setup/pending", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/setup", response.headers.get("Location", ""))

    def test_suspended_organization_owner_cannot_log_in(self) -> None:
        self.organization.status = "suspended"
        self.db.session.commit()

        response = self.client.post(
            "/login",
            data={"username": "owner@acme.test", "password": "Owner-pass1!"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your organization is currently suspended. Contact your platform admin.", response.data)
        self.assertIn(b"Login", response.data)
        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(dashboard.status_code, 302)
        self.assertIn("/login", dashboard.headers.get("Location", ""))

    def test_suspended_organization_invalidates_existing_session(self) -> None:
        self._login_owner()
        self.organization.status = "suspended"
        self.db.session.commit()

        response = self.client.get("/dashboard", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your organization is currently suspended. Contact your platform admin.", response.data)
        self.assertIn(b"Login", response.data)

    def test_platform_admin_sees_organizations_nav_link(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Platform Admin", response.data)
        self.assertIn(b"/platform/organizations", response.data)
        self.assertIn(b"bi-buildings", response.data)
        self.assertNotIn(b'href="/community"', response.data)
        self.assertNotIn(b"Search contacts", response.data)
        self.assertNotIn(b"Restart SaaS Services", response.data)

    def test_platform_admin_sees_restart_control_only_when_enabled(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        self._login_platform_admin()

        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Needs attention", response.data)
        self.assertIn(b"System utilities", response.data)
        self.assertNotIn(b"Platform Admin", response.data)
        self.assertNotIn(b"Manage organizations, onboarding, and provider readiness.", response.data)
        self.assertIn(b"Restart SaaS Services", response.data)
        self.assertIn(b"/platform/operations/restart-services", response.data)

    def test_platform_home_prioritizes_billing_blocker_headline(self) -> None:
        blocked_org = self.Organization(name="Billing Blocked Co", slug="billing-blocked-co", status="active")
        blocked_subscription = self.OrganizationSubscription(
            organization=blocked_org,
            stripe_customer_id="cus_blocked_test",
            stripe_subscription_id="sub_blocked_test",
            stripe_price_id="price_test_123",
            status="past_due",
        )
        blocked_messaging = self.OrganizationMessagingProfile(
            organization=blocked_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACblocked0001",
            messaging_service_sid="MGblocked0001",
            phone_number_sid="PNblocked0001",
            from_number="+15550007777",
            inbound_identity="+15550007777",
            status="active",
            provider_status="active",
            sender_review_status="approved",
        )
        self.db.session.add_all([blocked_org, blocked_subscription, blocked_messaging])
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Billing Blocked Co", response.data)
        self.assertGreaterEqual(
            response.data.count(b"Open the billing portal and resolve the payment issue."),
            2,
        )

    def test_platform_home_prioritizes_messaging_blocker_headline(self) -> None:
        blocked_org = self.Organization(name="Messaging Blocked Co", slug="messaging-blocked-co", status="active")
        blocked_subscription = self.OrganizationSubscription(
            organization=blocked_org,
            stripe_customer_id="cus_message_test",
            stripe_subscription_id="sub_message_test",
            stripe_price_id="price_test_123",
            status="active",
        )
        blocked_messaging = self.OrganizationMessagingProfile(
            organization=blocked_org,
            provider_mode="platform_managed",
            twilio_subaccount_sid="ACmessage0001",
            messaging_service_sid="MGmessage0001",
            status="error",
            provider_status="error",
            sender_review_status="pending",
            last_provision_error="Twilio A2P onboarding could not be queued. Check Redis/RQ and retry.",
        )
        self.db.session.add_all([blocked_org, blocked_subscription, blocked_messaging])
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Messaging Blocked Co", response.data)
        self.assertGreaterEqual(
            response.data.count(b"Twilio A2P onboarding could not be queued. Check Redis/RQ and retry."),
            2,
        )

    def test_owner_does_not_see_platform_organizations_nav_link(self) -> None:
        self._login_owner()

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"/platform/organizations", response.data)
        self.assertIn(b"Workspace", response.data)
        self.assertNotIn(b"Restart SaaS Services", response.data)

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
        self.subscription.stripe_customer_id = "cus_incomplete_seed"
        self.db.session.commit()

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
        self.assertIn(b"Trial active", response.data)
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
        self.assertIn(b"Trial active", response.data)
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
        self.assertIn(b"Sending enabled", response.data)
        self.assertIn(b"Starter", response.data)
        self.assertIn(b"1,000 SMS segments", response.data)
        self.assertIn(b"$0.03 per segment", response.data)
        self.assertIn(b"Paid", response.data)
        self.assertIn(b"Ready for owner testing", response.data)
        self.assertIn(b"Live SMS approved", response.data)

    def test_billing_overview_hides_stripe_actions_for_complimentary_workspace(self) -> None:
        self.subscription.status = "complimentary"
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/billing")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Complimentary billing", response.data)
        self.assertIn(b"Twilio charges stay on the customer-managed account.", response.data)
        self.assertNotIn(b"Start Subscription", response.data)
        self.assertNotIn(b"Open Billing Portal", response.data)

    def test_owner_dashboard_shows_pending_a2p_launchpad_when_messaging_is_not_live(self) -> None:
        self.subscription.status = "trialing"
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.status = "pending"
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="pending",
            business_name="Acme",
            business_type="LLC",
            email="ops@acme.test",
            first_name="Jamie",
            last_name="Owner",
            campaign_use_case="ACCOUNT_NOTIFICATION",
            campaign_description="Account reminders",
            message_flow="Users opt in on the website and reply STOP to unsubscribe.",
            message_samples_json='["Reminder one", "Reminder two"]',
            brand_status="pending-review",
            campaign_status="submitted",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Carrier review in progress", response.data)
        self.assertIn(b"Live SMS is paused.", response.data)
        self.assertIn(b"Configure keyword automation", response.data)
        self.assertIn(b"SMS Pending Approval", response.data)

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

    @patch("app.routes.create_checkout_session")
    def test_billing_checkout_get_redirects_to_overview_without_creating_session(self, mock_create_checkout) -> None:
        self._login_owner()

        response = self.client.get("/billing/checkout", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/billing", response.headers.get("Location", ""))
        self.assertNotIn("/billing/checkout", response.headers.get("Location", ""))
        mock_create_checkout.assert_not_called()

    @patch("app.routes.create_checkout_session")
    def test_billing_checkout_post_creates_session(self, mock_create_checkout) -> None:
        mock_create_checkout.return_value = SimpleNamespace(url="https://checkout.stripe.test/session")

        self._login_owner()
        response = self.client.post("/billing/checkout", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("Location"), "https://checkout.stripe.test/session")
        mock_create_checkout.assert_called_once()
        args = mock_create_checkout.call_args.args
        self.assertEqual(args[0].id, self.organization.id)
        self.assertEqual(args[1], "owner@acme.test")
        self.assertIn("session_id={CHECKOUT_SESSION_ID}", args[2])
        self.assertTrue(args[3].endswith("/setup?step=billing"))

    def test_billing_checkout_post_redirects_complimentary_workspace_to_dashboard(self) -> None:
        self.subscription.status = "complimentary"
        self.db.session.commit()

        self._login_owner()
        response = self.client.post("/billing/checkout", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers.get("Location", ""))

    def test_fake_checkout_route_is_disabled_by_default(self) -> None:
        self._login_owner()

        response = self.client.get(
            f"/_test/stripe/checkout/cs_fake_org_{self.organization.id}?organization_id={self.organization.id}",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 404)

    def test_setup_checkout_uses_fake_checkout_route_when_enabled(self) -> None:
        self.app.config["STRIPE_FAKE_CHECKOUT_ENABLED"] = True
        self._login_owner()

        response = self.client.post("/setup/billing/checkout", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertIn(f"/_test/stripe/checkout/cs_fake_org_{self.organization.id}", response.headers.get("Location", ""))
        self.assertIn(f"organization_id={self.organization.id}", response.headers.get("Location", ""))

    def test_fake_checkout_completion_marks_subscription_trialing(self) -> None:
        self.app.config["STRIPE_FAKE_CHECKOUT_ENABLED"] = True
        self.app.config["BILLING_TRIAL_DAYS"] = 14
        self._login_owner()

        response = self.client.post(
            f"/_test/stripe/checkout/cs_fake_org_{self.organization.id}",
            data={
                "organization_id": str(self.organization.id),
                "success_url": "http://localhost/setup?step=billing&session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": "http://localhost/setup?step=billing",
                "action": "complete",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Trial active", response.data)
        self.assertIn(b'data-current-step="billing"', response.data)
        self.assertEqual(self.organization.subscription.status, "trialing")
        self.assertIsNotNone(self.organization.subscription.current_period_end)

    def test_cross_tenant_event_detail_is_not_accessible(self) -> None:
        second_org = self.Organization(name="Second Org", slug="second-org", status="active")
        second_subscription = self.OrganizationSubscription(
            organization=second_org,
            stripe_price_id="price_test_123",
            status="active",
        )
        second_messaging = self.OrganizationMessagingProfile(
            organization=second_org,
            provider_mode="platform_managed",
            from_number="+15550000123",
            inbound_identity="+15550000123",
            messaging_service_sid="MGsecond0001",
            phone_number_sid="PNsecond0001",
            status="active",
            provider_status="active",
            sender_review_status="approved",
        )
        second_owner = self.AppUser(
            username="second-owner",
            email="second-owner@acme.test",
            full_name="Second Owner",
            phone="+15550000022",
            role="admin",
            must_change_password=False,
        )
        second_owner.set_password("SecondOwner-pass1!")
        second_event = self.Event(
            organization=second_org,
            title="Second Org Event",
            date=datetime(2026, 4, 10).date(),
        )
        self.db.session.add_all([second_org, second_subscription, second_messaging, second_owner, second_event])
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=second_org.id,
                user_id=second_owner.id,
                role="owner",
            )
        )
        self.db.session.commit()

        self._login_owner()
        response = self.client.get(f"/events/{second_event.id}", follow_redirects=False)

        self.assertEqual(response.status_code, 404)

    def test_staff_does_not_see_restart_control(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        staff_user = self.AppUser(
            username="staff-viewer",
            email="staff-viewer@acme.test",
            full_name="Staff Viewer",
            phone="+15550000013",
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
            data={"username": "staff-viewer@acme.test", "password": "Staff-pass1!"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        dashboard_response = self.client.get("/dashboard")
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertNotIn(b"Restart SaaS Services", dashboard_response.data)

    @patch("app.services.billing_service._stripe_module")
    def test_refresh_subscription_from_stripe_uses_subscription_status(self, mock_stripe_module) -> None:
        from datetime import timedelta

        from app.services.billing_service import refresh_subscription_from_stripe

        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.list.return_value.data = [
            {
                "id": "cs_test_123",
                "created": int((self.subscription.created_at + timedelta(minutes=1)).timestamp()),
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

    @patch("app.services.billing_service._stripe_module")
    def test_refresh_subscription_from_stripe_ignores_checkout_sessions_older_than_org(self, mock_stripe_module) -> None:
        from datetime import timedelta

        from app.services.billing_service import refresh_subscription_from_stripe

        mock_stripe = MagicMock()
        mock_stripe.checkout.Session.list.return_value.data = [
            {
                "id": "cs_test_old",
                "created": int((self.subscription.created_at - timedelta(days=1)).timestamp()),
                "status": "complete",
                "customer_email": "owner@acme.test",
                "customer": "cus_test_old",
                "subscription": "sub_test_old",
                "client_reference_id": str(self.organization.id),
                "metadata": {"organization_id": str(self.organization.id)},
            }
        ]
        mock_stripe_module.return_value = mock_stripe

        subscription = refresh_subscription_from_stripe(self.organization, "owner@acme.test")

        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.status, "incomplete")
        self.assertIsNone(subscription.stripe_customer_id)
        self.assertIsNone(subscription.stripe_subscription_id)
        mock_stripe.Subscription.retrieve.assert_not_called()

    @patch("app.routes.refresh_subscription_from_stripe")
    def test_billing_overview_skips_live_stripe_refresh_in_fake_checkout_mode(self, mock_refresh) -> None:
        self.app.config["STRIPE_FAKE_CHECKOUT_ENABLED"] = True
        self.subscription.status = "past_due"
        self.subscription.stripe_customer_id = "cus_test_123"
        self.subscription.stripe_subscription_id = "sub_test_123"
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/billing", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        mock_refresh.assert_not_called()

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
        self.assertNotIn(b'name="organization_filter"', response.data)
        self.assertNotIn(b"Organization</span>", response.data)

    def test_platform_users_list_offers_add_platform_admin_action(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/users")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add Platform Admin", response.data)
        self.assertIn(b"Platform Admin", response.data)

    def test_platform_users_list_shows_organization_context_and_filter_control(self) -> None:
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

        self._login_platform_admin()
        response = self.client.get("/users")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="organization_filter"', response.data)
        self.assertIn(b"Filter users", response.data)
        self.assertIn(b"Acme", response.data)
        self.assertIn(b"acme", response.data)
        self.assertIn(b"Other Co", response.data)
        self.assertIn(b"other-co", response.data)
        self.assertIn(b"Platform-wide access", response.data)

    def test_platform_users_list_filters_by_organization(self) -> None:
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

        self._login_platform_admin()
        response = self.client.get(f"/users?organization_filter=org:{other_org.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"other@org.test", response.data)
        self.assertIn(b"Other Co", response.data)
        self.assertNotIn(b"owner@acme.test", response.data)
        self.assertNotIn(b"platform@acme.test", response.data)

    def test_platform_users_list_filters_platform_admins(self) -> None:
        self._login_platform_admin()
        response = self.client.get("/users?organization_filter=platform_admins")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"platform@acme.test", response.data)
        self.assertIn(b"Platform-wide access", response.data)
        self.assertNotIn(b"owner@acme.test", response.data)

    def test_platform_users_list_filters_unassigned_users(self) -> None:
        unassigned_user = self.AppUser(
            username="orphan-user",
            email="orphan@acme.test",
            phone="+15550000003",
            role="social_manager",
            must_change_password=False,
        )
        unassigned_user.set_password("Orphan-pass1!")
        self.db.session.add(unassigned_user)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/users?organization_filter=unassigned")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"orphan@acme.test", response.data)
        self.assertIn(b"Unassigned", response.data)
        self.assertNotIn(b"owner@acme.test", response.data)
        self.assertNotIn(b"platform@acme.test", response.data)

    def test_platform_admin_can_create_another_platform_admin(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            "/users/add",
            data={
                "username": "ops-admin",
                "email": "ops-admin@acme.test",
                "full_name": "Ops Admin",
                "role": "admin",
                "is_platform_admin": "on",
                "phone": "+15550000014",
                "password": "Operations-pass1!",
                "must_change_password": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        created_user = self.AppUser.query.filter_by(email="ops-admin@acme.test").first()
        self.assertIsNotNone(created_user)
        self.assertTrue(created_user.is_platform_admin)
        self.assertEqual(created_user.role, "admin")

    def test_platform_admin_cannot_create_non_platform_standalone_user_in_saas_mode(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            "/users/add",
            data={
                "username": "standalone-user",
                "email": "standalone@acme.test",
                "full_name": "Standalone User",
                "role": "social_manager",
                "phone": "+15550000015",
                "password": "Standalone-pass1!",
                "must_change_password": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Users created from the platform in SaaS mode must have platform admin access.",
            response.data,
        )
        self.assertIsNone(self.AppUser.query.filter_by(email="standalone@acme.test").first())

    def test_platform_admin_cannot_remove_own_platform_admin_access(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            f"/users/{self.platform_admin.id}/edit",
            data={
                "username": "platform-admin",
                "email": "platform@acme.test",
                "full_name": "Platform Admin",
                "role": "admin",
                "phone": "+15550000009",
                "must_change_password": "off",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"You cannot remove your own platform admin access.",
            response.data,
        )
        self.db.session.refresh(self.platform_admin)
        self.assertTrue(self.platform_admin.is_platform_admin)

    def test_users_edit_promotes_saas_member_to_owner_membership_and_owner_access(self) -> None:
        staff_user = self.AppUser(
            username="staff-user",
            email="staff-user@acme.test",
            full_name="Staff User",
            phone="+15550000016",
            role="social_manager",
            must_change_password=False,
        )
        staff_user.set_password("Staff-pass1!")
        self.db.session.add(staff_user)
        self.db.session.flush()
        membership = self.OrganizationMembership(
            organization_id=self.organization.id,
            user_id=staff_user.id,
            role="staff",
        )
        self.db.session.add(membership)
        self.subscription.status = "trialing"
        self.db.session.commit()

        self._login_owner()
        response = self.client.post(
            f"/users/{staff_user.id}/edit",
            data={
                "username": "staff-user",
                "email": "staff-user@acme.test",
                "full_name": "Staff User",
                "role": "admin",
                "phone": "+15550000016",
                "must_change_password": "off",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(staff_user)
        self.db.session.refresh(membership)
        self.assertEqual(staff_user.role, "admin")
        self.assertEqual(membership.role, "owner")

        self._logout()
        self._login_with_credentials("staff-user@acme.test", "Staff-pass1!")

        self.assertEqual(self.client.get("/billing").status_code, 200)
        self.assertEqual(self.client.get("/team/invite").status_code, 200)

    def test_users_edit_demotes_saas_owner_membership_and_revokes_billing_access(self) -> None:
        second_owner = self.AppUser(
            username="second-owner",
            email="second-owner@acme.test",
            full_name="Second Owner",
            phone="+15550000017",
            role="admin",
            must_change_password=False,
        )
        second_owner.set_password("SecondOwner-pass1!")
        self.db.session.add(second_owner)
        self.db.session.flush()
        membership = self.OrganizationMembership(
            organization_id=self.organization.id,
            user_id=second_owner.id,
            role="owner",
        )
        self.db.session.add(membership)
        self.subscription.status = "trialing"
        self.db.session.commit()

        self._login_owner()
        response = self.client.post(
            f"/users/{second_owner.id}/edit",
            data={
                "username": "second-owner",
                "email": "second-owner@acme.test",
                "full_name": "Second Owner",
                "role": "social_manager",
                "phone": "+15550000017",
                "must_change_password": "off",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(second_owner)
        self.db.session.refresh(membership)
        self.assertEqual(second_owner.role, "social_manager")
        self.assertEqual(membership.role, "staff")

        self._logout()
        self._login_with_credentials("second-owner@acme.test", "SecondOwner-pass1!")

        self.assertEqual(self.client.get("/billing").status_code, 403)

    def test_users_edit_rejects_demoting_last_saas_owner(self) -> None:
        self._login_owner()

        response = self.client.post(
            f"/users/{self.owner.id}/edit",
            data={
                "username": "owner",
                "email": "owner@acme.test",
                "full_name": "Owner User",
                "role": "social_manager",
                "phone": "+15550000001",
                "must_change_password": "off",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"At least one owner is required.", response.data)
        self.db.session.refresh(self.owner)
        membership = self.OrganizationMembership.query.filter_by(user_id=self.owner.id).first()
        self.assertIsNotNone(membership)
        self.assertEqual(self.owner.role, "admin")
        self.assertEqual(membership.role, "owner")

    def test_platform_admin_can_reset_password_for_imported_saas_user_without_email(self) -> None:
        imported_user = self.AppUser(
            username="magasiev-aoc",
            email=None,
            full_name="Imported Owner",
            phone="+15550000088",
            role="admin",
            must_change_password=False,
        )
        imported_user.set_password("Imported-pass1!")
        self.db.session.add(imported_user)
        self.db.session.flush()
        self.db.session.add(
            self.OrganizationMembership(
                organization_id=self.organization.id,
                user_id=imported_user.id,
                role="owner",
            )
        )
        self.db.session.commit()

        original_password_hash = imported_user.password_hash
        self._login_platform_admin()
        response = self.client.post(
            f"/users/{imported_user.id}/edit",
            data={
                "username": "magasiev-aoc",
                "email": "",
                "full_name": "Imported Owner",
                "role": "admin",
                "phone": "+15550000088",
                "password": "Imported-reset1!",
                "must_change_password": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(imported_user)
        self.assertIsNone(imported_user.email)
        self.assertTrue(imported_user.must_change_password)
        self.assertNotEqual(imported_user.password_hash, original_password_hash)

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
        self.assertIn(f"https://app.example.com/invites/{invitation.token}".encode(), response.data)

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
        self.assertEqual(response.data.count(b"5/6 core steps complete"), 2)
        self.assertIn(b"Open invite", response.data)
        self.assertIn(b"Access", response.data)
        self.assertIn(f"https://app.example.com/invites/{invitation.token}".encode(), response.data)

    def test_platform_organization_access_page_can_create_staff_invite(self) -> None:
        self._login_platform_admin()

        page_response = self.client.get(f"/platform/organizations/{self.organization.id}/access")

        self.assertEqual(page_response.status_code, 200)
        self.assertIn(b"Invite Staff Member", page_response.data)
        self.assertIn(b"Owner Recovery", page_response.data)
        self.assertIn(b"owner@acme.test", page_response.data)

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/invite-staff",
            data={"email": "support-staff@acme.test"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Staff invitation created.", response.data)
        invitation = (
            self.OrganizationInvitation.query
            .filter_by(
                organization_id=self.organization.id,
                email="support-staff@acme.test",
                role="staff",
                status="pending",
            )
            .order_by(self.OrganizationInvitation.id.desc())
            .first()
        )
        self.assertIsNotNone(invitation)
        self.assertIn(
            f"https://app.example.com/invites/{invitation.token}".encode(),
            response.data,
        )
        event = (
            self.AuthEvent.query
            .filter_by(event_type="platform_organization_staff_invite")
            .order_by(self.AuthEvent.id.desc())
            .first()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.metadata_payload.get("target_email"), "support-staff@acme.test")

    def test_platform_organization_access_staff_invite_rejects_platform_admin_email(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/invite-staff",
            data={"email": "platform@acme.test"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Platform admin accounts cannot be assigned to an organization.",
            response.data,
        )
        self.assertIsNone(
            self.OrganizationInvitation.query.filter_by(
                organization_id=self.organization.id,
                email="platform@acme.test",
                role="staff",
                status="pending",
            ).first()
        )

    def test_platform_organization_access_staff_invite_rejects_org_bound_email(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/invite-staff",
            data={"email": "owner@acme.test"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"That email is already attached to an organization.", response.data)
        self.assertIsNone(
            self.OrganizationInvitation.query.filter_by(
                organization_id=self.organization.id,
                email="owner@acme.test",
                role="staff",
                status="pending",
            ).first()
        )

    def test_platform_organization_access_staff_invite_rejects_duplicate_pending_email(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="duplicate-staff@acme.test",
            role="staff",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/invite-staff",
            data={"email": "duplicate-staff@acme.test"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"A pending invitation already exists for that email in this organization.",
            response.data,
        )
        pending_invites = self.OrganizationInvitation.query.filter_by(
            organization_id=self.organization.id,
            email="duplicate-staff@acme.test",
            status="pending",
        ).all()
        self.assertEqual(len(pending_invites), 1)

    def test_platform_organization_access_reissues_owner_invite(self) -> None:
        organization, original_invitation = self._create_support_organization()
        original_token = original_invitation.token

        self._login_platform_admin()
        response = self.client.post(
            f"/platform/organizations/{organization.id}/access/reissue-owner-invite",
            data={"owner_email": "fixed-owner@acme.test"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Owner invite reissued with a fresh link.", response.data)
        self.db.session.refresh(original_invitation)
        self.assertEqual(original_invitation.status, "revoked")
        new_invitation = (
            self.OrganizationInvitation.query
            .filter_by(
                organization_id=organization.id,
                email="fixed-owner@acme.test",
                role="owner",
                status="pending",
            )
            .order_by(self.OrganizationInvitation.id.desc())
            .first()
        )
        self.assertIsNotNone(new_invitation)
        self.assertNotEqual(new_invitation.token, original_token)
        self.assertIn(
            f"https://app.example.com/invites/{new_invitation.token}".encode(),
            response.data,
        )
        event = (
            self.AuthEvent.query
            .filter_by(event_type="platform_organization_owner_invite_reissue")
            .order_by(self.AuthEvent.id.desc())
            .first()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.metadata_payload.get("target_email"), "fixed-owner@acme.test")
        self.assertEqual(event.metadata_payload.get("revoked_count"), 1)

    def test_platform_organization_access_reissue_owner_invite_blocked_once_owner_joined(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/reissue-owner-invite",
            data={"owner_email": "replacement-owner@acme.test"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"An owner has already joined this organization. Owner invite recovery is no longer available.",
            response.data,
        )
        self.assertIsNone(
            self.OrganizationInvitation.query.filter_by(
                organization_id=self.organization.id,
                email="replacement-owner@acme.test",
                role="owner",
                status="pending",
            ).first()
        )

    def test_owner_cannot_access_platform_organization_access_routes(self) -> None:
        self._login_owner()

        access_response = self.client.get(
            f"/platform/organizations/{self.organization.id}/access",
            follow_redirects=False,
        )
        invite_response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/invite-staff",
            data={"email": "owner-route-staff@acme.test"},
            follow_redirects=False,
        )
        reissue_response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/reissue-owner-invite",
            data={"owner_email": "owner-route-owner@acme.test"},
            follow_redirects=False,
        )

        self.assertEqual(access_response.status_code, 403)
        self.assertEqual(invite_response.status_code, 403)
        self.assertEqual(reissue_response.status_code, 403)

    def test_platform_organizations_add_page_shows_admin_setup_guidance(self) -> None:
        self._login_platform_admin()

        response = self.client.get("/platform/organizations/add")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create Business Account", response.data)
        self.assertIn(b"Platform-managed Twilio by default", response.data)
        self.assertIn(b"Twilio subaccounts and messaging services are provisioned later", response.data)
        self.assertIn(b"First Invite Role", response.data)
        self.assertIn(b"The first invite stays owner-only", response.data)
        self.assertNotIn(b"Initial Role", response.data)

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

    def test_platform_organizations_add_requires_owner_initial_invite(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            "/platform/organizations/add",
            data={
                "name": "Staff First Org",
                "slug": "staff-first-org",
                "owner_email": "staff-first@acme.test",
                "owner_role": "staff",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The initial organization invite must be for an owner.", response.data)
        self.assertIsNone(self.Organization.query.filter_by(slug="staff-first-org").first())
        self.assertIsNone(self.OrganizationInvitation.query.filter_by(email="staff-first@acme.test").first())

    def test_platform_organizations_add_rejects_platform_admin_owner_email(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            "/platform/organizations/add",
            data={
                "name": "Bad Owner Org",
                "slug": "bad-owner-org",
                "owner_email": "platform@acme.test",
                "owner_role": "owner",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Platform admin accounts cannot be assigned to an organization.",
            response.data,
        )
        self.assertIsNone(self.Organization.query.filter_by(slug="bad-owner-org").first())

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

    def test_platform_admin_can_grant_complimentary_billing(self) -> None:
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/access/billing",
            data={"action": "grant_complimentary"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(self.subscription)
        self.assertEqual(self.subscription.status, "complimentary")

    @patch("app.routes.ensure_a2p_event_stream_subscription")
    @patch("app.routes._sync_customer_managed_onboarding_state")
    @patch("app.routes.save_customer_managed_profile")
    def test_platform_organizations_messaging_edit_can_save_customer_managed_provider(
        self,
        mock_save_customer_managed_profile,
        mock_sync_customer_managed_onboarding_state,
        mock_ensure_a2p_event_stream_subscription,
    ) -> None:
        self._login_platform_admin()
        self.messaging_profile.provider_mode = "customer_managed"
        self.messaging_profile.twilio_account_sid = None
        self.messaging_profile.twilio_subaccount_sid = None
        self.messaging_profile.phone_number_sid = None
        self.messaging_profile.from_number = None
        self.messaging_profile.sender_review_status = "pending"
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.status = "pending"
        self.db.session.commit()
        validation_result = SimpleNamespace(
            account_sid="ACcust0001",
            phone_number_sid="PNcust0001",
            from_number="+15550001111",
            messaging_service_sid="MGcust0001",
            campaign_sid="QEcust0001",
            campaign_status="verified",
            campaign_failure_reason=None,
            campaign_failure_code=None,
            brand_registration_sid="BNcust0001",
            brand_status="verified",
            current_phone_sms_url="https://sms.theitwingman.com/webhooks/twilio/inbound",
            current_phone_sms_method="POST",
            current_service_use_inbound_webhook_on_number=False,
        )
        mock_save_customer_managed_profile.return_value = (self.messaging_profile, validation_result)

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "save",
                "provider_mode": "customer_managed",
                "twilio_account_sid": "ACcust0001",
                "twilio_auth_token": "customer-token",
                "sender_number": "+15550001111",
                "messaging_service_sid": "MGcust0001",
                "business_type": "Nonprofit",
                "use_case": "Announcements",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_save_customer_managed_profile.assert_called_once()
        self.assertEqual(mock_save_customer_managed_profile.call_args.kwargs["twilio_account_sid"], "ACcust0001")
        self.assertEqual(mock_save_customer_managed_profile.call_args.kwargs["from_number"], "+15550001111")
        self.assertFalse(mock_save_customer_managed_profile.call_args.kwargs["activation_complete"])
        mock_sync_customer_managed_onboarding_state.assert_called_once()
        self.assertNotIn("bind_inbound_webhook", mock_sync_customer_managed_onboarding_state.call_args.kwargs)
        mock_ensure_a2p_event_stream_subscription.assert_not_called()

    @patch("app.routes._customer_managed_auth_token_for_save", return_value="customer-token")
    @patch("app.routes.ensure_a2p_event_stream_subscription")
    @patch("app.routes._sync_customer_managed_onboarding_state")
    @patch("app.routes.save_customer_managed_profile")
    def test_platform_organizations_messaging_edit_can_activate_customer_managed_provider(
        self,
        mock_save_customer_managed_profile,
        mock_sync_customer_managed_onboarding_state,
        mock_ensure_a2p_event_stream_subscription,
        _mock_customer_managed_auth_token_for_save,
    ) -> None:
        self._login_platform_admin()
        self.messaging_profile.provider_mode = "customer_managed"
        self.messaging_profile.twilio_account_sid = "ACcust0001"
        self.messaging_profile.twilio_subaccount_sid = None
        self.messaging_profile.messaging_service_sid = "MGcust0001"
        self.messaging_profile.phone_number_sid = "PNcust0001"
        self.messaging_profile.from_number = "+15550001111"
        self.messaging_profile.sender_review_status = "approved"
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.status = "pending"
        self.db.session.commit()
        validation_result = SimpleNamespace(
            account_sid="ACcust0001",
            phone_number_sid="PNcust0001",
            from_number="+15550001111",
            messaging_service_sid="MGcust0001",
            campaign_sid="QEcust0001",
            campaign_status="verified",
            campaign_failure_reason=None,
            campaign_failure_code=None,
            brand_registration_sid="BNcust0001",
            brand_status="verified",
            current_phone_sms_url="https://sms.theitwingman.com/webhooks/twilio/inbound",
            current_phone_sms_method="POST",
            current_service_use_inbound_webhook_on_number=False,
        )
        mock_save_customer_managed_profile.return_value = (self.messaging_profile, validation_result)
        mock_ensure_a2p_event_stream_subscription.side_effect = lambda organization, profile: setattr(profile, "event_stream_status", "configured")

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={"action": "activate_customer_managed"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_save_customer_managed_profile.assert_called_once()
        self.assertTrue(mock_save_customer_managed_profile.call_args.kwargs["bind_inbound_webhook"])
        self.assertFalse(mock_save_customer_managed_profile.call_args.kwargs["activation_complete"])
        self.assertEqual(mock_sync_customer_managed_onboarding_state.call_count, 2)
        first_call = mock_sync_customer_managed_onboarding_state.call_args_list[0]
        second_call = mock_sync_customer_managed_onboarding_state.call_args_list[1]
        self.assertTrue(first_call.kwargs["bind_inbound_webhook"])
        self.assertFalse(first_call.kwargs["activation_complete"])
        self.assertTrue(second_call.kwargs["bind_inbound_webhook"])
        self.assertTrue(second_call.kwargs["activation_complete"])
        self.db.session.refresh(self.messaging_profile)
        self.assertEqual(self.messaging_profile.provider_status, "active")
        self.assertTrue(self.messaging_profile.can_send)

    @patch("app.routes.rollback_customer_managed_profile")
    def test_platform_organizations_messaging_edit_can_restore_customer_managed_webhook(
        self,
        mock_rollback_customer_managed_profile,
    ) -> None:
        self._login_platform_admin()
        self.messaging_profile.provider_mode = "customer_managed"
        self.messaging_profile.provider_status = "active"
        self.messaging_profile.status = "active"
        self.messaging_profile.twilio_account_sid = "ACcust0001"
        self.messaging_profile.twilio_subaccount_sid = None
        self.db.session.commit()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={"action": "rollback_customer_managed"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_rollback_customer_managed_profile.assert_called_once_with(
            self.organization.id,
            actor_user_id=self.platform_admin.id,
        )

    def test_platform_organizations_messaging_edit_shows_platform_test_send_when_org_can_send(self) -> None:
        self._login_platform_admin()
        self.subscription.status = "complimentary"
        self.db.session.commit()

        response = self.client.get(
            f"/platform/organizations/{self.organization.id}/messaging",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Platform Test Send", response.data)

    @patch("app.routes.send_operational_test_message")
    def test_platform_organizations_messaging_edit_blocks_test_send_for_suspended_org(
        self,
        mock_send_operational_test_message,
    ) -> None:
        self._login_platform_admin()
        self.organization.status = "suspended"
        self.subscription.status = "complimentary"
        self.db.session.commit()

        get_response = self.client.get(
            f"/platform/organizations/{self.organization.id}/messaging",
            follow_redirects=False,
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertNotIn(b"Platform Test Send", get_response.data)

        post_response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "platform_test_send",
                "platform_test_phone": "+15550001234",
                "platform_test_body": "Operational test",
            },
            follow_redirects=True,
        )

        self.assertEqual(post_response.status_code, 200)
        self.assertIn(
            b"This organization is not ready for a live operational test send yet.",
            post_response.data,
        )
        mock_send_operational_test_message.assert_not_called()

    @patch("app.routes.send_operational_test_message")
    def test_platform_organizations_messaging_edit_can_send_operational_test(
        self,
        mock_send_operational_test_message,
    ) -> None:
        self._login_platform_admin()
        self.subscription.status = "complimentary"
        self.db.session.commit()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "platform_test_send",
                "platform_test_phone": "+15550001234",
                "platform_test_body": "Operational test",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_send_operational_test_message.assert_called_once_with(
            self.organization.id,
            to_number="+15550001234",
            body="Operational test",
            actor_user_id=self.platform_admin.id,
        )

    @patch("app.routes.send_operational_test_message")
    def test_platform_organizations_messaging_edit_duplicate_test_send_is_suppressed(
        self,
        mock_send_operational_test_message,
    ) -> None:
        self._login_platform_admin()
        self.subscription.status = "complimentary"
        self.db.session.commit()

        fake_redis = FakeRedis()
        with patch(
            "app.services.outbound_idempotency_service.get_redis_connection",
            return_value=fake_redis,
        ):
            first_response = self.client.post(
                f"/platform/organizations/{self.organization.id}/messaging",
                data={
                    "action": "platform_test_send",
                    "platform_test_phone": "+15550001234",
                    "platform_test_body": "Operational test",
                },
                follow_redirects=False,
            )
            second_response = self.client.post(
                f"/platform/organizations/{self.organization.id}/messaging",
                data={
                    "action": "platform_test_send",
                    "platform_test_phone": "+15550001234",
                    "platform_test_body": "Operational test",
                },
                follow_redirects=True,
            )

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 200)
        mock_send_operational_test_message.assert_called_once_with(
            self.organization.id,
            to_number="+15550001234",
            body="Operational test",
            actor_user_id=self.platform_admin.id,
        )
        self.assertIn(
            b"An identical platform test send was already submitted. The duplicate request was ignored.",
            second_response.data,
        )

    def test_platform_organizations_messaging_edit_rejects_invalid_test_phone(self) -> None:
        self._login_platform_admin()
        self.subscription.status = "complimentary"
        self.db.session.commit()

        fake_redis = FakeRedis()
        with patch(
            "app.services.outbound_idempotency_service.get_redis_connection",
            return_value=fake_redis,
        ):
            response = self.client.post(
                f"/platform/organizations/{self.organization.id}/messaging",
                data={
                    "action": "platform_test_send",
                    "platform_test_phone": "not-a-phone",
                    "platform_test_body": "Operational test",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Enter a valid E.164 phone number for the operational test send.",
            response.data,
        )

    def test_platform_organizations_messaging_edit_rejects_blank_test_body(self) -> None:
        self._login_platform_admin()
        self.subscription.status = "complimentary"
        self.db.session.commit()

        fake_redis = FakeRedis()
        with patch(
            "app.services.outbound_idempotency_service.get_redis_connection",
            return_value=fake_redis,
        ):
            response = self.client.post(
                f"/platform/organizations/{self.organization.id}/messaging",
                data={
                    "action": "platform_test_send",
                    "platform_test_phone": "+15550001234",
                    "platform_test_body": "   ",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Message body is required for the operational test send.",
            response.data,
        )

    @patch("app.routes.send_operational_test_message")
    def test_platform_organizations_messaging_edit_rejects_test_send_when_org_not_ready(
        self,
        mock_send_operational_test_message,
    ) -> None:
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "platform_test_send",
                "platform_test_phone": "+15550001234",
                "platform_test_body": "Operational test",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"This organization is not ready for a live operational test send yet.",
            response.data,
        )
        mock_send_operational_test_message.assert_not_called()

    def test_platform_organizations_messaging_edit_browser_fake_test_send_records_normalized_metadata(self) -> None:
        self._login_platform_admin()
        self.subscription.status = "complimentary"
        self.db.session.commit()
        self.app.config["TWILIO_BROWSER_FAKE_SENDS"] = True
        self.app.config["TWILIO_ACCOUNT_SID"] = "ACplatformtest"
        self.app.config["TWILIO_AUTH_TOKEN"] = "platform-token"

        fake_redis = FakeRedis()
        with patch(
            "app.services.outbound_idempotency_service.get_redis_connection",
            return_value=fake_redis,
        ):
            response = self.client.post(
                f"/platform/organizations/{self.organization.id}/messaging",
                data={
                    "action": "platform_test_send",
                    "platform_test_phone": "+15550001234",
                    "platform_test_body": "—" * 71,
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Platform operational test send completed.", response.data)
        record = self.MessagingUsageRecord.query.one()
        self.assertEqual(record.organization_id, self.organization.id)
        self.assertEqual(record.source, "operational_test")
        self.assertEqual(record.twilio_message_status, "sent")
        audit_log = (
            self.OrganizationProviderAuditLog.query.filter_by(
                organization_id=self.organization.id,
                action="operational_test_send",
                status="success",
            )
            .order_by(self.OrganizationProviderAuditLog.id.desc())
            .first()
        )
        self.assertIsNotNone(audit_log)
        metadata = json.loads(audit_log.metadata_json or "{}")
        self.assertEqual(metadata.get("segment_count"), 1)
        self.assertEqual(metadata.get("encoding"), "gsm-7")
        self.assertTrue(str(metadata.get("message_sid", "")).startswith("SM"))

    def test_sync_customer_managed_onboarding_state_keeps_external_identifiers_out_of_unique_columns(self) -> None:
        from app.routes import _sync_customer_managed_onboarding_state

        organization, _, _, _ = self._create_customer_managed_workspace(
            slug="customer-managed-sync",
            username="customer-managed-sync",
            email="customer-managed-sync@acme.test",
        )

        validation_result = SimpleNamespace(
            account_sid="ACcust0001",
            phone_number_sid="PNcust0001",
            from_number="+15550001111",
            messaging_service_sid="MGcust0001",
            campaign_sid="QEcust0001",
            campaign_status="verified",
            campaign_failure_reason=None,
            campaign_failure_code=None,
            brand_registration_sid="BNcust0001",
            brand_status="verified",
            current_phone_sms_url="https://sms.theitwingman.com/webhooks/twilio/inbound",
            current_phone_sms_method="POST",
            current_service_use_inbound_webhook_on_number=False,
        )

        _sync_customer_managed_onboarding_state(organization, validation_result)
        self.db.session.commit()

        onboarding = self.OrganizationA2POnboarding.query.filter_by(organization_id=organization.id).first()
        self.assertIsNotNone(onboarding)
        self.assertIsNone(onboarding.campaign_sid)
        self.assertIsNone(onboarding.brand_registration_sid)
        self.assertEqual(onboarding.campaign_status, "verified")
        self.assertEqual(onboarding.brand_status, "verified")
        self.assertEqual(onboarding.onboarding_status, "approved")

        status_payload = json.loads(onboarding.raw_status_json)
        self.assertEqual(status_payload["campaign_sid"], "QEcust0001")
        self.assertEqual(status_payload["brand_registration_sid"], "BNcust0001")
        self.assertEqual(status_payload["messaging_service_sid"], "MGcust0001")
        self.assertEqual(
            status_payload["customer_managed_activation"]["activation_state"],
            "validated",
        )

    @patch("app.routes.release_sender")
    @patch("app.routes.finalize_sender_setup")
    @patch("app.routes.list_reusable_subaccount_numbers")
    def test_platform_organizations_messaging_edit_can_release_save_service_address_and_finalize_sender(
        self,
        mock_list_reusable_subaccount_numbers,
        mock_finalize_sender_setup,
        mock_release_sender,
    ) -> None:
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
            status="active",
            provider_status="active",
            service_address_source_mode="twilio_import",
            twilio_address_sid="ADstale0001",
            twilio_address_json='{"sid":"ADstale0001"}',
            emergency_address_sid="ADstale0001",
            emergency_address_status="synced",
            emergency_address_last_error="stale error",
            sender_finalization_status="active",
        )
        self.db.session.add_all([other_org, other_subscription, other_messaging_profile])
        self.db.session.commit()

        def _release_side_effect(organization_id, actor_user_id=None):
            profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).first()
            profile.from_number = None
            profile.phone_number_sid = None
            profile.inbound_identity = profile.messaging_service_sid
            profile.set_sender_finalization_status("awaiting_number_purchase")
            profile.set_provider_status("pending")
            self.db.session.commit()
            return profile

        def _finalize_side_effect(organization_id, actor_user_id=None):
            profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).first()
            profile.from_number = "+15550009999"
            profile.phone_number_sid = "PN1234567890ABCDE"
            profile.inbound_identity = profile.from_number
            profile.set_sender_finalization_status("active")
            profile.set_provider_status("active")
            self.db.session.commit()
            return profile

        mock_release_sender.side_effect = _release_side_effect
        mock_finalize_sender_setup.side_effect = _finalize_side_effect
        mock_list_reusable_subaccount_numbers.return_value = [
            SimpleNamespace(sid="PN1234567890ABCDE", phone_number="+15550009999")
        ]

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
                "number_strategy": "existing_subaccount_number",
                "existing_subaccount_phone_number_sid": "PN1234567890ABCDE",
                "service_address_line1": "456 Other Street",
                "service_address_city": "Denver",
                "service_address_region": "CO",
                "service_address_postal_code": "80203",
                "service_address_country": "US",
            },
            follow_redirects=False,
        )
        self.assertEqual(assign_response.status_code, 302)

        self.db.session.refresh(other_messaging_profile)
        self.assertEqual(other_messaging_profile.status, "pending")
        self.assertEqual(other_messaging_profile.provider_status, "pending")
        self.assertEqual(other_messaging_profile.service_address_line1, "456 Other Street")
        self.assertEqual(other_messaging_profile.service_address_city, "Denver")
        self.assertEqual(other_messaging_profile.service_address_region, "CO")
        self.assertEqual(other_messaging_profile.service_address_postal_code, "80203")
        self.assertEqual(other_messaging_profile.service_address_country, "US")
        self.assertEqual(other_messaging_profile.service_address_source_mode, "app_input")
        self.assertEqual(other_messaging_profile.messaging_service_sid, "MGother0001")
        self.assertIsNone(other_messaging_profile.phone_number_sid)
        self.assertIsNone(other_messaging_profile.twilio_address_sid)
        self.assertIsNone(other_messaging_profile.twilio_address_json)
        self.assertIsNone(other_messaging_profile.emergency_address_sid)
        self.assertIsNone(other_messaging_profile.emergency_address_status)
        self.assertIsNone(other_messaging_profile.emergency_address_last_error)
        self.assertEqual(other_messaging_profile.sender_finalization_status, "awaiting_a2p_approval")

        self.db.session.refresh(other_org)
        self.assertEqual(other_org.a2p_onboarding.number_strategy, "existing_subaccount_number")
        self.assertEqual(other_org.a2p_onboarding.desired_phone_number_sid, "PN1234567890ABCDE")

        finalize_response = self.client.post(
            f"/platform/organizations/{other_org.id}/messaging",
            data={"action": "finalize_sender"},
            follow_redirects=False,
        )
        self.assertEqual(finalize_response.status_code, 302)
        mock_finalize_sender_setup.assert_called_once_with(other_org.id, actor_user_id=self.platform_admin.id)

        self.db.session.refresh(other_messaging_profile)
        self.assertEqual(other_messaging_profile.status, "active")
        self.assertEqual(other_messaging_profile.provider_status, "active")
        self.assertEqual(other_messaging_profile.from_number, "+15550009999")
        self.assertEqual(other_messaging_profile.phone_number_sid, "PN1234567890ABCDE")
        self.assertEqual(other_messaging_profile.inbound_identity, "+15550009999")

    def test_platform_admin_can_open_a2p_onboarding_wizard(self) -> None:
        self._login_platform_admin()

        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A2P Onboarding", response.data)
        self.assertIn(b"Legal Business Name", response.data)
        self.assertRegex(response.data, rb'<option value="low_volume_standard" selected>')
        self.assertRegex(response.data, rb'<option value="ACCOUNT_NOTIFICATION" selected>')
        self.assertIn(b"external_privacy_policy_url", response.data)
        self.assertIn(b"has_public_website", response.data)
        self.assertIn(b"https://app.example.com/compliance/acme/sms/privacy", response.data)
        self.assertNotIn(b'value="None"', response.data)

    def test_platform_admin_messaging_page_shows_a2p_failure_detail(self) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="rejected",
            brand_status="verified",
            campaign_status="failed",
            brand_registration_sid="BNcust0001",
            campaign_sid="QEcust0001",
            failure_code="30909",
            last_error="CTA could not be verified.",
            raw_status_json='{"campaign_failure_code":"30909","campaign_failure_reason":"CTA could not be verified."}',
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Twilio 30909", response.data)
        self.assertIn(b"CTA could not be verified.", response.data)

    @patch("app.routes.list_reusable_subaccount_numbers")
    def test_platform_admin_messaging_page_defaults_blank_org_to_auto_buy(self, mock_list_reusable_subaccount_numbers) -> None:
        organization, _ = self._create_support_organization(
            name="Blank Co",
            slug="blank-co",
            owner_email="blank@acme.test",
        )
        blank_profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization.id).first()
        blank_profile.twilio_subaccount_sid = "ACblank0001"
        blank_profile.messaging_service_sid = "MGblank0001"
        self.db.session.commit()
        mock_list_reusable_subaccount_numbers.return_value = []
        self._login_platform_admin()

        response = self.client.get(f"/platform/organizations/{organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.data, rb'<option value="auto_buy" selected>')
        self.assertNotIn(b"Reusable subaccount numbers found", response.data)

    @patch("app.routes.list_reusable_subaccount_numbers")
    def test_platform_admin_messaging_page_recommends_existing_subaccount_number_when_inventory_exists(
        self,
        mock_list_reusable_subaccount_numbers,
    ) -> None:
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_finalization_status = "awaiting_sender_attach"
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            brand_status="approved",
            campaign_status="verified",
            number_strategy="auto_buy",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        mock_list_reusable_subaccount_numbers.return_value = [
            SimpleNamespace(sid="PNowned0001", phone_number="+15550001111")
        ]

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.data, rb'<option value="existing_subaccount_number" selected>')
        self.assertIn(b"Reusable subaccount numbers found", response.data)
        self.assertIn(b"existing_subaccount_phone_number_sid", response.data)
        self.assertIn(b'id="finalize_number_strategy"', response.data)
        self.assertIn(b'id="finalize_existing_subaccount_phone_number_sid"', response.data)
        self.assertIn(b"+15550001111", response.data)

    @patch("app.routes.finalize_sender_setup")
    @patch("app.routes.list_reusable_subaccount_numbers")
    def test_platform_admin_finalize_persists_selected_reusable_number_without_prior_save(
        self,
        mock_list_reusable_subaccount_numbers,
        mock_finalize_sender_setup,
    ) -> None:
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_finalization_status = "awaiting_sender_attach"
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            brand_status="approved",
            campaign_status="verified",
            number_strategy="existing_subaccount_number",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        mock_list_reusable_subaccount_numbers.return_value = [
            SimpleNamespace(sid="PNowned0001", phone_number="+15550001111")
        ]

        def _finalize_side_effect(organization_id, actor_user_id=None):
            current_onboarding = self.OrganizationA2POnboarding.query.filter_by(organization_id=organization_id).one()
            self.assertEqual(current_onboarding.number_strategy, "existing_subaccount_number")
            self.assertEqual(current_onboarding.desired_phone_number_sid, "PNowned0001")
            profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).one()
            profile.set_sender_finalization_status("awaiting_sender_attach")
            self.db.session.commit()
            return profile

        mock_finalize_sender_setup.side_effect = _finalize_side_effect

        self._login_platform_admin()
        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={
                "action": "finalize_sender",
                "number_strategy": "existing_subaccount_number",
                "existing_subaccount_phone_number_sid": "PNowned0001",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_finalize_sender_setup.assert_called_once_with(self.organization.id, actor_user_id=self.platform_admin.id)

    @patch("app.routes.finalize_sender_setup")
    @patch("app.routes.list_reusable_subaccount_numbers")
    def test_platform_admin_finalize_uses_only_reusable_number_when_saved_strategy_has_no_sid(
        self,
        mock_list_reusable_subaccount_numbers,
        mock_finalize_sender_setup,
    ) -> None:
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_finalization_status = "awaiting_sender_attach"
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            brand_status="approved",
            campaign_status="verified",
            number_strategy="existing_subaccount_number",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        mock_list_reusable_subaccount_numbers.return_value = [
            SimpleNamespace(sid="PNonly0001", phone_number="+15550002222")
        ]

        def _finalize_side_effect(organization_id, actor_user_id=None):
            current_onboarding = self.OrganizationA2POnboarding.query.filter_by(organization_id=organization_id).one()
            self.assertEqual(current_onboarding.desired_phone_number_sid, "PNonly0001")
            profile = self.OrganizationMessagingProfile.query.filter_by(organization_id=organization_id).one()
            profile.set_sender_finalization_status("awaiting_sender_attach")
            self.db.session.commit()
            return profile

        mock_finalize_sender_setup.side_effect = _finalize_side_effect

        self._login_platform_admin()
        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging",
            data={"action": "finalize_sender"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_finalize_sender_setup.assert_called_once_with(self.organization.id, actor_user_id=self.platform_admin.id)

    @patch("app.routes.list_reusable_subaccount_numbers")
    def test_platform_admin_messaging_page_surfaces_subaccount_number_discovery_errors(
        self,
        mock_list_reusable_subaccount_numbers,
    ) -> None:
        from app.services.twilio_service import ProviderProvisioningError

        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_finalization_status = "awaiting_sender_attach"
        self.db.session.commit()
        mock_list_reusable_subaccount_numbers.side_effect = ProviderProvisioningError("Stored Twilio auth token is missing.")

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Subaccount number discovery", response.data)
        self.assertIn(b"Stored Twilio auth token is missing.", response.data)

    def test_platform_admin_messaging_page_shows_launch_checklist_and_recent_twilio_activity(self) -> None:
        self.subscription.status = "active"
        self.messaging_profile.status = "pending"
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_review_status = "pending"
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        approved_at = datetime.utcnow()
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            approved_at=approved_at,
            brand_status="approved",
            campaign_status="approved",
            campaign_sid="QEapproved123",
            brand_registration_sid="BNapproved123",
            registration_path="low_volume_standard",
            number_strategy="platform_assign",
            submission_source_mode="hosted_fallback",
            business_name="Acme LLC",
            email="owner@acme.test",
            first_name="Owner",
            last_name="User",
            campaign_description="Transactional reminders and support updates.",
            message_flow="Customers opt in on the Acme website before receiving reminders and support updates. Reply STOP to opt out and HELP for help.",
            message_samples_json='["Acme: Your reminder is ready.", "Acme: Your appointment is confirmed."]',
            privacy_policy_url="https://app.example.com/compliance/acme/sms/privacy",
            terms_and_conditions_url="https://app.example.com/compliance/acme/sms/terms",
            cta_proof_url="https://app.example.com/compliance/acme/sms/opt-in",
        )
        self.db.session.add(onboarding)
        self.db.session.add(
            self.OrganizationProviderAuditLog(
                organization_id=self.organization.id,
                action="a2p_review_approved",
                status="success",
                message="Twilio approved the A2P registration.",
                metadata_json=json.dumps(
                    {
                        "campaign_sid": "QEapproved123",
                        "messaging_service_sid": self.messaging_profile.messaging_service_sid,
                        "submission_source_mode": "hosted_fallback",
                    }
                ),
            )
        )
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Launch readiness", response.data)
        self.assertNotIn(b"Await sender assignment", response.data)
        self.assertNotIn(b"Next operator action", response.data)
        self.assertIn(b"Save the target PN SID from the org Twilio subaccount", response.data)
        self.assertIn(b"Service address", response.data)
        self.assertIn(b"Emergency address sync", response.data)
        self.assertIn(b"Recent Twilio activity", response.data)
        self.assertIn(b"A2P approved", response.data)
        self.assertIn(b"Hosted fallback", response.data)

    def test_platform_admin_onboarding_page_shows_a2p_failure_detail(self) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="rejected",
            brand_status="verified",
            campaign_status="failed",
            failure_code="30909",
            last_error="CTA could not be verified.",
            raw_status_json='{"campaign_failure_code":"30909","campaign_failure_reason":"CTA could not be verified."}',
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Twilio code:", response.data)
        self.assertIn(b"30909", response.data)
        self.assertIn(b"CTA could not be verified.", response.data)

    def test_platform_admin_onboarding_page_shows_retry_guidance_for_same_campaign_failure(self) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="rejected",
            brand_status="approved",
            campaign_status="failed",
            brand_registration_sid="BNacct123",
            campaign_sid="QEacct123",
            campaign_use_case="ACCOUNT_NOTIFICATION",
            failure_code="30909",
            last_error="CTA could not be verified.",
            raw_status_json='{"campaign_use_case":"ACCOUNT_NOTIFICATION","campaign_failure_code":"30909","campaign_failure_reason":"CTA could not be verified."}',
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Retry guidance", response.data)
        self.assertIn(b"edit and retry flow instead of delete-and-recreate", response.data)

    def test_platform_admin_onboarding_page_shows_reconcile_and_fee_aware_campaign_actions(self) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="needs_action",
            brand_status="approved",
            customer_profile_sid="BUstale123",
            trust_product_sid="BUtruststale123",
            brand_registration_sid="BNstale123",
            campaign_sid="QEstale123",
            raw_status_json=json.dumps(
                {
                    "recovery_state": {
                        "type": "provider_drift",
                        "recommended_action": "reconcile",
                        "summary": "Twilio still has approved resources, but the app is bound to stale identifiers.",
                        "stored": {
                            "messaging_service_sid": "MGstale123",
                            "customer_profile_sid": "BUstale123",
                            "trust_product_sid": "BUtruststale123",
                            "brand_registration_sid": "BNstale123",
                        },
                        "live": {
                            "services": [{"sid": "MGlive123", "friendly_name": "SMS"}],
                            "customer_profiles": [{"sid": "BUcustomer123", "friendly_name": "Acme", "status": "twilio-approved"}],
                            "trust_products": [{"sid": "BUtrust123", "friendly_name": "Acme", "status": "twilio-approved"}],
                            "brands": [{"sid": "BNlive123", "status": "approved", "tcr_id": "TCR123"}],
                        },
                        "selected": {
                            "messaging_service_sid": "MGlive123",
                            "customer_profile_sid": "BUcustomer123",
                            "trust_product_sid": "BUtrust123",
                            "brand_registration_sid": "BNlive123",
                        },
                        "missing": {"messaging_service_sid": True},
                        "only_missing_campaign": False,
                        "observed_ids": {},
                    }
                }
            ),
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reconcile Twilio State", response.data)
        self.assertIn(b"Stored vs Live Twilio State", response.data)
        self.assertIn(b"Stored Brand Registration SID", response.data)
        self.assertIn(b"Stored Customer Profile SID", response.data)
        self.assertIn(b"Stored Trust Product SID", response.data)
        self.assertIn(b"MGlive123", response.data)

        onboarding.raw_status_json = json.dumps(
            {
                "recovery_state": {
                    "type": "missing_campaign",
                    "recommended_action": "create_campaign",
                    "summary": "Twilio approved the brand package, but the Messaging Service has no campaign attached.",
                    "stored": {},
                    "live": {},
                    "selected": {},
                    "missing": {"campaign_sid": True},
                    "only_missing_campaign": True,
                    "observed_ids": {},
                }
            }
        )
        self.db.session.commit()

        follow_up = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")
        self.assertEqual(follow_up.status_code, 200)
        self.assertIn(b"Create Campaign", follow_up.data)
        self.assertIn(b"another Twilio campaign vetting fee", follow_up.data)

    def test_platform_admin_a2p_onboarding_is_read_only_for_customer_managed_org(self) -> None:
        self.messaging_profile.provider_mode = "customer_managed"
        self.messaging_profile.twilio_account_sid = "ACcust0001"
        self.messaging_profile.messaging_service_sid = "MGcust0001"
        self.messaging_profile.phone_number_sid = "PNcust0001"
        self.messaging_profile.from_number = "+15550001111"
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="approved",
            brand_status="verified",
            campaign_status="verified",
            brand_registration_sid="BNcust0001",
            campaign_sid="QEcust0001",
            raw_status_json='{"external_managed": true, "brand_status": "verified", "campaign_status": "verified", "messaging_service_sid": "MGcust0001", "phone_number_sid": "PNcust0001", "console_campaign_id": "CMcust0001"}',
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        self._login_platform_admin()

        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Customer-managed A2P", response.data)
        self.assertIn(b"External Twilio active", response.data)
        self.assertIn(b"Back to Messaging Setup", response.data)
        self.assertIn(b"Service Campaign Association SID", response.data)
        self.assertIn(b"Brand Registration SID", response.data)
        self.assertIn(b"Console Campaign ID", response.data)
        self.assertIn(b"CMcust0001", response.data)
        self.assertNotIn(b"Legal Business Name", response.data)
        self.assertNotIn(b"Submit A2P Onboarding", response.data)

    def test_platform_admin_sees_manage_a2p_onboarding_link_on_messaging_page_before_onboarding_exists(self) -> None:
        self._login_platform_admin()

        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Manage A2P Onboarding", response.data)
        self.assertIn(b"Status: <strong>Not submitted yet</strong>", response.data)
        self.assertIn(b"Finalize Sender Setup", response.data)
        self.assertIn(b"Target Phone Number SID", response.data)
        self.assertIn(b"Service Address Line 1", response.data)

    def test_platform_admin_messaging_page_does_not_render_none_field_values(self) -> None:
        self.messaging_profile.from_number = None
        self.messaging_profile.phone_number_sid = None
        self.messaging_profile.business_type = None
        self.messaging_profile.use_case = None
        self.messaging_profile.service_address_line1 = None
        self.messaging_profile.service_address_city = None
        self.messaging_profile.service_address_region = None
        self.messaging_profile.service_address_postal_code = None
        self.messaging_profile.service_address_country = None
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'value="None"', response.data)

    def test_platform_admin_get_onboarding_restores_saved_checkbox_defaults(self) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="pending",
            business_name="Acme Co",
            email="ops@acme.test",
            first_name="Avery",
            last_name="Admin",
            campaign_description="Announcements",
            message_flow="Users opt in on the website.",
            message_samples_json='["Sample message"]',
            raw_submission_json='{"has_embedded_links": true, "has_embedded_phone": true}',
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        self._login_platform_admin()

        response = self.client.get(f"/platform/organizations/{self.organization.id}/messaging/onboarding")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.data, rb'id="has_embedded_links"[^>]*checked')
        self.assertRegex(response.data, rb'id="has_embedded_phone"[^>]*checked')

    def test_hosted_sms_compliance_pages_render_tenant_sender(self) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            business_name="Acme Realty",
            campaign_description="Appointment reminders and account updates.",
            message_flow="Customers opt in on this page before receiving recurring SMS reminders. Reply STOP to opt out and HELP for help.",
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        privacy_response = self.client.get("/compliance/acme/sms/privacy")
        terms_response = self.client.get("/compliance/acme/sms/terms")
        opt_in_response = self.client.get("/compliance/acme/sms/opt-in")

        self.assertEqual(privacy_response.status_code, 200)
        self.assertEqual(terms_response.status_code, 200)
        self.assertEqual(opt_in_response.status_code, 200)
        self.assertIn(b"Acme Realty", privacy_response.data)
        self.assertIn(b"uses SMS for this program:", privacy_response.data)
        self.assertIn(b"SMS Terms and Conditions", terms_response.data)
        self.assertIn(b"related to this program:", terms_response.data)
        self.assertIn(b"STOP", opt_in_response.data)

    @patch("app.routes.submit_a2p_onboarding")
    def test_platform_admin_can_submit_a2p_onboarding(self, mock_submit_a2p_onboarding) -> None:
        mock_submit_a2p_onboarding.return_value = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            registration_path="standard",
            number_strategy="auto_buy",
        )
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            data={
                "action": "submit",
                "registration_path": "standard",
                "number_strategy": "auto_buy",
                "business_name": "Acme",
                "email": "ops@acme.test",
                "first_name": "Jane",
                "last_name": "Doe",
                "campaign_description": "Announcements",
                "message_flow": "Users opt in on the website.",
                "message_samples": "Sample message",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/messaging/onboarding", response.headers.get("Location", ""))
        mock_submit_a2p_onboarding.assert_called_once()

    @patch("app.routes.submit_a2p_onboarding")
    def test_platform_admin_submit_a2p_onboarding_surfaces_provider_errors(self, mock_submit_a2p_onboarding) -> None:
        from app.services.twilio_service import ProviderProvisioningError

        mock_submit_a2p_onboarding.side_effect = ProviderProvisioningError("Queue is unavailable.")
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            data={
                "action": "submit",
                "registration_path": "standard",
                "number_strategy": "auto_buy",
                "business_name": "Acme",
                "email": "ops@acme.test",
                "first_name": "Jane",
                "last_name": "Doe",
                "campaign_description": "Announcements",
                "message_flow": "Users opt in on the website.",
                "message_samples": "Sample message",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Queue is unavailable.", response.data)
        self.assertIn(b"A2P Onboarding", response.data)
        self.assertIn(b"ops@acme.test", response.data)
        self.assertIn(b"Announcements", response.data)

    @patch("app.routes.reconcile_a2p_twilio_state")
    def test_platform_admin_reconcile_action_requires_confirmation_and_calls_service(self, mock_reconcile) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="needs_action",
            raw_status_json=json.dumps(
                {
                    "recovery_state": {
                        "type": "provider_drift",
                        "recommended_action": "reconcile",
                        "summary": "Twilio state drifted.",
                        "stored": {},
                        "live": {
                            "services": [{"sid": "MGlive123"}],
                            "customer_profiles": [{"sid": "BUcustomer123"}],
                            "trust_products": [{"sid": "BUtrust123"}],
                            "brands": [{"sid": "BNlive123"}],
                        },
                        "selected": {
                            "messaging_service_sid": "MGlive123",
                            "customer_profile_sid": "BUcustomer123",
                            "trust_product_sid": "BUtrust123",
                            "brand_registration_sid": "BNlive123",
                        },
                        "missing": {},
                        "only_missing_campaign": False,
                        "observed_ids": {},
                    }
                }
            ),
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        self._login_platform_admin()

        missing_confirmation = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            data={
                "action": "reconcile",
                "messaging_service_sid": "MGlive123",
                "customer_profile_sid": "BUcustomer123",
                "trust_product_sid": "BUtrust123",
                "brand_registration_sid": "BNlive123",
            },
            follow_redirects=False,
        )
        self.assertEqual(missing_confirmation.status_code, 200)
        self.assertIn(b"Confirm the Twilio state reconcile", missing_confirmation.data)
        mock_reconcile.assert_not_called()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            data={
                "action": "reconcile",
                "messaging_service_sid": "MGlive123",
                "customer_profile_sid": "BUcustomer123",
                "trust_product_sid": "BUtrust123",
                "brand_registration_sid": "BNlive123",
                "confirm_reconcile": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        mock_reconcile.assert_called_once()

    @patch("app.routes.create_missing_a2p_campaign")
    def test_platform_admin_create_campaign_action_requires_confirmation_and_calls_service(self, mock_create_campaign) -> None:
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="needs_action",
            brand_status="approved",
            brand_registration_sid="BNlive123",
            raw_status_json=json.dumps(
                {
                    "recovery_state": {
                        "type": "missing_campaign",
                        "recommended_action": "create_campaign",
                        "summary": "Twilio approved the packet, but no campaign is attached.",
                        "stored": {},
                        "live": {},
                        "selected": {},
                        "missing": {"campaign_sid": True},
                        "only_missing_campaign": True,
                        "observed_ids": {},
                    }
                }
            ),
        )
        self.db.session.add(onboarding)
        self.db.session.commit()
        self._login_platform_admin()

        missing_confirmation = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            data={"action": "create_campaign"},
            follow_redirects=False,
        )
        self.assertEqual(missing_confirmation.status_code, 200)
        self.assertIn(b"Confirm campaign creation", missing_confirmation.data)
        mock_create_campaign.assert_not_called()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            data={
                "action": "create_campaign",
                "confirm_campaign_create": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        mock_create_campaign.assert_called_once()

    @patch("app.routes.refresh_a2p_onboarding")
    @patch("app.routes.validate_inbound_signature_detailed")
    def test_trusthub_webhook_falls_back_to_subaccount_and_records_observed_ids(
        self,
        mock_validate,
        mock_refresh,
    ) -> None:
        mock_validate.return_value = MagicMock(is_valid=True, reason="valid")
        onboarding = self.OrganizationA2POnboarding(
            organization_id=self.organization.id,
            onboarding_status="pending",
            raw_status_json=json.dumps({"brand_tcr_id": "TCR123"}),
        )
        self.db.session.add(onboarding)
        self.db.session.commit()

        response = self.client.post(
            "/webhooks/twilio/trusthub-status",
            data={
                "AccountSid": self.messaging_profile.twilio_subaccount_sid,
                "MessagingServiceSid": "MGlive123",
                "BrandTcrId": "TCR123",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 204)
        mock_refresh.assert_called_once_with(self.organization.id)
        payload = json.loads(onboarding.raw_status_json)
        self.assertEqual(payload["recovery_state"]["observed_ids"]["messaging_service_sid"], "MGlive123")

    def test_owner_cannot_access_platform_onboarding_route(self) -> None:
        self._login_owner()

        response = self.client.get(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

    def test_staff_cannot_access_platform_onboarding_route(self) -> None:
        staff_user = self.AppUser(
            username="staff-user-a2p",
            email="staff-a2p@acme.test",
            full_name="Staff User",
            phone="+15550000010",
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
            data={"username": "staff-a2p@acme.test", "password": "Staff-pass1!"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        route_response = self.client.get(
            f"/platform/organizations/{self.organization.id}/messaging/onboarding",
            follow_redirects=False,
        )
        self.assertEqual(route_response.status_code, 403)

    def test_invitation_accept_creates_membership_and_redirects_owner_to_setup(self) -> None:
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
        self.assertIn("/setup", response.headers.get("Location", ""))
        self.assertNotIn("/billing/checkout", response.headers.get("Location", ""))
        user = self.AppUser.query.filter_by(email="new-owner@acme.test").first()
        self.assertIsNotNone(user)
        membership = self.OrganizationMembership.query.filter_by(user_id=user.id).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.organization_id, self.organization.id)
        self.assertEqual(membership.role, "owner")
        recipients = self.OrganizationTestRecipient.query.filter_by(
            organization_id=self.organization.id
        ).all()
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].phone, "+15550000003")
        self.assertEqual(recipients[0].label, "New Owner")

    def test_invitation_accept_redirects_staff_to_pending_setup_when_workspace_is_not_live(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="new-staff@acme.test",
            role="staff",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        response = self.client.post(
            f"/invites/{invitation.token}",
            data={
                "username": "new-staff",
                "full_name": "New Staff",
                "phone": "+15550000004",
                "password": "Stronger-pass1!",
                "confirm_password": "Stronger-pass1!",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/setup/pending", response.headers.get("Location", ""))
        user = self.AppUser.query.filter_by(email="new-staff@acme.test").first()
        self.assertIsNotNone(user)
        membership = self.OrganizationMembership.query.filter_by(user_id=user.id).first()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.organization_id, self.organization.id)
        self.assertEqual(membership.role, "staff")

    def test_staff_invitation_page_describes_automatic_workspace_routing(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="pending-staff-copy@acme.test",
            role="staff",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        response = self.client.get(f"/invites/{invitation.token}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"route you to the right workspace page automatically", response.data)
        self.assertNotIn(b"land in the workspace dashboard", response.data)

    def test_invitation_accept_rejects_platform_admin_email(self) -> None:
        invitation = self.OrganizationInvitation(
            organization_id=self.organization.id,
            email="platform@acme.test",
            role="owner",
            status="pending",
        )
        self.db.session.add(invitation)
        self.db.session.commit()

        response = self.client.post(
            f"/invites/{invitation.token}",
            data={
                "username": "platform-org-owner",
                "full_name": "Platform Admin Owner",
                "phone": "+15550000012",
                "password": "Stronger-pass1!",
                "confirm_password": "Stronger-pass1!",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Platform admin accounts cannot be assigned to an organization.",
            response.data,
        )
        self.assertEqual(self.OrganizationMembership.query.filter_by(user_id=self.platform_admin.id).count(), 0)

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
        self.assertIn("/setup/pending", response.headers.get("Location", ""))
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

    def test_team_invite_is_available_while_a2p_review_is_pending(self) -> None:
        self.subscription.status = "trialing"
        self.messaging_profile.status = "pending"
        self.messaging_profile.provider_status = "pending"
        self.messaging_profile.sender_review_status = "pending"
        self.messaging_profile.from_number = None
        self.messaging_profile.inbound_identity = None
        self.messaging_profile.phone_number_sid = None
        self.db.session.commit()

        self._login_owner()
        response = self.client.get("/team/invite", follow_redirects=False)

        self.assertEqual(response.status_code, 200)

    def test_team_invite_requires_billing_activation(self) -> None:
        self._login_owner()
        response = self.client.get("/team/invite", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/setup?step=billing", response.headers.get("Location", ""))

    def test_team_invite_rejects_platform_admin_email(self) -> None:
        self.subscription.status = "trialing"
        self.db.session.commit()

        self._login_owner()
        response = self.client.post(
            "/team/invite",
            data={
                "email": "platform@acme.test",
                "role": "staff",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Platform admin accounts cannot be assigned to an organization.",
            response.data,
        )
        self.assertIsNone(
            self.OrganizationInvitation.query.filter_by(email="platform@acme.test", status="pending").first()
        )

    def test_team_invite_allows_same_email_when_pending_invite_is_in_another_organization(self) -> None:
        self.subscription.status = "trialing"
        self.db.session.commit()
        other_organization, _other_owner_invite = self._create_support_organization(
            name="Other Co",
            slug="other-co",
            owner_email="other-owner@acme.test",
        )
        self.db.session.add(
            self.OrganizationInvitation(
                organization_id=other_organization.id,
                email="shared-staff@acme.test",
                role="staff",
                status="pending",
                invited_by_user_id=self.platform_admin.id,
            )
        )
        self.db.session.commit()

        self._login_owner()
        response = self.client.post(
            "/team/invite",
            data={
                "email": "shared-staff@acme.test",
                "role": "staff",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        current_workspace_invite = self.OrganizationInvitation.query.filter_by(
            organization_id=self.organization.id,
            email="shared-staff@acme.test",
            status="pending",
        ).first()
        other_workspace_invite = self.OrganizationInvitation.query.filter_by(
            organization_id=other_organization.id,
            email="shared-staff@acme.test",
            status="pending",
        ).first()
        self.assertIsNotNone(current_workspace_invite)
        self.assertIsNotNone(other_workspace_invite)

    def test_owner_cannot_post_platform_restart_services(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        self._login_owner()

        response = self.client.post("/platform/operations/restart-services", follow_redirects=False)

        self.assertEqual(response.status_code, 403)

    def test_platform_restart_services_creates_durable_request_and_records_queued_auth_event(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True

        self._login_platform_admin()
        response = self.client.post(
            "/platform/operations/restart-services",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Restart request queued. Waiting for the host processor.", response.data)
        self.assertIn(b"Last request: Queued", response.data)
        restart_request = (
            self.PlatformServiceRestartRequest.query
            .order_by(self.PlatformServiceRestartRequest.id.desc())
            .first()
        )
        self.assertIsNotNone(restart_request)
        self.assertEqual(restart_request.status, "pending")
        self.assertEqual(restart_request.requested_by_user_id, self.platform_admin.id)
        self.assertEqual(restart_request.requested_username, "platform-admin")
        event = (
            self.AuthEvent.query
            .filter_by(event_type="platform_service_restart")
            .order_by(self.AuthEvent.id.desc())
            .first()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, "queued")
        self.assertEqual(event.metadata_payload.get("request_id"), restart_request.id)
        self.assertEqual(event.metadata_payload.get("status"), "pending")

    def test_platform_restart_services_dedupes_active_request(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        restart_request = self.PlatformServiceRestartRequest(
            requested_by_user_id=self.platform_admin.id,
            requested_username=self.platform_admin.username,
            client_ip="127.0.0.1",
            status="queued",
            transient_unit="twinevia-saas-manual-restart-123",
            summary="Restart queued. The SaaS services are restarting.",
        )
        self.db.session.add(restart_request)
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.post(
            "/platform/operations/restart-services",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Restart queued. The SaaS services are restarting.", response.data)
        self.assertEqual(self.PlatformServiceRestartRequest.query.count(), 1)
        self.assertEqual(self.AuthEvent.query.filter_by(event_type="platform_service_restart").count(), 0)

    def test_platform_home_shows_queued_restart_request(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        self.db.session.add(
            self.PlatformServiceRestartRequest(
                requested_by_user_id=self.platform_admin.id,
                requested_username=self.platform_admin.username,
                client_ip="127.0.0.1",
                status="queued",
                transient_unit="twinevia-saas-manual-restart-555",
                summary="Restart queued. The SaaS services are restarting.",
            )
        )
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Service restart", response.data)
        self.assertIn(b"Last request: Queued", response.data)
        self.assertIn(b"Restart queued. The SaaS services are restarting.", response.data)

    def test_platform_home_shows_succeeded_restart_request(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        self.db.session.add(
            self.PlatformServiceRestartRequest(
                requested_by_user_id=self.platform_admin.id,
                requested_username=self.platform_admin.username,
                client_ip="127.0.0.1",
                status="succeeded",
                transient_unit="twinevia-saas-manual-restart-777",
                summary="Restart completed successfully.",
                detail="Transient unit twinevia-saas-manual-restart-777 completed with result success.",
                completed_at=self.organization.created_at,
            )
        )
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Service restart", response.data)
        self.assertIn(b"Last request: Succeeded", response.data)
        self.assertIn(b"Restart completed successfully.", response.data)

    def test_platform_home_shows_failed_restart_request(self) -> None:
        self.app.config["PLATFORM_SERVICE_RESTART_ENABLED"] = True
        self.db.session.add(
            self.PlatformServiceRestartRequest(
                requested_by_user_id=self.platform_admin.id,
                requested_username=self.platform_admin.username,
                client_ip="127.0.0.1",
                status="failed",
                transient_unit="twinevia-saas-manual-restart-999",
                summary="Restart failed.",
                detail="Transient unit twinevia-saas-manual-restart-999 finished with result failed.",
                completed_at=self.organization.created_at,
            )
        )
        self.db.session.commit()

        self._login_platform_admin()
        response = self.client.get("/platform")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Service restart", response.data)
        self.assertIn(b"Last request: Failed", response.data)
        self.assertIn(b"Restart failed.", response.data)

    @patch("app.routes.suspend_org")
    def test_platform_toggle_status_rolls_back_when_provider_suspend_fails(self, mock_suspend_org) -> None:
        from app.services.twilio_service import ProviderProvisioningError

        mock_suspend_org.side_effect = ProviderProvisioningError("Twilio unavailable")
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/toggle-status",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Could not update organization status to suspended.", response.data)
        self.db.session.expire_all()
        organization = self.db.session.get(self.Organization, self.organization.id)
        self.assertEqual(organization.status, "active")

    @patch("app.routes.suspend_org")
    def test_platform_toggle_status_updates_organization_after_provider_suspend(self, mock_suspend_org) -> None:
        mock_suspend_org.return_value = self.messaging_profile
        self._login_platform_admin()

        response = self.client.post(
            f"/platform/organizations/{self.organization.id}/toggle-status",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Organization status updated to suspended.", response.data)
        self.db.session.expire_all()
        organization = self.db.session.get(self.Organization, self.organization.id)
        self.assertEqual(organization.status, "suspended")

    @patch("app.queue.get_queue")
    def test_dashboard_queue_preflight_failure_does_not_create_message_log(self, mock_get_queue) -> None:
        self.subscription.status = "trialing"
        self.db.session.add(
            self.CommunityMember(
                organization_id=self.organization.id,
                name="Queue Target",
                phone="+15550001010",
            )
        )
        self.db.session.commit()

        mock_queue = MagicMock()
        mock_queue.connection.ping.side_effect = RuntimeError("redis unavailable")
        mock_get_queue.return_value = mock_queue

        self._login_owner()
        before_logs = self.MessageLog.query.count()
        response = self.client.post(
            "/dashboard",
            data={
                "message_body": "Hello queue",
                "target": "community",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Background queue is unavailable right now. The blast was not queued. Check Redis/worker health and try again.",
            response.data,
        )
        self.assertEqual(self.MessageLog.query.count(), before_logs)
        mock_queue.enqueue.assert_not_called()

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

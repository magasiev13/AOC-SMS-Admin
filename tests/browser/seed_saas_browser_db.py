#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path


def _remove_db_files(db_path: Path) -> None:
    for candidate in (
        db_path,
        db_path.with_suffix(db_path.suffix + "-wal"),
        db_path.with_suffix(db_path.suffix + "-shm"),
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    db_path_arg = sys.argv[1] if len(sys.argv) > 1 else ".playwright/browser-tests.db"
    base_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:5010"
    db_path = Path(db_path_arg).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_db_files(db_path)

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["FLASK_DEBUG"] = "0"
    os.environ["SAAS_MODE"] = "1"
    os.environ["SCHEDULER_ENABLED"] = "0"
    os.environ["SECRET_KEY"] = "playwright-browser-secret"
    os.environ["SAAS_BASE_URL"] = base_url
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_browser"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_browser"
    os.environ["STRIPE_PRICE_ID"] = "price_browser"
    os.environ["TWILIO_CREDENTIAL_ENCRYPTION_KEY"] = "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o="

    from app import create_app, db
    from app.models import (
        AppUser,
        CommunityMember,
        Event,
        EventRegistration,
        Organization,
        OrganizationA2POnboarding,
        OrganizationInvitation,
        OrganizationMembership,
        OrganizationMessagingProfile,
        OrganizationSubscription,
        utc_now,
    )
    from app.migrations.runner import run_pending_migrations
    from app.saas_migrations.runner import run_pending_saas_migrations

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        db.create_all()
        run_pending_migrations(db.engine, app.logger)
        run_pending_saas_migrations(db.engine, app.logger)

        now = utc_now()

        def make_user(*, username: str, email: str, full_name: str, phone: str, password: str, role: str = "admin", is_platform_admin: bool = False) -> AppUser:
            user = AppUser(
                username=username,
                email=email,
                full_name=full_name,
                phone=phone,
                role=role,
                is_platform_admin=is_platform_admin,
                must_change_password=False,
            )
            user.set_password(password)
            return user

        platform_admin = make_user(
            username="platform-admin",
            email="platform@browser.test",
            full_name="Platform Admin",
            phone="+15550001001",
            password="Platform-pass1!",
            is_platform_admin=True,
        )
        owner = make_user(
            username="owner-browser",
            email="owner@browser.test",
            full_name="Owner Browser",
            phone="+15550001002",
            password="Owner-pass1!",
        )
        staff = make_user(
            username="staff-browser",
            email="staff@browser.test",
            full_name="Staff Browser",
            phone="+15550001003",
            password="Staff-pass1!",
            role="social_manager",
        )
        trial_owner = make_user(
            username="trial-owner-browser",
            email="trial-owner@browser.test",
            full_name="Trial Owner Browser",
            phone="+15550001004",
            password="TrialOwner-pass1!",
        )
        pending_a2p_owner = make_user(
            username="pending-a2p-owner",
            email="pending-a2p-owner@browser.test",
            full_name="Pending A2P Owner",
            phone="+15550001005",
            password="PendingA2P-pass1!",
        )
        past_due_owner = make_user(
            username="past-due-owner",
            email="past-due-owner@browser.test",
            full_name="Past Due Owner",
            phone="+15550001006",
            password="PastDue-pass1!",
        )
        suspended_owner = make_user(
            username="suspended-owner",
            email="suspended-owner@browser.test",
            full_name="Suspended Owner",
            phone="+15550001007",
            password="Suspended-pass1!",
        )
        isolation_owner = make_user(
            username="isolation-owner",
            email="isolation-owner@browser.test",
            full_name="Isolation Owner",
            phone="+15550001008",
            password="Isolation-pass1!",
        )

        onboarding_org = Organization(name="Onboarding Bakery", slug="onboarding-bakery", status="active")
        onboarding_subscription = OrganizationSubscription(
            organization=onboarding_org,
            stripe_price_id="price_browser",
            status="incomplete",
        )
        onboarding_invite = OrganizationInvitation(
            organization=onboarding_org,
            email="new-owner@browser.test",
            role="owner",
            status="pending",
            token="browser-owner-invite-token",
            expires_at=now + timedelta(days=7),
        )
        onboarding_messaging = OrganizationMessagingProfile(
            organization=onboarding_org,
            status="pending",
            provider_status="pending",
        )

        setup_org = Organization(name="Setup Runway Bakery", slug="setup-runway-bakery", status="active")
        setup_subscription = OrganizationSubscription(
            organization=setup_org,
            stripe_price_id="price_browser",
            status="incomplete",
        )
        setup_messaging = OrganizationMessagingProfile(
            organization=setup_org,
            status="pending",
            provider_status="pending",
        )

        active_org = Organization(name="Acme Bakery", slug="acme-bakery", status="active")
        active_subscription = OrganizationSubscription(
            organization=active_org,
            stripe_customer_id="cus_browser_123",
            stripe_subscription_id="sub_browser_123",
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=now + timedelta(days=14),
        )
        active_messaging = OrganizationMessagingProfile(
            organization=active_org,
            twilio_subaccount_sid="ACactive0001",
            messaging_service_sid="MGactive0001",
            phone_number_sid="PNactive0001",
            from_number="+15550001111",
            inbound_identity="+15550001111",
            status="active",
            provider_status="active",
            sender_review_status="approved",
            business_type="Bakery",
            use_case="Announcements",
            consent_acknowledged_at=now,
        )

        pending_review_org = Organization(name="Pending Review Bakery", slug="pending-review-bakery", status="active")
        pending_review_subscription = OrganizationSubscription(
            organization=pending_review_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=now + timedelta(days=14),
        )
        pending_review_messaging = OrganizationMessagingProfile(
            organization=pending_review_org,
            twilio_subaccount_sid="ACpending0001",
            messaging_service_sid="MGpending0001",
            status="pending",
            provider_status="pending",
            business_type="Bakery",
            use_case="Announcements",
        )
        pending_review_onboarding = OrganizationA2POnboarding(
            organization=pending_review_org,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="pending",
            brand_status="pending-review",
            campaign_status="pending",
            verification_status="pending",
            business_name="Pending Review Bakery",
            email="ops@pending.test",
            notification_email="alerts@pending.test",
            website_url="https://pending.test",
            first_name="Penny",
            last_name="Pending",
            business_title="Owner",
            job_position="Director",
            business_type="Limited Liability Corporation",
            business_industry="TECHNOLOGY",
            business_registration_identifier="EIN",
            business_regions_json='["USA_AND_CANADA"]',
            address_country="US",
            address_line1="123 Pending St",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Pending review messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Pending sample message 1", "Pending sample message 2"]',
            raw_submission_json='{"has_embedded_links": true, "has_embedded_phone": true}',
            submitted_at=now,
            last_synced_at=now,
        )

        approved_org = Organization(name="Approved Bakery", slug="approved-bakery", status="active")
        approved_subscription = OrganizationSubscription(
            organization=approved_org,
            stripe_price_id="price_browser",
            status="active",
            current_period_end=now + timedelta(days=30),
        )
        approved_messaging = OrganizationMessagingProfile(
            organization=approved_org,
            twilio_subaccount_sid="ACapproved0001",
            messaging_service_sid="MGapproved0001",
            phone_number_sid="PNapproved0001",
            from_number="+15550002222",
            inbound_identity="+15550002222",
            status="active",
            provider_status="active",
            sender_review_status="approved",
            business_type="Bakery",
            use_case="Announcements",
            consent_acknowledged_at=now,
        )
        approved_onboarding = OrganizationA2POnboarding(
            organization=approved_org,
            registration_path="standard",
            number_strategy="existing_subaccount_number",
            onboarding_status="approved",
            brand_status="approved",
            campaign_status="approved",
            business_name="Approved Bakery",
            email="ops@approved.test",
            notification_email="alerts@approved.test",
            website_url="https://approved.test",
            first_name="Avery",
            last_name="Approved",
            business_title="Owner",
            job_position="Director",
            business_type="Limited Liability Corporation",
            business_industry="TECHNOLOGY",
            business_registration_identifier="EIN",
            business_regions_json='["USA_AND_CANADA"]',
            address_country="US",
            address_line1="123 Approved St",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Approved messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Approved sample message 1", "Approved sample message 2"]',
            desired_phone_number_sid="PNapproved0001",
            submitted_at=now,
            approved_at=now,
            last_synced_at=now,
        )

        queued_org = Organization(name="Queued Bakery", slug="queued-bakery", status="active")
        queued_subscription = OrganizationSubscription(
            organization=queued_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=now + timedelta(days=14),
        )
        queued_messaging = OrganizationMessagingProfile(
            organization=queued_org,
            twilio_subaccount_sid="ACqueued0001",
            messaging_service_sid="MGqueued0001",
            status="pending",
            provider_status="pending",
            business_type="Bakery",
            use_case="Announcements",
        )
        queued_onboarding = OrganizationA2POnboarding(
            organization=queued_org,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="queued",
            business_name="Queued Bakery",
            email="ops@queued.test",
            notification_email="alerts@queued.test",
            website_url="https://queued.test",
            first_name="Quinn",
            last_name="Queue",
            business_title="Owner",
            job_position="Director",
            business_type="Limited Liability Corporation",
            business_industry="TECHNOLOGY",
            business_registration_identifier="EIN",
            business_regions_json='["USA_AND_CANADA"]',
            address_country="US",
            address_line1="123 Queue St",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Queued messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Queued sample message 1", "Queued sample message 2"]',
            submitted_at=now,
        )

        rejected_org = Organization(name="Rejected Bakery", slug="rejected-bakery", status="active")
        rejected_subscription = OrganizationSubscription(
            organization=rejected_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=now + timedelta(days=14),
        )
        rejected_messaging = OrganizationMessagingProfile(
            organization=rejected_org,
            twilio_subaccount_sid="ACrejected0001",
            messaging_service_sid="MGrejected0001",
            status="error",
            provider_status="error",
            last_provision_error="Twilio rejected the registration because the campaign description was too vague.",
            business_type="Bakery",
            use_case="Marketing",
        )
        rejected_onboarding = OrganizationA2POnboarding(
            organization=rejected_org,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="rejected",
            brand_status="approved",
            campaign_status="rejected",
            business_name="Rejected Bakery",
            email="ops@rejected.test",
            notification_email="alerts@rejected.test",
            website_url="https://rejected.test",
            first_name="Riley",
            last_name="Rejected",
            business_title="Owner",
            job_position="Director",
            business_type="Limited Liability Corporation",
            business_industry="TECHNOLOGY",
            business_registration_identifier="EIN",
            business_regions_json='["USA_AND_CANADA"]',
            address_country="US",
            address_line1="123 Rejected St",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Rejected campaign",
            message_flow="Users opt in on the website.",
            message_samples_json='["Rejected sample message 1", "Rejected sample message 2"]',
            submitted_at=now,
            last_synced_at=now,
            last_error="Twilio rejected the registration because the campaign description was too vague.",
        )

        error_org = Organization(name="Error Bakery", slug="error-bakery", status="active")
        error_subscription = OrganizationSubscription(
            organization=error_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=now + timedelta(days=14),
        )
        error_messaging = OrganizationMessagingProfile(
            organization=error_org,
            twilio_subaccount_sid="ACerror0001",
            messaging_service_sid="MGerror0001",
            status="error",
            provider_status="error",
            last_provision_error="Twilio A2P onboarding could not be queued. Check Redis/RQ and retry.",
            business_type="Bakery",
            use_case="Announcements",
        )
        error_onboarding = OrganizationA2POnboarding(
            organization=error_org,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="error",
            business_name="Error Bakery",
            email="ops@error.test",
            notification_email="alerts@error.test",
            website_url="https://error.test",
            first_name="Erin",
            last_name="Error",
            business_title="Owner",
            job_position="Director",
            business_type="Limited Liability Corporation",
            business_industry="TECHNOLOGY",
            business_registration_identifier="EIN",
            business_regions_json='["USA_AND_CANADA"]',
            address_country="US",
            address_line1="123 Error St",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Error messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Error sample message 1", "Error sample message 2"]',
            submitted_at=now,
            last_synced_at=now,
            last_error="Twilio A2P onboarding could not be queued. Check Redis/RQ and retry.",
        )

        canceled_org = Organization(name="Canceled Bakery", slug="canceled-bakery", status="active")
        canceled_subscription = OrganizationSubscription(
            organization=canceled_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=now + timedelta(days=14),
        )
        canceled_messaging = OrganizationMessagingProfile(
            organization=canceled_org,
            twilio_subaccount_sid="ACcanceled0001",
            messaging_service_sid="MGcanceled0001",
            status="pending",
            provider_status="pending",
            business_type="Bakery",
            use_case="Announcements",
        )
        canceled_onboarding = OrganizationA2POnboarding(
            organization=canceled_org,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="canceled",
            business_name="Canceled Bakery",
            email="ops@canceled.test",
            notification_email="alerts@canceled.test",
            website_url="https://canceled.test",
            first_name="Casey",
            last_name="Canceled",
            business_title="Owner",
            job_position="Director",
            business_type="Limited Liability Corporation",
            business_industry="TECHNOLOGY",
            business_registration_identifier="EIN",
            business_regions_json='["USA_AND_CANADA"]',
            address_country="US",
            address_line1="123 Canceled St",
            address_city="Denver",
            address_region="CO",
            address_postal_code="80202",
            campaign_description="Canceled messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Canceled sample message 1", "Canceled sample message 2"]',
            submitted_at=now,
            canceled_at=now,
        )

        past_due_org = Organization(name="Past Due Bakery", slug="past-due-bakery", status="active")
        past_due_subscription = OrganizationSubscription(
            organization=past_due_org,
            stripe_customer_id="cus_past_due_browser",
            stripe_subscription_id="sub_past_due_browser",
            stripe_price_id="price_browser",
            status="past_due",
            current_period_end=now - timedelta(days=2),
        )
        past_due_messaging = OrganizationMessagingProfile(
            organization=past_due_org,
            twilio_subaccount_sid="ACpastdue0001",
            messaging_service_sid="MGpastdue0001",
            phone_number_sid="PNpastdue0001",
            from_number="+15550003333",
            inbound_identity="+15550003333",
            status="active",
            provider_status="active",
            sender_review_status="approved",
            business_type="Bakery",
            use_case="Announcements",
            consent_acknowledged_at=now,
        )

        suspended_org = Organization(name="Suspended Bakery", slug="suspended-bakery", status="suspended")
        suspended_subscription = OrganizationSubscription(
            organization=suspended_org,
            stripe_customer_id="cus_suspended_browser",
            stripe_subscription_id="sub_suspended_browser",
            stripe_price_id="price_browser",
            status="past_due",
            current_period_end=now - timedelta(days=4),
        )
        suspended_messaging = OrganizationMessagingProfile(
            organization=suspended_org,
            twilio_subaccount_sid="ACsuspended0001",
            messaging_service_sid="MGsuspended0001",
            phone_number_sid="PNsuspended0001",
            from_number="+15550004444",
            inbound_identity="+15550004444",
            status="suspended",
            provider_status="suspended",
            sender_review_status="approved",
            business_type="Bakery",
            use_case="Announcements",
            consent_acknowledged_at=now,
            suspended_at=now,
        )

        isolation_org = Organization(name="Northstar Fitness", slug="northstar-fitness", status="active")
        isolation_subscription = OrganizationSubscription(
            organization=isolation_org,
            stripe_customer_id="cus_isolation_browser",
            stripe_subscription_id="sub_isolation_browser",
            stripe_price_id="price_browser",
            status="active",
            current_period_end=now + timedelta(days=21),
        )
        isolation_messaging = OrganizationMessagingProfile(
            organization=isolation_org,
            twilio_subaccount_sid="ACisolation0001",
            messaging_service_sid="MGisolation0001",
            phone_number_sid="PNisolation0001",
            from_number="+15550005555",
            inbound_identity="+15550005555",
            status="active",
            provider_status="active",
            sender_review_status="approved",
            business_type="Fitness",
            use_case="Class reminders",
            consent_acknowledged_at=now,
        )

        db.session.add_all(
            [
                platform_admin,
                owner,
                staff,
                trial_owner,
                pending_a2p_owner,
                past_due_owner,
                suspended_owner,
                isolation_owner,
                onboarding_org,
                onboarding_subscription,
                onboarding_invite,
                onboarding_messaging,
                setup_org,
                setup_subscription,
                setup_messaging,
                active_org,
                active_subscription,
                active_messaging,
                pending_review_org,
                pending_review_subscription,
                pending_review_messaging,
                pending_review_onboarding,
                approved_org,
                approved_subscription,
                approved_messaging,
                approved_onboarding,
                queued_org,
                queued_subscription,
                queued_messaging,
                queued_onboarding,
                rejected_org,
                rejected_subscription,
                rejected_messaging,
                rejected_onboarding,
                error_org,
                error_subscription,
                error_messaging,
                error_onboarding,
                canceled_org,
                canceled_subscription,
                canceled_messaging,
                canceled_onboarding,
                past_due_org,
                past_due_subscription,
                past_due_messaging,
                suspended_org,
                suspended_subscription,
                suspended_messaging,
                isolation_org,
                isolation_subscription,
                isolation_messaging,
            ]
        )
        db.session.flush()

        db.session.add_all(
            [
                OrganizationMembership(organization_id=setup_org.id, user_id=trial_owner.id, role="owner"),
                OrganizationMembership(organization_id=active_org.id, user_id=owner.id, role="owner"),
                OrganizationMembership(organization_id=active_org.id, user_id=staff.id, role="staff"),
                OrganizationMembership(organization_id=pending_review_org.id, user_id=pending_a2p_owner.id, role="owner"),
                OrganizationMembership(organization_id=past_due_org.id, user_id=past_due_owner.id, role="owner"),
                OrganizationMembership(organization_id=suspended_org.id, user_id=suspended_owner.id, role="owner"),
                OrganizationMembership(organization_id=isolation_org.id, user_id=isolation_owner.id, role="owner"),
                OrganizationInvitation(
                    organization_id=active_org.id,
                    email="pending-staff@browser.test",
                    role="staff",
                    status="pending",
                    token="browser-staff-invite-token",
                    invited_by_user_id=owner.id,
                    expires_at=now + timedelta(days=7),
                ),
            ]
        )

        acme_members = [
            CommunityMember(organization_id=active_org.id, name="Maya Chen", phone="+17205550101"),
            CommunityMember(organization_id=active_org.id, name="Alex Rivera", phone="+17205550102"),
        ]
        northstar_members = [
            CommunityMember(organization_id=isolation_org.id, name="Taylor Reed", phone="+17205550201"),
            CommunityMember(organization_id=isolation_org.id, name="Chris Lopez", phone="+17205550202"),
        ]
        db.session.add_all(acme_members + northstar_members)
        db.session.flush()

        acme_event = Event(
            organization_id=active_org.id,
            title="Acme Spring Launch",
            date=(now + timedelta(days=7)).date(),
        )
        northstar_event = Event(
            organization_id=isolation_org.id,
            title="Northstar Bootcamp",
            date=(now + timedelta(days=9)).date(),
        )
        db.session.add_all([acme_event, northstar_event])
        db.session.flush()

        db.session.add_all(
            [
                EventRegistration(
                    organization_id=active_org.id,
                    event_id=acme_event.id,
                    name="Acme RSVP",
                    phone="+17205550103",
                ),
                EventRegistration(
                    organization_id=isolation_org.id,
                    event_id=northstar_event.id,
                    name="Northstar RSVP",
                    phone="+17205550203",
                ),
            ]
        )

        db.session.commit()


if __name__ == "__main__":
    main()

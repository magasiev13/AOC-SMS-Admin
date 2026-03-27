#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path


def _remove_db_files(db_path: Path) -> None:
    for candidate in (db_path, db_path.with_suffix(db_path.suffix + "-wal"), db_path.with_suffix(db_path.suffix + "-shm")):
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

        platform_admin = AppUser(
            username="platform-admin",
            email="platform@browser.test",
            full_name="Platform Admin",
            phone="+15550001001",
            role="admin",
            is_platform_admin=True,
            must_change_password=False,
        )
        platform_admin.set_password("Platform-pass1!")

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
            expires_at=utc_now() + timedelta(days=7),
        )
        onboarding_messaging = OrganizationMessagingProfile(
            organization=onboarding_org,
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
            current_period_end=utc_now() + timedelta(days=14),
        )
        active_messaging = OrganizationMessagingProfile(
            organization=active_org,
            from_number="+15550001111",
            messaging_service_sid="MGbrowser1111",
            inbound_identity="+15550001111",
            status="active",
            provider_status="active",
            sender_review_status="approved",
        )

        queued_org = Organization(name="Queued Bakery", slug="queued-bakery", status="active")
        queued_subscription = OrganizationSubscription(
            organization=queued_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=utc_now() + timedelta(days=14),
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
            first_name="Quinn",
            last_name="Queue",
            campaign_description="Queued messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Queued sample message"]',
            submitted_at=utc_now(),
        )

        pending_org = Organization(name="Pending Review Bakery", slug="pending-review-bakery", status="active")
        pending_subscription = OrganizationSubscription(
            organization=pending_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=utc_now() + timedelta(days=14),
        )
        pending_messaging = OrganizationMessagingProfile(
            organization=pending_org,
            twilio_subaccount_sid="ACpending0001",
            messaging_service_sid="MGpending0001",
            status="pending",
            provider_status="pending",
            business_type="Bakery",
            use_case="Announcements",
        )
        pending_onboarding = OrganizationA2POnboarding(
            organization=pending_org,
            registration_path="standard",
            number_strategy="auto_buy",
            onboarding_status="pending",
            brand_status="pending-review",
            campaign_status="pending",
            verification_status="pending",
            business_name="Pending Review Bakery",
            email="ops@pending.test",
            first_name="Penny",
            last_name="Pending",
            campaign_description="Pending review messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Pending sample message"]',
            raw_submission_json='{"has_embedded_links": true, "has_embedded_phone": true}',
            submitted_at=utc_now(),
            last_synced_at=utc_now(),
        )

        approved_org = Organization(name="Approved Bakery", slug="approved-bakery", status="active")
        approved_subscription = OrganizationSubscription(
            organization=approved_org,
            stripe_price_id="price_browser",
            status="active",
            current_period_end=utc_now() + timedelta(days=30),
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
            consent_acknowledged_at=utc_now(),
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
            first_name="Avery",
            last_name="Approved",
            campaign_description="Approved messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Approved sample message"]',
            desired_phone_number_sid="PNapproved0001",
            submitted_at=utc_now(),
            approved_at=utc_now(),
            last_synced_at=utc_now(),
        )

        rejected_org = Organization(name="Rejected Bakery", slug="rejected-bakery", status="active")
        rejected_subscription = OrganizationSubscription(
            organization=rejected_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=utc_now() + timedelta(days=14),
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
            first_name="Riley",
            last_name="Rejected",
            campaign_description="Rejected campaign",
            message_flow="Users opt in on the website.",
            message_samples_json='["Rejected sample message"]',
            submitted_at=utc_now(),
            last_synced_at=utc_now(),
            last_error="Twilio rejected the registration because the campaign description was too vague.",
        )

        error_org = Organization(name="Error Bakery", slug="error-bakery", status="active")
        error_subscription = OrganizationSubscription(
            organization=error_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=utc_now() + timedelta(days=14),
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
            first_name="Erin",
            last_name="Error",
            campaign_description="Error messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Error sample message"]',
            submitted_at=utc_now(),
            last_synced_at=utc_now(),
            last_error="Twilio A2P onboarding could not be queued. Check Redis/RQ and retry.",
        )

        canceled_org = Organization(name="Canceled Bakery", slug="canceled-bakery", status="active")
        canceled_subscription = OrganizationSubscription(
            organization=canceled_org,
            stripe_price_id="price_browser",
            status="trialing",
            current_period_end=utc_now() + timedelta(days=14),
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
            first_name="Casey",
            last_name="Canceled",
            campaign_description="Canceled messages",
            message_flow="Users opt in on the website.",
            message_samples_json='["Canceled sample message"]',
            submitted_at=utc_now(),
            canceled_at=utc_now(),
        )

        owner = AppUser(
            username="owner-browser",
            email="owner@browser.test",
            full_name="Owner Browser",
            phone="+15550001002",
            role="admin",
            must_change_password=False,
        )
        owner.set_password("Owner-pass1!")
        trial_owner = AppUser(
            username="trial-owner-browser",
            email="trial-owner@browser.test",
            full_name="Trial Owner Browser",
            phone="+15550001004",
            role="admin",
            must_change_password=False,
        )
        trial_owner.set_password("TrialOwner-pass1!")
        staff = AppUser(
            username="staff-browser",
            email="staff@browser.test",
            full_name="Staff Browser",
            phone="+15550001003",
            role="social_manager",
            must_change_password=False,
        )
        staff.set_password("Staff-pass1!")

        db.session.add_all([
            platform_admin,
            onboarding_org,
            onboarding_subscription,
            onboarding_invite,
            onboarding_messaging,
            active_org,
            active_subscription,
            active_messaging,
            queued_org,
            queued_subscription,
            queued_messaging,
            queued_onboarding,
            pending_org,
            pending_subscription,
            pending_messaging,
            pending_onboarding,
            approved_org,
            approved_subscription,
            approved_messaging,
            approved_onboarding,
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
            owner,
            trial_owner,
            staff,
        ])
        db.session.flush()
        db.session.add_all([
            OrganizationMembership(
                organization_id=onboarding_org.id,
                user_id=trial_owner.id,
                role="owner",
            ),
            OrganizationMembership(
                organization_id=active_org.id,
                user_id=owner.id,
                role="owner",
            ),
            OrganizationMembership(
                organization_id=active_org.id,
                user_id=staff.id,
                role="staff",
            ),
            OrganizationInvitation(
                organization_id=active_org.id,
                email="pending-staff@browser.test",
                role="staff",
                status="pending",
                token="browser-staff-invite-token",
                invited_by_user_id=owner.id,
                expires_at=utc_now() + timedelta(days=7),
            ),
        ])
        db.session.commit()


if __name__ == "__main__":
    main()

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

        owner = AppUser(
            username="owner-browser",
            email="owner@browser.test",
            full_name="Owner Browser",
            phone="+15550001002",
            role="admin",
            must_change_password=False,
        )
        owner.set_password("Owner-pass1!")
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
            owner,
            staff,
        ])
        db.session.flush()
        db.session.add_all([
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

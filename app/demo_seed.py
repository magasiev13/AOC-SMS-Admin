from __future__ import annotations

import argparse
import importlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


PLATFORM_ADMIN_PASSWORD = "Platform-pass123!"
OWNER_PASSWORD = "Owner-pass123!"
STAFF_PASSWORD = "Staff-pass123!"


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


def _sqlite_db_path(database_url: str) -> Path | None:
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    if parsed.database in {None, "", ":memory:"}:
        return None
    return Path(parsed.database).expanduser().resolve()


def _seed_env_defaults(*, database_url: str | None, base_url: str | None) -> None:
    load_dotenv()
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    if base_url:
        os.environ["SAAS_BASE_URL"] = base_url.rstrip("/")

    os.environ.setdefault("FLASK_DEBUG", "1")
    os.environ["SAAS_MODE"] = "1"
    os.environ["SCHEDULER_ENABLED"] = "0"
    os.environ.setdefault("SECRET_KEY", "demo-seed-secret")
    os.environ.setdefault("SAAS_BASE_URL", "http://127.0.0.1:5000")
    os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_demo")
    os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_demo")
    os.environ.setdefault("STRIPE_PRICE_ID", "price_demo")
    os.environ.setdefault("STRIPE_MONTHLY_PRICE_ID", "price_demo")
    os.environ.setdefault("STRIPE_ANNUAL_PRICE_ID", "price_demo_annual")
    os.environ.setdefault("STRIPE_ACTIVATION_PRICE_ID", "price_demo_activation")
    os.environ.setdefault("TWILIO_CREDENTIAL_ENCRYPTION_KEY", "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o=")


def _message_log_details(*, delivered: list[str], failed: list[dict[str, str]] | None = None) -> str:
    payload: list[dict[str, str]] = [
        {"phone": phone, "status": "sent"}
        for phone in delivered
    ]
    for item in failed or []:
        payload.append(
            {
                "phone": item["phone"],
                "status": "failed",
                "error": item["error"],
            }
        )
    return json.dumps(payload)


def _print_summary(summary: dict[str, Any]) -> None:
    print("")
    print("Demo seed complete.")
    print(f"Database: {summary['database_url']}")
    print("")
    print("Accounts")
    for account in summary["accounts"]:
        print(f"- {account['label']}: {account['email']} ({account['home']})")
    print("")
    print("Organizations")
    for organization in summary["organizations"]:
        print(
            f"- {organization['name']}: org={organization['status']}, "
            f"billing={organization['billing_status']}, messaging={organization['messaging_status']}"
        )
    print("")
    print("Pending invitations")
    for invite in summary["pending_invites"]:
        print(f"- {invite['organization']} {invite['role']} -> {invite['email']}")
    print("Open pending invitations from the platform or organization access screen.")
    if summary["live_sender_note"]:
        print("")
        print(summary["live_sender_note"])


def seed_demo_database(
    *,
    reset: bool = False,
    database_url: str | None = None,
    base_url: str | None = None,
    live_from_number: str | None = None,
    live_messaging_service_sid: str | None = None,
) -> dict[str, Any]:
    if live_messaging_service_sid and not live_from_number:
        raise RuntimeError("A live sender number is required when a Messaging Service SID is provided.")

    _seed_env_defaults(database_url=database_url, base_url=base_url)
    configured_database_url = os.environ["DATABASE_URL"]
    sqlite_db_path = _sqlite_db_path(configured_database_url)
    if sqlite_db_path is None:
        raise RuntimeError("Demo seeding is restricted to a file-backed local SQLite database.")
    if live_from_number or live_messaging_service_sid:
        raise RuntimeError("Demo seeding cannot attach a live Twilio sender.")

    if reset:
        sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
        _remove_db_files(sqlite_db_path)

    import app.config

    importlib.reload(app.config)

    from app import create_app, db
    from app.migrations.runner import run_pending_migrations
    from app.models import (
        AppUser,
        AuthEvent,
        CommunityMember,
        Event,
        EventRegistration,
        InboxMessage,
        InboxThread,
        KeywordAutomationRule,
        MessageLog,
        Organization,
        OrganizationInvitation,
        OrganizationMembership,
        OrganizationMessagingProfile,
        OrganizationSubscription,
        ScheduledMessage,
        SuppressedContact,
        SurveyFlow,
        SurveyResponse,
        SurveySession,
        UnsubscribedContact,
        utc_now,
    )

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        db.create_all()
        if db.engine.url.drivername.startswith("sqlite"):
            run_pending_migrations(db.engine, app.logger)

        if AppUser.query.first() or Organization.query.first():
            raise RuntimeError(
                "Refusing to seed a non-empty database. Use --reset with a local SQLite DB or point to a fresh database."
            )

        now = utc_now()
        today = now.date()
        stripe_price_id = app.config.get("STRIPE_MONTHLY_PRICE_ID") or app.config.get("STRIPE_PRICE_ID") or "price_demo"

        def make_user(
            *,
            username: str,
            email: str,
            full_name: str,
            phone: str,
            role: str,
            password: str,
            is_platform_admin: bool = False,
        ) -> AppUser:
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
            username="platform-admin-demo",
            email="platform@demo.test",
            full_name="Platform Admin",
            phone="+15550001001",
            role="admin",
            password=PLATFORM_ADMIN_PASSWORD,
            is_platform_admin=True,
        )

        internal_owner = make_user(
            username="aoc-owner",
            email="owner@twineviainternal.demo.test",
            full_name="Avery Cole",
            phone="+15550001002",
            role="admin",
            password=OWNER_PASSWORD,
        )
        internal_staff = make_user(
            username="aoc-staff",
            email="staff@twineviainternal.demo.test",
            full_name="Riley Morgan",
            phone="+15550001003",
            role="social_manager",
            password=STAFF_PASSWORD,
        )
        northstar_owner = make_user(
            username="northstar-owner",
            email="owner@northstar.demo.test",
            full_name="Jamie Torres",
            phone="+15550001004",
            role="admin",
            password=OWNER_PASSWORD,
        )
        northstar_staff = make_user(
            username="northstar-staff",
            email="staff@northstar.demo.test",
            full_name="Taylor Reed",
            phone="+15550001005",
            role="social_manager",
            password=STAFF_PASSWORD,
        )
        sunset_owner = make_user(
            username="sunset-owner",
            email="owner@sunset.demo.test",
            full_name="Morgan Ellis",
            phone="+15550001006",
            role="admin",
            password=OWNER_PASSWORD,
        )

        internal_org = Organization(name="Twinevia Internal", slug="twinevia-internal", status="active")
        northstar_org = Organization(name="Northstar Fitness", slug="northstar-fitness", status="active")
        harbor_org = Organization(name="Harbor Events Co", slug="harbor-events-co", status="active")
        sunset_org = Organization(name="Sunset Realty Group", slug="sunset-realty-group", status="suspended")

        internal_subscription = OrganizationSubscription(
            organization=internal_org,
            stripe_customer_id="cus_demo_internal",
            stripe_subscription_id="sub_demo_internal",
            stripe_price_id=stripe_price_id,
            status="trialing",
            current_period_end=now + timedelta(days=12),
        )
        northstar_subscription = OrganizationSubscription(
            organization=northstar_org,
            stripe_customer_id="cus_demo_northstar",
            stripe_subscription_id="sub_demo_northstar",
            stripe_price_id=stripe_price_id,
            status="active",
            current_period_end=now + timedelta(days=26),
        )
        harbor_subscription = OrganizationSubscription(
            organization=harbor_org,
            stripe_price_id=stripe_price_id,
            status="incomplete",
        )
        sunset_subscription = OrganizationSubscription(
            organization=sunset_org,
            stripe_customer_id="cus_demo_sunset",
            stripe_subscription_id="sub_demo_sunset",
            stripe_price_id=stripe_price_id,
            status="past_due",
            current_period_end=now - timedelta(days=3),
        )

        internal_messaging_status = "active" if live_from_number else "pending"
        internal_messaging = OrganizationMessagingProfile(
            organization=internal_org,
            from_number=live_from_number,
            inbound_identity=live_from_number,
            messaging_service_sid=live_messaging_service_sid,
            status=internal_messaging_status,
            provider_status=internal_messaging_status,
            sender_review_status="approved" if live_from_number else "pending",
            consent_acknowledged_at=now if live_from_number else None,
        )
        northstar_messaging = OrganizationMessagingProfile(
            organization=northstar_org,
            status="pending",
            provider_status="pending",
        )
        harbor_messaging = OrganizationMessagingProfile(
            organization=harbor_org,
            status="pending",
            provider_status="pending",
        )
        sunset_messaging = OrganizationMessagingProfile(
            organization=sunset_org,
            status="pending",
            provider_status="pending",
        )

        db.session.add_all(
            [
                platform_admin,
                internal_owner,
                internal_staff,
                northstar_owner,
                northstar_staff,
                sunset_owner,
                internal_org,
                northstar_org,
                harbor_org,
                sunset_org,
                internal_subscription,
                northstar_subscription,
                harbor_subscription,
                sunset_subscription,
                internal_messaging,
                northstar_messaging,
                harbor_messaging,
                sunset_messaging,
            ]
        )
        db.session.flush()

        db.session.add_all(
            [
                OrganizationMembership(organization_id=internal_org.id, user_id=internal_owner.id, role="owner"),
                OrganizationMembership(organization_id=internal_org.id, user_id=internal_staff.id, role="staff"),
                OrganizationMembership(organization_id=northstar_org.id, user_id=northstar_owner.id, role="owner"),
                OrganizationMembership(organization_id=northstar_org.id, user_id=northstar_staff.id, role="staff"),
                OrganizationMembership(organization_id=sunset_org.id, user_id=sunset_owner.id, role="owner"),
            ]
        )

        harbor_owner_invite = OrganizationInvitation(
            organization_id=harbor_org.id,
            email="owner@harbor.demo.test",
            role="owner",
            status="pending",
            token="harbor-owner-demo-token",
            invited_by_user_id=platform_admin.id,
            expires_at=now + timedelta(days=7),
        )
        internal_staff_invite = OrganizationInvitation(
            organization_id=internal_org.id,
            email="next.staff@twineviainternal.demo.test",
            role="staff",
            status="pending",
            token="twinevia-staff-demo-token",
            invited_by_user_id=internal_owner.id,
            expires_at=now + timedelta(days=7),
        )
        northstar_staff_invite = OrganizationInvitation(
            organization_id=northstar_org.id,
            email="ops@northstar.demo.test",
            role="staff",
            status="pending",
            token="northstar-staff-demo-token",
            invited_by_user_id=northstar_owner.id,
            expires_at=now + timedelta(days=7),
        )
        db.session.add_all([harbor_owner_invite, internal_staff_invite, northstar_staff_invite])

        internal_contacts = [
            CommunityMember(organization_id=internal_org.id, name="Maya Chen", phone="+17205550101"),
            CommunityMember(organization_id=internal_org.id, name="Alex Rivera", phone="+17205550102"),
            CommunityMember(organization_id=internal_org.id, name="Jordan Patel", phone="+17205550103"),
            CommunityMember(organization_id=internal_org.id, name="Sierra Brooks", phone="+17205550104"),
            CommunityMember(organization_id=internal_org.id, name="Devin Flores", phone="+17205550105"),
            CommunityMember(organization_id=internal_org.id, name="Parker Kim", phone="+17205550106"),
        ]
        northstar_contacts = [
            CommunityMember(organization_id=northstar_org.id, name="Taylor Reed", phone="+17205550101"),
            CommunityMember(organization_id=northstar_org.id, name="Chris Lopez", phone="+17205550202"),
            CommunityMember(organization_id=northstar_org.id, name="Morgan Price", phone="+17205550203"),
            CommunityMember(organization_id=northstar_org.id, name="Skyler Hughes", phone="+17205550204"),
        ]
        sunset_contacts = [
            CommunityMember(organization_id=sunset_org.id, name="Jordan Hale", phone="+17205550301"),
            CommunityMember(organization_id=sunset_org.id, name="Ari Bennett", phone="+17205550302"),
        ]
        db.session.add_all(internal_contacts + northstar_contacts + sunset_contacts)

        db.session.add_all(
            [
                UnsubscribedContact(
                    organization_id=internal_org.id,
                    name="Parker Kim",
                    phone="+17205550106",
                    reason="Requested STOP during March update",
                    source="inbox",
                ),
                SuppressedContact(
                    organization_id=northstar_org.id,
                    phone="+17205550209",
                    reason="Carrier marked message as undeliverable",
                    category="deliverability",
                    source="twilio-status-callback",
                ),
            ]
        )

        webinar_event = Event(
            organization_id=internal_org.id,
            title="Customer Advisory Webinar",
            date=today + timedelta(days=9),
        )
        launch_demo_event = Event(
            organization_id=internal_org.id,
            title="Spring Launch Demo",
            date=today + timedelta(days=17),
        )
        bootcamp_event = Event(
            organization_id=northstar_org.id,
            title="Open House Bootcamp",
            date=today + timedelta(days=6),
        )
        db.session.add_all([webinar_event, launch_demo_event, bootcamp_event])
        db.session.flush()

        db.session.add_all(
            [
                EventRegistration(
                    organization_id=internal_org.id,
                    event_id=webinar_event.id,
                    name="Maya Chen",
                    phone="+17205550101",
                ),
                EventRegistration(
                    organization_id=internal_org.id,
                    event_id=webinar_event.id,
                    name="Alex Rivera",
                    phone="+17205550102",
                ),
                EventRegistration(
                    organization_id=internal_org.id,
                    event_id=launch_demo_event.id,
                    name="Sierra Brooks",
                    phone="+17205550104",
                ),
                EventRegistration(
                    organization_id=northstar_org.id,
                    event_id=bootcamp_event.id,
                    name="Chris Lopez",
                    phone="+17205550202",
                ),
                EventRegistration(
                    organization_id=northstar_org.id,
                    event_id=bootcamp_event.id,
                    name="Morgan Price",
                    phone="+17205550203",
                ),
            ]
        )

        internal_log = MessageLog(
            organization_id=internal_org.id,
            message_body="Reminder: the customer advisory webinar starts tomorrow at 11:00 AM MT.",
            target="community",
            status="sent",
            total_recipients=6,
            success_count=5,
            failure_count=1,
            details=_message_log_details(
                delivered=[
                    "+17205550101",
                    "+17205550102",
                    "+17205550103",
                    "+17205550104",
                    "+17205550105",
                ],
                failed=[{"phone": "+17205550106", "error": "Contact unsubscribed"}],
            ),
        )
        launch_log = MessageLog(
            organization_id=internal_org.id,
            event_id=launch_demo_event.id,
            message_body="You're confirmed for the Spring Launch Demo. Reply HELP for agenda details.",
            target="event",
            status="sent",
            total_recipients=1,
            success_count=1,
            failure_count=0,
            details=_message_log_details(delivered=["+17205550104"]),
        )
        northstar_log = MessageLog(
            organization_id=northstar_org.id,
            message_body="Open House Bootcamp starts Saturday at 9:00 AM. Bring water and check in at the front desk.",
            target="community",
            status="sent",
            total_recipients=4,
            success_count=4,
            failure_count=0,
            details=_message_log_details(
                delivered=[
                    "+17205550101",
                    "+17205550202",
                    "+17205550203",
                    "+17205550204",
                ]
            ),
        )
        db.session.add_all([internal_log, launch_log, northstar_log])
        db.session.flush()

        internal_thread = InboxThread(
            organization_id=internal_org.id,
            phone="+17205550101",
            contact_name="Maya Chen",
            unread_count=1,
            last_message_at=now - timedelta(hours=2),
            last_message_preview="Can I bring a guest to the webinar?",
            last_direction="inbound",
        )
        internal_stop_thread = InboxThread(
            organization_id=internal_org.id,
            phone="+17205550106",
            contact_name="Parker Kim",
            unread_count=0,
            last_message_at=now - timedelta(days=1),
            last_message_preview="You have been unsubscribed.",
            last_direction="outbound",
        )
        northstar_thread = InboxThread(
            organization_id=northstar_org.id,
            phone="+17205550202",
            contact_name="Chris Lopez",
            unread_count=0,
            last_message_at=now - timedelta(hours=6),
            last_message_preview="Thanks, I'll be there.",
            last_direction="inbound",
        )
        db.session.add_all([internal_thread, internal_stop_thread, northstar_thread])
        db.session.flush()

        db.session.add_all(
            [
                InboxMessage(
                    organization_id=internal_org.id,
                    thread_id=internal_thread.id,
                    phone=internal_thread.phone,
                    direction="outbound",
                    body="Thanks for registering for the customer advisory webinar. Reply with any questions.",
                    message_sid="SMDEMO0001",
                    created_at=now - timedelta(days=1, hours=4),
                ),
                InboxMessage(
                    organization_id=internal_org.id,
                    thread_id=internal_thread.id,
                    phone=internal_thread.phone,
                    direction="inbound",
                    body="Can I bring a guest to the webinar?",
                    message_sid="SMDEMO0002",
                    created_at=now - timedelta(hours=2),
                ),
                InboxMessage(
                    organization_id=internal_org.id,
                    thread_id=internal_stop_thread.id,
                    phone=internal_stop_thread.phone,
                    direction="inbound",
                    body="STOP",
                    message_sid="SMDEMO0003",
                    created_at=now - timedelta(days=1, hours=3),
                ),
                InboxMessage(
                    organization_id=internal_org.id,
                    thread_id=internal_stop_thread.id,
                    phone=internal_stop_thread.phone,
                    direction="outbound",
                    body="You have been unsubscribed and will no longer receive updates.",
                    message_sid="SMDEMO0004",
                    delivery_status="sent",
                    created_at=now - timedelta(days=1, hours=3) + timedelta(minutes=1),
                ),
                InboxMessage(
                    organization_id=northstar_org.id,
                    thread_id=northstar_thread.id,
                    phone=northstar_thread.phone,
                    direction="outbound",
                    body="Open House Bootcamp starts Saturday at 9:00 AM. Reply YES to confirm.",
                    message_sid="SMDEMO0005",
                    created_at=now - timedelta(hours=8),
                ),
                InboxMessage(
                    organization_id=northstar_org.id,
                    thread_id=northstar_thread.id,
                    phone=northstar_thread.phone,
                    direction="inbound",
                    body="Thanks, I'll be there.",
                    message_sid="SMDEMO0006",
                    created_at=now - timedelta(hours=6),
                ),
            ]
        )

        help_rule = KeywordAutomationRule(
            organization_id=internal_org.id,
            keyword="HELP",
            response_body="Twinevia Internal support: reply to this message and our team will follow up.",
            is_active=True,
            match_count=3,
            last_matched_at=now - timedelta(hours=5),
        )
        class_rule = KeywordAutomationRule(
            organization_id=northstar_org.id,
            keyword="CLASS",
            response_body="Northstar Fitness classes update every Monday at 8:00 AM.",
            is_active=True,
            match_count=5,
            last_matched_at=now - timedelta(days=2),
        )
        db.session.add_all([help_rule, class_rule])

        rsvp_survey = SurveyFlow(
            organization_id=internal_org.id,
            name="Webinar RSVP Follow-up",
            trigger_keyword="RSVP",
            intro_message="Thanks for your interest. A few quick questions will help us plan the session.",
            completion_message="Perfect, you're on the list. We'll send a reminder the day before.",
            linked_event_id=webinar_event.id,
            is_active=True,
            start_count=4,
            completion_count=3,
        )
        rsvp_survey.set_questions(
            [
                "Will you attend live or watch the replay?",
                "What topic should we make sure to cover?",
            ]
        )
        db.session.add(rsvp_survey)
        db.session.flush()

        survey_session = SurveySession(
            organization_id=internal_org.id,
            survey_id=rsvp_survey.id,
            thread_id=internal_thread.id,
            phone=internal_thread.phone,
            status="completed",
            current_question_index=2,
            started_at=now - timedelta(days=3),
            last_activity_at=now - timedelta(days=3, minutes=-5),
            completed_at=now - timedelta(days=3, minutes=-5),
        )
        db.session.add(survey_session)
        db.session.flush()
        db.session.add_all(
            [
                SurveyResponse(
                    organization_id=internal_org.id,
                    session_id=survey_session.id,
                    survey_id=rsvp_survey.id,
                    phone=survey_session.phone,
                    question_index=0,
                    question_prompt="Will you attend live or watch the replay?",
                    answer="Attend live",
                    created_at=now - timedelta(days=3, minutes=10),
                ),
                SurveyResponse(
                    organization_id=internal_org.id,
                    session_id=survey_session.id,
                    survey_id=rsvp_survey.id,
                    phone=survey_session.phone,
                    question_index=1,
                    question_prompt="What topic should we make sure to cover?",
                    answer="How automations should be staged before rollout",
                    created_at=now - timedelta(days=3, minutes=5),
                ),
            ]
        )

        db.session.add_all(
            [
                ScheduledMessage(
                    organization_id=internal_org.id,
                    scheduled_at=now + timedelta(hours=3),
                    message_body="Tomorrow's webinar reminder goes out at 9:00 AM. Reply HELP if you need anything before then.",
                    target="community",
                    status="pending",
                    test_mode=False,
                ),
                ScheduledMessage(
                    organization_id=northstar_org.id,
                    scheduled_at=now + timedelta(hours=5),
                    message_body="Bootcamp reminder: wear comfortable shoes and bring a refillable water bottle.",
                    target="event",
                    event_id=bootcamp_event.id,
                    status="pending",
                    test_mode=False,
                ),
                ScheduledMessage(
                    organization_id=sunset_org.id,
                    scheduled_at=now - timedelta(days=2),
                    message_body="This message is blocked because the subscription is past due.",
                    target="community",
                    status="failed",
                    test_mode=False,
                    attempt_count=1,
                    last_attempt_at=now - timedelta(days=2),
                    error_message="Billing access is disabled for this organization.",
                ),
            ]
        )

        platform_login = AuthEvent(
            event_type="login_success",
            outcome="success",
            user_id=platform_admin.id,
            username=platform_admin.username,
            client_ip="127.0.0.1",
            created_at=now - timedelta(hours=1),
        )
        platform_login.set_metadata({"surface": "platform", "method": "password"})

        owner_login = AuthEvent(
            event_type="login_success",
            outcome="success",
            organization_id=internal_org.id,
            user_id=internal_owner.id,
            username=internal_owner.username,
            client_ip="127.0.0.1",
            created_at=now - timedelta(hours=2),
        )
        owner_login.set_metadata({"surface": "workspace", "method": "password"})

        billing_block = AuthEvent(
            event_type="billing_access_denied",
            outcome="blocked",
            organization_id=sunset_org.id,
            user_id=sunset_owner.id,
            username=sunset_owner.username,
            client_ip="127.0.0.1",
            created_at=now - timedelta(days=1, hours=2),
        )
        billing_block.set_metadata({"reason": "subscription_past_due"})

        db.session.add_all([platform_login, owner_login, billing_block])
        db.session.commit()

        base_url_value = (app.config.get("SAAS_BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
        live_sender_note = (
            f"Approved sender assigned to Twinevia Internal: {live_from_number} "
            f"{f'via managed Messaging Service {live_messaging_service_sid}' if live_messaging_service_sid else '(no Messaging Service SID provided)'}."
            if live_from_number
            else "No live Twilio sender assigned. All organizations are safe to test locally in non-live mode."
        )
        return {
            "database_url": configured_database_url,
            "accounts": [
                {
                    "label": "Platform admin",
                    "email": platform_admin.email,
                    "password": PLATFORM_ADMIN_PASSWORD,
                    "home": "/platform",
                },
                {
                    "label": "Twinevia Internal owner",
                    "email": internal_owner.email,
                    "password": OWNER_PASSWORD,
                    "home": "/dashboard",
                },
                {
                    "label": "Twinevia Internal staff",
                    "email": internal_staff.email,
                    "password": STAFF_PASSWORD,
                    "home": "/dashboard",
                },
                {
                    "label": "Northstar Fitness owner",
                    "email": northstar_owner.email,
                    "password": OWNER_PASSWORD,
                    "home": "/dashboard",
                },
                {
                    "label": "Northstar Fitness staff",
                    "email": northstar_staff.email,
                    "password": STAFF_PASSWORD,
                    "home": "/dashboard",
                },
                {
                    "label": "Sunset Realty owner",
                    "email": sunset_owner.email,
                    "password": OWNER_PASSWORD,
                    "home": "/dashboard",
                },
            ],
            "organizations": [
                {
                    "name": internal_org.name,
                    "status": internal_org.status,
                    "billing_status": internal_subscription.status,
                    "messaging_status": internal_messaging.status,
                },
                {
                    "name": northstar_org.name,
                    "status": northstar_org.status,
                    "billing_status": northstar_subscription.status,
                    "messaging_status": northstar_messaging.status,
                },
                {
                    "name": harbor_org.name,
                    "status": harbor_org.status,
                    "billing_status": harbor_subscription.status,
                    "messaging_status": harbor_messaging.status,
                },
                {
                    "name": sunset_org.name,
                    "status": sunset_org.status,
                    "billing_status": sunset_subscription.status,
                    "messaging_status": sunset_messaging.status,
                },
            ],
            "pending_invites": [
                {
                    "organization": internal_org.name,
                    "role": "staff",
                    "email": internal_staff_invite.email,
                    "accept_url": f"{base_url_value}/invites/{internal_staff_invite.token}",
                },
                {
                    "organization": northstar_org.name,
                    "role": "staff",
                    "email": northstar_staff_invite.email,
                    "accept_url": f"{base_url_value}/invites/{northstar_staff_invite.token}",
                },
                {
                    "organization": harbor_org.name,
                    "role": "owner",
                    "email": harbor_owner_invite.email,
                    "accept_url": f"{base_url_value}/invites/{harbor_owner_invite.token}",
                },
            ],
            "live_sender_note": live_sender_note,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a realistic local multi-tenant SaaS demo dataset.")
    parser.add_argument("--reset", action="store_true", help="Delete the target SQLite database before seeding.")
    parser.add_argument("--database-url", help="Override DATABASE_URL for the seed run.")
    parser.add_argument("--base-url", help="Override SAAS_BASE_URL for invite links in the printed summary.")
    parser.add_argument("--live-from-number", help="Assign an approved live sender number to Twinevia Internal.")
    parser.add_argument(
        "--live-messaging-service-sid",
        help="Optional MG... Messaging Service SID paired with --live-from-number.",
    )
    args = parser.parse_args()

    summary = seed_demo_database(
        reset=args.reset,
        database_url=args.database_url,
        base_url=args.base_url,
        live_from_number=args.live_from_number,
        live_messaging_service_sid=args.live_messaging_service_sid,
    )
    _print_summary(summary)


if __name__ == "__main__":
    main()

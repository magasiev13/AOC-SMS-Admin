import importlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

from sqlalchemy import create_engine, text


class TestLegacyImportService(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.target_db_path = os.path.join(self._temp_dir.name, "target.db")
        self.legacy_db_path = os.path.join(self._temp_dir.name, "legacy.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{self.target_db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
                "STRIPE_PRICE_ID": "price_test_123",
            }
        )

        import app.config

        importlib.reload(app.config)
        self.engine = create_engine(os.environ["DATABASE_URL"])

        from app.saas_migrations.runner import run_pending_saas_migrations

        run_pending_saas_migrations(self.engine, MagicMock())
        self._seed_legacy_sqlite()

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def _seed_legacy_sqlite(self) -> None:
        with sqlite3.connect(self.legacy_db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    phone TEXT,
                    role TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    session_nonce TEXT,
                    created_at TEXT
                );
                CREATE TABLE community_members (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    phone TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    date TEXT,
                    created_at TEXT
                );
                CREATE TABLE event_registrations (
                    id INTEGER PRIMARY KEY,
                    event_id INTEGER NOT NULL,
                    name TEXT,
                    phone TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE unsubscribed_contacts (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    phone TEXT NOT NULL,
                    reason TEXT,
                    source TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE suppressed_contacts (
                    id INTEGER PRIMARY KEY,
                    phone TEXT NOT NULL,
                    reason TEXT,
                    category TEXT NOT NULL,
                    source TEXT,
                    source_type TEXT,
                    source_message_log_id INTEGER,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE message_logs (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT,
                    message_body TEXT NOT NULL,
                    target TEXT NOT NULL,
                    event_id INTEGER,
                    status TEXT,
                    total_recipients INTEGER,
                    success_count INTEGER,
                    failure_count INTEGER,
                    details TEXT
                );
                CREATE TABLE scheduled_messages (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT,
                    scheduled_at TEXT NOT NULL,
                    message_body TEXT NOT NULL,
                    target TEXT NOT NULL,
                    event_id INTEGER,
                    status TEXT,
                    test_mode INTEGER,
                    attempt_count INTEGER,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    processing_started_at TEXT,
                    sent_at TEXT,
                    error_message TEXT,
                    message_log_id INTEGER
                );
                CREATE TABLE inbox_threads (
                    id INTEGER PRIMARY KEY,
                    phone TEXT NOT NULL,
                    contact_name TEXT,
                    unread_count INTEGER,
                    last_message_at TEXT,
                    last_message_preview TEXT,
                    last_direction TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE inbox_messages (
                    id INTEGER PRIMARY KEY,
                    thread_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    body TEXT NOT NULL,
                    message_sid TEXT,
                    automation_source TEXT,
                    automation_source_id INTEGER,
                    matched_keyword TEXT,
                    delivery_status TEXT,
                    delivery_error TEXT,
                    raw_payload TEXT,
                    created_at TEXT
                );
                CREATE TABLE keyword_automation_rules (
                    id INTEGER PRIMARY KEY,
                    keyword TEXT NOT NULL,
                    response_body TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    match_count INTEGER NOT NULL DEFAULT 0,
                    last_matched_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE survey_flows (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    trigger_keyword TEXT NOT NULL,
                    intro_message TEXT,
                    questions_json TEXT NOT NULL,
                    completion_message TEXT,
                    linked_event_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    start_count INTEGER NOT NULL DEFAULT 0,
                    completion_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE survey_sessions (
                    id INTEGER PRIMARY KEY,
                    survey_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_question_index INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    last_activity_at TEXT,
                    completed_at TEXT
                );
                CREATE TABLE survey_responses (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL,
                    survey_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    question_index INTEGER NOT NULL,
                    question_prompt TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE auth_events (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    outcome TEXT,
                    user_id INTEGER,
                    username TEXT,
                    client_ip TEXT,
                    metadata_json TEXT,
                    created_at TEXT
                );
                """
            )

            connection.execute(
                """
                INSERT INTO users (id, username, password_hash, phone, role, must_change_password, session_nonce, created_at)
                VALUES
                    (1, 'magasiev13', 'pbkdf2:sha256:1$abc$hash', '+15550000001', 'admin', 0, 'nonce-owner', '2025-01-01 00:00:00'),
                    (2, 'kfolyan', 'pbkdf2:sha256:1$def$hash', '+15550000002', 'social_manager', 1, 'nonce-staff-1', '2025-01-02 00:00:00'),
                    (3, 'cyounit', 'pbkdf2:sha256:1$ghi$hash', '+15550000003', 'social_manager', 0, 'nonce-staff-2', '2025-01-03 00:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO community_members (id, name, phone, created_at)
                VALUES (1, 'Client One', '+15551110001', '2025-02-01 10:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO events (id, title, date, created_at)
                VALUES (1, 'Open House', '2025-03-01', '2025-02-10 12:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO event_registrations (id, event_id, name, phone, created_at)
                VALUES (1, 1, 'Event Guest', '+15552220001', '2025-02-11 12:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO unsubscribed_contacts (id, name, phone, reason, source, created_at)
                VALUES (1, 'Opted Out', '+15553330001', 'STOP', 'inbound', '2025-02-12 12:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO message_logs (id, created_at, message_body, target, event_id, status, total_recipients, success_count, failure_count, details)
                VALUES (1, '2025-02-15 14:00:00', 'Legacy message', 'event', 1, 'sent', 1, 1, 0, '[]')
                """
            )
            connection.execute(
                """
                INSERT INTO suppressed_contacts (id, phone, reason, category, source, source_type, source_message_log_id, created_at, updated_at)
                VALUES (1, '+15554449999', 'carrier violation', 'hard_fail', 'twilio', 'delivery', 1, '2025-02-15 15:00:00', '2025-02-15 15:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO scheduled_messages (id, created_at, scheduled_at, message_body, target, event_id, status, test_mode, attempt_count, last_attempt_at, next_retry_at, processing_started_at, sent_at, error_message, message_log_id)
                VALUES (1, '2025-02-16 09:00:00', '2025-02-17 09:00:00', 'Scheduled legacy message', 'event', 1, 'sent', 0, 1, '2025-02-17 09:00:00', NULL, '2025-02-17 09:00:00', '2025-02-17 09:01:00', NULL, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO inbox_threads (id, phone, contact_name, unread_count, last_message_at, last_message_preview, last_direction, created_at, updated_at)
                VALUES (1, '+15554440001', 'Inbox Contact', 1, '2025-02-18 08:00:00', 'Hello', 'inbound', '2025-02-18 08:00:00', '2025-02-18 08:05:00')
                """
            )
            connection.execute(
                """
                INSERT INTO inbox_messages (id, thread_id, phone, direction, body, message_sid, automation_source, automation_source_id, matched_keyword, delivery_status, delivery_error, raw_payload, created_at)
                VALUES (1, 1, '+15554440001', 'inbound', 'Hello', 'SMlegacy0001', 'keyword', 1, 'JOIN', 'received', NULL, '{"Body":"Hello"}', '2025-02-18 08:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO keyword_automation_rules (id, keyword, response_body, is_active, match_count, last_matched_at, created_at, updated_at)
                VALUES (1, 'JOIN', 'Thanks for joining', 1, 3, '2025-02-18 08:00:00', '2025-02-14 07:00:00', '2025-02-18 08:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO survey_flows (id, name, trigger_keyword, intro_message, questions_json, completion_message, linked_event_id, is_active, start_count, completion_count, created_at, updated_at)
                VALUES (1, 'AOC Survey', 'SURVEY', 'Welcome', '["Question 1?","Question 2?"]', 'Done', 1, 1, 1, 0, '2025-02-14 09:00:00', '2025-02-18 08:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO survey_sessions (id, survey_id, thread_id, phone, status, current_question_index, started_at, last_activity_at, completed_at)
                VALUES (1, 1, 1, '+15554440001', 'active', 1, '2025-02-18 08:01:00', '2025-02-18 08:04:00', NULL)
                """
            )
            connection.execute(
                """
                INSERT INTO survey_responses (id, session_id, survey_id, phone, question_index, question_prompt, answer, created_at)
                VALUES (1, 1, 1, '+15554440001', 0, 'Question 1?', 'Yes', '2025-02-18 08:03:00')
                """
            )
            connection.execute(
                """
                INSERT INTO auth_events (id, event_type, outcome, user_id, username, client_ip, metadata_json, created_at)
                VALUES (1, 'login_success', 'success', 1, 'magasiev13', '127.0.0.1', '{"source":"legacy"}', '2025-02-19 10:00:00')
                """
            )
            connection.commit()

    def test_import_legacy_sqlite_snapshot_creates_default_organization(self) -> None:
        from app import create_app, db
        from app.models import (
            AppUser,
            AuthEvent,
            CommunityMember,
            Event,
            EventRegistration,
            InboxMessage,
            KeywordAutomationRule,
            MessageLog,
            Organization,
            OrganizationMembership,
            ScheduledMessage,
            SurveyFlow,
            SurveyResponse,
            SurveySession,
            UnsubscribedContact,
        )
        from app.services.legacy_import_service import import_legacy_sqlite_snapshot

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        with app.app_context():
            summary = import_legacy_sqlite_snapshot(
                legacy_db_path=self.legacy_db_path,
                organization_name="Legacy Production",
                organization_slug="legacy-production",
            )

            organization = Organization.query.one()
            self.assertEqual(organization.slug, "legacy-production")
            self.assertEqual(summary["organization_id"], organization.id)
            self.assertEqual(summary["counts"]["users"], 3)
            self.assertEqual(summary["counts"]["message_logs"], 1)
            self.assertEqual(AppUser.query.filter_by(is_platform_admin=False).count(), 3)
            self.assertEqual(OrganizationMembership.query.filter_by(role="owner").count(), 1)
            self.assertEqual(OrganizationMembership.query.filter_by(role="staff").count(), 2)
            self.assertEqual(CommunityMember.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(Event.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(EventRegistration.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(UnsubscribedContact.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(MessageLog.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(ScheduledMessage.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(InboxMessage.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(KeywordAutomationRule.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(SurveyFlow.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(SurveySession.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(SurveyResponse.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(AuthEvent.query.filter_by(organization_id=organization.id).count(), 1)

            scheduled_message = ScheduledMessage.query.one()
            message_log = MessageLog.query.one()
            event = Event.query.one()
            owner = AppUser.query.filter_by(username="magasiev13").one()
            auth_event = AuthEvent.query.one()
            self.assertEqual(scheduled_message.message_log_id, message_log.id)
            self.assertEqual(scheduled_message.event_id, event.id)
            self.assertEqual(owner.email, None)
            self.assertEqual(auth_event.username, "magasiev13")

            audit = db.session.execute(
                text("SELECT status, organization_id FROM saas_import_runs ORDER BY id DESC LIMIT 1")
            ).first()
            self.assertIsNotNone(audit)
            self.assertEqual(audit[0], "completed")
            self.assertEqual(audit[1], organization.id)

    def test_import_legacy_sqlite_snapshot_into_new_org_supports_username_remap_and_existing_saas_data(self) -> None:
        from app import create_app, db
        from app.models import (
            AppUser,
            AuthEvent,
            InboxMessage,
            Organization,
            OrganizationMembership,
            OrganizationMessagingProfile,
            OrganizationSubscription,
        )
        from app.services.legacy_import_service import import_legacy_sqlite_snapshot_into_new_org

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        with app.app_context():
            platform_admin = AppUser(
                username="magasiev13",
                email="platform@example.test",
                role="admin",
                is_platform_admin=True,
                password_hash="pbkdf2:sha256:1$plat$hash",
            )
            existing_owner = AppUser(
                username="magasiev",
                email="owner@itwingman.test",
                role="admin",
                is_platform_admin=False,
                password_hash="pbkdf2:sha256:1$owner$hash",
            )
            existing_org = Organization(name="IT Wingman LLC", slug="it-wingman", status="active")
            db.session.add_all([platform_admin, existing_owner, existing_org])
            db.session.flush()
            db.session.add(
                OrganizationMembership(
                    organization_id=existing_org.id,
                    user_id=existing_owner.id,
                    role="owner",
                )
            )
            db.session.add(
                OrganizationSubscription(
                    organization_id=existing_org.id,
                    stripe_price_id="price_test_123",
                    status="active",
                )
            )
            db.session.add(
                OrganizationMessagingProfile(
                    organization_id=existing_org.id,
                    provider_mode="platform_managed",
                    status="active",
                    provider_status="active",
                )
            )
            db.session.commit()

            summary = import_legacy_sqlite_snapshot_into_new_org(
                legacy_db_path=self.legacy_db_path,
                organization_name="AOC",
                organization_slug="aoc",
                subscription_status="complimentary",
                provider_mode="customer_managed",
                username_remaps={"magasiev13": "magasiev-aoc"},
            )

            imported_org = Organization.query.filter_by(slug="aoc").one()
            imported_users = AppUser.query.join(OrganizationMembership).filter(
                OrganizationMembership.organization_id == imported_org.id
            ).order_by(AppUser.username.asc()).all()
            self.assertEqual(summary["organization_id"], imported_org.id)
            self.assertEqual(summary["counts"]["users"], 3)
            self.assertEqual(imported_org.subscription.status, "complimentary")
            self.assertEqual(imported_org.messaging_profile.provider_mode, "customer_managed")
            self.assertEqual([user.username for user in imported_users], ["cyounit", "kfolyan", "magasiev-aoc"])
            self.assertEqual(AppUser.query.filter_by(username="magasiev13").one().is_platform_admin, True)
            self.assertEqual(AppUser.query.filter_by(username="magasiev-aoc").one().password_hash, "pbkdf2:sha256:1$abc$hash")
            self.assertEqual(AuthEvent.query.filter_by(organization_id=imported_org.id).one().username, "magasiev-aoc")
            self.assertEqual(InboxMessage.query.filter_by(organization_id=imported_org.id).one().message_sid, "SMlegacy0001")

    def test_import_legacy_sqlite_snapshot_into_new_org_rejects_username_conflict_without_remap(self) -> None:
        from app import create_app, db
        from app.models import AppUser
        from app.services.legacy_import_service import import_legacy_sqlite_snapshot_into_new_org

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        with app.app_context():
            db.session.add(
                AppUser(
                    username="magasiev13",
                    email="platform@example.test",
                    role="admin",
                    is_platform_admin=True,
                    password_hash="pbkdf2:sha256:1$plat$hash",
                )
            )
            db.session.commit()

            with self.assertRaises(RuntimeError) as ctx:
                import_legacy_sqlite_snapshot_into_new_org(
                    legacy_db_path=self.legacy_db_path,
                    organization_name="AOC",
                    organization_slug="aoc",
                    provider_mode="customer_managed",
                )

            self.assertIn("Username conflict importing legacy user 'magasiev13'", str(ctx.exception))

    def test_import_legacy_sqlite_snapshot_into_new_org_rejects_message_sid_conflict(self) -> None:
        from app import create_app, db
        from app.models import InboxMessage, InboxThread, Organization
        from app.services.legacy_import_service import import_legacy_sqlite_snapshot_into_new_org

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        with app.app_context():
            existing_org = Organization(name="Existing", slug="existing", status="active")
            existing_thread = InboxThread(
                organization=existing_org,
                phone="+15554440001",
                contact_name="Existing Contact",
            )
            db.session.add_all([existing_org, existing_thread])
            db.session.flush()
            db.session.add(
                InboxMessage(
                    organization_id=existing_org.id,
                    thread_id=existing_thread.id,
                    phone="+15554440001",
                    direction="inbound",
                    body="Existing",
                    message_sid="SMlegacy0001",
                )
            )
            db.session.commit()

            with self.assertRaises(RuntimeError) as ctx:
                import_legacy_sqlite_snapshot_into_new_org(
                    legacy_db_path=self.legacy_db_path,
                    organization_name="AOC",
                    organization_slug="aoc",
                )

            self.assertIn("Message SID conflict importing legacy inbox message 'SMlegacy0001'", str(ctx.exception))

    def test_sync_legacy_sqlite_snapshot_into_existing_org_supports_dry_run_and_apply(self) -> None:
        from app import create_app, db
        from app.models import AppUser, CommunityMember, InboxMessage, InboxThread, MessageLog, Organization
        from app.services.legacy_import_service import (
            import_legacy_sqlite_snapshot_into_new_org,
            sync_legacy_sqlite_snapshot_into_existing_org,
        )

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        with app.app_context():
            import_legacy_sqlite_snapshot_into_new_org(
                legacy_db_path=self.legacy_db_path,
                organization_name="AOC",
                organization_slug="aoc",
                subscription_status="complimentary",
                provider_mode="customer_managed",
                username_remaps={"magasiev13": "magasiev-aoc"},
            )

            with sqlite3.connect(self.legacy_db_path) as connection:
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = 'pbkdf2:sha256:1$sync$hash', must_change_password = 1
                    WHERE username = 'kfolyan'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO community_members (id, name, phone, created_at)
                    VALUES (2, 'Client Two', '+15551110002', '2025-02-20 09:00:00')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO unsubscribed_contacts (id, name, phone, reason, source, created_at)
                    VALUES (2, 'Opted Out Again', '+15553330002', 'STOP', 'inbound', '2025-02-20 10:00:00')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO message_logs (id, created_at, message_body, target, event_id, status, total_recipients, success_count, failure_count, details)
                    VALUES (2, '2025-02-20 11:00:00', 'Second legacy message', 'community', NULL, 'sent', 1, 1, 0, '[]')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO inbox_threads (id, phone, contact_name, unread_count, last_message_at, last_message_preview, last_direction, created_at, updated_at)
                    VALUES (2, '+15554440002', 'Second Inbox Contact', 0, '2025-02-20 12:00:00', 'Follow up', 'outbound', '2025-02-20 12:00:00', '2025-02-20 12:01:00')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO inbox_messages (id, thread_id, phone, direction, body, message_sid, automation_source, automation_source_id, matched_keyword, delivery_status, delivery_error, raw_payload, created_at)
                    VALUES (2, 2, '+15554440002', 'outbound', 'Follow up', 'SMlegacy0002', NULL, NULL, NULL, 'sent', NULL, '{"Body":"Follow up"}', '2025-02-20 12:00:00')
                    """
                )
                connection.commit()

            organization = Organization.query.filter_by(slug="aoc").one()
            imported_user = AppUser.query.filter_by(username="kfolyan").one()
            original_password_hash = imported_user.password_hash

            dry_run_summary = sync_legacy_sqlite_snapshot_into_existing_org(
                legacy_db_path=self.legacy_db_path,
                organization_slug="aoc",
                username_remaps={"magasiev13": "magasiev-aoc"},
                dry_run=True,
            )

            self.assertTrue(dry_run_summary["dry_run"])
            self.assertEqual(dry_run_summary["target_counts_before"]["community_members"], 1)
            self.assertEqual(dry_run_summary["target_counts_after"]["community_members"], 2)
            self.assertEqual(dry_run_summary["target_counts_after"]["unsubscribed_contacts"], 2)
            self.assertEqual(dry_run_summary["target_counts_after"]["message_logs"], 2)
            self.assertEqual(dry_run_summary["target_counts_after"]["inbox_threads"], 2)
            self.assertEqual(dry_run_summary["target_counts_after"]["inbox_messages"], 2)
            self.assertEqual(CommunityMember.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(MessageLog.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(InboxThread.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(InboxMessage.query.filter_by(organization_id=organization.id).count(), 1)
            db.session.expire_all()
            self.assertEqual(AppUser.query.filter_by(username="kfolyan").one().password_hash, original_password_hash)

            apply_summary = sync_legacy_sqlite_snapshot_into_existing_org(
                legacy_db_path=self.legacy_db_path,
                organization_slug="aoc",
                username_remaps={"magasiev13": "magasiev-aoc"},
                dry_run=False,
            )

            self.assertFalse(apply_summary["dry_run"])
            self.assertEqual(apply_summary["count_mismatches_after"], {})
            self.assertEqual(CommunityMember.query.filter_by(organization_id=organization.id).count(), 2)
            self.assertEqual(MessageLog.query.filter_by(organization_id=organization.id).count(), 2)
            self.assertEqual(InboxThread.query.filter_by(organization_id=organization.id).count(), 2)
            self.assertEqual(InboxMessage.query.filter_by(organization_id=organization.id).count(), 2)
            db.session.expire_all()
            self.assertEqual(AppUser.query.filter_by(username="kfolyan").one().password_hash, "pbkdf2:sha256:1$sync$hash")
            self.assertTrue(AppUser.query.filter_by(username="kfolyan").one().must_change_password)

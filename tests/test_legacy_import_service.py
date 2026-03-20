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
                    email TEXT,
                    full_name TEXT,
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
                    created_at TEXT
                );
                """
            )

            connection.execute(
                """
                INSERT INTO users (id, username, email, full_name, password_hash, phone, role, must_change_password, session_nonce, created_at)
                VALUES
                    (1, 'legacy-admin', 'admin@legacy.test', 'Legacy Admin', 'pbkdf2:sha256:1$abc$hash', '+15550000001', 'admin', 0, 'nonce-admin', '2025-01-01 00:00:00'),
                    (2, 'legacy-staff', 'staff@legacy.test', 'Legacy Staff', 'pbkdf2:sha256:1$def$hash', '+15550000002', 'social_manager', 1, 'nonce-staff', '2025-01-02 00:00:00')
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
                INSERT INTO scheduled_messages (id, created_at, scheduled_at, message_body, target, event_id, status, test_mode, attempt_count, message_log_id)
                VALUES (1, '2025-02-16 09:00:00', '2025-02-17 09:00:00', 'Scheduled legacy message', 'event', 1, 'sent', 0, 1, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO inbox_threads (id, phone, contact_name, unread_count, last_message_at, last_message_preview, last_direction, created_at, updated_at)
                VALUES (1, '+15554440001', 'Inbox Contact', 0, '2025-02-18 08:00:00', 'Hello', 'inbound', '2025-02-18 08:00:00', '2025-02-18 08:00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO inbox_messages (id, thread_id, phone, direction, body, created_at)
                VALUES (1, 1, '+15554440001', 'inbound', 'Hello', '2025-02-18 08:00:00')
                """
            )
            connection.commit()

    def test_import_legacy_sqlite_snapshot_creates_default_organization(self) -> None:
        from app import create_app, db
        from app.models import (
            AppUser,
            CommunityMember,
            Event,
            EventRegistration,
            InboxMessage,
            MessageLog,
            Organization,
            OrganizationMembership,
            ScheduledMessage,
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
            self.assertEqual(summary["counts"]["users"], 2)
            self.assertEqual(summary["counts"]["message_logs"], 1)
            self.assertEqual(AppUser.query.filter_by(is_platform_admin=False).count(), 2)
            self.assertEqual(OrganizationMembership.query.filter_by(role="owner").count(), 1)
            self.assertEqual(OrganizationMembership.query.filter_by(role="staff").count(), 1)
            self.assertEqual(CommunityMember.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(Event.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(EventRegistration.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(UnsubscribedContact.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(MessageLog.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(ScheduledMessage.query.filter_by(organization_id=organization.id).count(), 1)
            self.assertEqual(InboxMessage.query.filter_by(organization_id=organization.id).count(), 1)

            scheduled_message = ScheduledMessage.query.one()
            message_log = MessageLog.query.one()
            event = Event.query.one()
            self.assertEqual(scheduled_message.message_log_id, message_log.id)
            self.assertEqual(scheduled_message.event_id, event.id)

            audit = db.session.execute(
                text("SELECT status, organization_id FROM saas_import_runs ORDER BY id DESC LIMIT 1")
            ).first()
            self.assertIsNotNone(audit)
            self.assertEqual(audit[0], "completed")
            self.assertEqual(audit[1], organization.id)

import importlib
import os
import sys
import tempfile
import unittest

from sqlalchemy import text


class TestMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "sms.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SCHEDULER_ENABLED": "0",
            }
        )

        if "app.config" in sys.modules:
            import app.config

            importlib.reload(app.config)

        from app import create_app, db

        self.db = db
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config["TESTING"] = True
        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_migration_versions_are_unique(self) -> None:
        from app.migrations.runner import _migration_files

        versions = [migration.version for migration in _migration_files()]
        self.assertEqual(
            len(versions),
            len(set(versions)),
            "Migration versions must be unique so schema_migrations can track each file.",
        )

    def test_keyword_normalization_migration_skips_conflicts_instead_of_failing(self) -> None:
        from datetime import datetime, timezone

        from app.migrations.runner import _load_migration, _migration_files

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        # Insert legacy dirty rows via raw SQL to bypass model-level normalization validators.
        self.db.session.execute(
            text(
                """
                INSERT INTO keyword_automation_rules
                    (keyword, response_body, is_active, match_count, created_at, updated_at)
                VALUES
                    (:k1, 'A', 1, 0, :now, :now),
                    (:k2, 'B', 1, 0, :now, :now),
                    (:k3, 'C', 1, 0, :now, :now)
                """
            ),
            {
                "k1": "join now",
                "k2": "  join   now ",
                "k3": "   ",
                "now": now,
            },
        )
        self.db.session.commit()

        normalize = next(
            migration
            for migration in _migration_files()
            if migration.name == "007_normalize_inbox_keywords"
        )
        module = _load_migration(normalize)
        with self.db.engine.begin() as connection:
            module.apply(connection, self.app.logger)

        rows = self.db.session.execute(
            text("SELECT keyword FROM keyword_automation_rules ORDER BY id")
        ).scalars().all()
        self.assertEqual(rows[0], "JOIN NOW")
        self.assertEqual(rows[1], "  join   now ")
        self.assertEqual(rows[2], "   ")

    def test_drop_users_phone_unique_index_migration_removes_legacy_constraint(self) -> None:
        from app.migrations.runner import _load_migration, _migration_files

        add_auth_hardening = next(
            migration
            for migration in _migration_files()
            if migration.name == "010_add_auth_hardening_tables_and_columns"
        )
        drop_phone_unique = next(
            migration
            for migration in _migration_files()
            if migration.name == "013_drop_users_phone_unique_index"
        )

        add_auth_hardening_module = _load_migration(add_auth_hardening)
        drop_phone_unique_module = _load_migration(drop_phone_unique)

        with self.db.engine.begin() as connection:
            add_auth_hardening_module.apply(connection, self.app.logger)

        indexes_before = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND tbl_name = 'users'
                    """
                )
            ).scalars()
        )
        self.assertIn("uq_users_phone_nonempty", indexes_before)

        with self.db.engine.begin() as connection:
            drop_phone_unique_module.apply(connection, self.app.logger)

        indexes_after = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND tbl_name = 'users'
                    """
                )
            ).scalars()
        )
        self.assertNotIn("uq_users_phone_nonempty", indexes_after)
        self.assertIn("ix_users_phone", indexes_after)

    def test_add_stripe_webhook_events_migration_creates_ledger_table(self) -> None:
        from app.migrations.runner import _load_migration, _migration_files

        self.db.session.execute(text("DROP TABLE IF EXISTS stripe_webhook_events"))
        self.db.session.commit()

        add_webhook_events = next(
            migration
            for migration in _migration_files()
            if migration.name == "014_add_stripe_webhook_events"
        )
        add_webhook_events_module = _load_migration(add_webhook_events)

        with self.db.engine.begin() as connection:
            add_webhook_events_module.apply(connection, self.app.logger)

        table_names = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            ).scalars()
        )
        self.assertIn("stripe_webhook_events", table_names)

        index_names = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND tbl_name = 'stripe_webhook_events'
                    """
                )
            ).scalars()
        )
        self.assertNotIn("ix_stripe_webhook_events_object_id", index_names)
        self.assertNotIn("ix_stripe_webhook_events_customer_id", index_names)
        self.assertNotIn("ix_stripe_webhook_events_subscription_id", index_names)

    def test_drop_duplicate_stripe_webhook_indexes_migration_removes_legacy_indexes(self) -> None:
        from app.migrations.runner import _load_migration, _migration_files

        add_webhook_events = next(
            migration
            for migration in _migration_files()
            if migration.name == "014_add_stripe_webhook_events"
        )
        drop_duplicate_indexes = next(
            migration
            for migration in _migration_files()
            if migration.name == "015_drop_duplicate_stripe_webhook_indexes"
        )

        add_webhook_events_module = _load_migration(add_webhook_events)
        drop_duplicate_indexes_module = _load_migration(drop_duplicate_indexes)

        with self.db.engine.begin() as connection:
            add_webhook_events_module.apply(connection, self.app.logger)
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_object_id
                    ON stripe_webhook_events (stripe_object_id)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_customer_id
                    ON stripe_webhook_events (stripe_customer_id)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_subscription_id
                    ON stripe_webhook_events (stripe_subscription_id)
                    """
                )
            )

        indexes_before = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND tbl_name = 'stripe_webhook_events'
                    """
                )
            ).scalars()
        )
        self.assertIn("ix_stripe_webhook_events_object_id", indexes_before)
        self.assertIn("ix_stripe_webhook_events_customer_id", indexes_before)
        self.assertIn("ix_stripe_webhook_events_subscription_id", indexes_before)

        with self.db.engine.begin() as connection:
            drop_duplicate_indexes_module.apply(connection, self.app.logger)

        indexes_after = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND tbl_name = 'stripe_webhook_events'
                    """
                )
            ).scalars()
        )
        self.assertNotIn("ix_stripe_webhook_events_object_id", indexes_after)
        self.assertNotIn("ix_stripe_webhook_events_customer_id", indexes_after)
        self.assertNotIn("ix_stripe_webhook_events_subscription_id", indexes_after)

    def test_ensure_stripe_webhook_indexes_migration_restores_canonical_indexes(self) -> None:
        from app.migrations.runner import _load_migration, _migration_files

        add_webhook_events = next(
            migration
            for migration in _migration_files()
            if migration.name == "014_add_stripe_webhook_events"
        )
        ensure_canonical_indexes = next(
            migration
            for migration in _migration_files()
            if migration.name == "016_ensure_stripe_webhook_indexes"
        )

        add_webhook_events_module = _load_migration(add_webhook_events)
        ensure_canonical_indexes_module = _load_migration(ensure_canonical_indexes)

        with self.db.engine.begin() as connection:
            add_webhook_events_module.apply(connection, self.app.logger)
            ensure_canonical_indexes_module.apply(connection, self.app.logger)

        indexes = set(
            self.db.session.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'index' AND tbl_name = 'stripe_webhook_events'
                    """
                )
            ).scalars()
        )
        self.assertIn("ix_stripe_webhook_events_event_type", indexes)
        self.assertIn("ix_stripe_webhook_events_organization_id", indexes)
        self.assertIn("ix_stripe_webhook_events_status", indexes)
        self.assertIn("ix_stripe_webhook_events_stripe_object_id", indexes)
        self.assertIn("ix_stripe_webhook_events_stripe_customer_id", indexes)
        self.assertIn("ix_stripe_webhook_events_stripe_subscription_id", indexes)


if __name__ == "__main__":
    unittest.main()

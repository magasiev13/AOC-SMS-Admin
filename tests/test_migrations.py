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


if __name__ == "__main__":
    unittest.main()

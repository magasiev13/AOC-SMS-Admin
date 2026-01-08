import importlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager


class TestDbDoctor(unittest.TestCase):
    @contextmanager
    def _temporary_env(self, updates: dict[str, str]) -> None:
        original = os.environ.copy()
        os.environ.update(updates)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)
            if "app.config" in sys.modules:
                import app.config

                importlib.reload(app.config)

    def _create_legacy_message_logs_db(self, db_path: str) -> None:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TABLE message_logs (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT,
                    message_body TEXT NOT NULL,
                    target TEXT NOT NULL,
                    event_id INTEGER
                )
                """
            )

    def test_dbdoctor_without_secret_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "sms.db")
            env = os.environ.copy()
            env.pop("SECRET_KEY", None)
            env["DATABASE_URL"] = f"sqlite:///{db_path}"

            result = subprocess.run(
                [sys.executable, "-m", "app.dbdoctor", "--print"],
                env=env,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Database file:", result.stdout)

    def test_dbdoctor_applies_message_logs_columns(self) -> None:
        from sqlalchemy.exc import OperationalError

        expected_columns = {
            "status",
            "total_recipients",
            "success_count",
            "failure_count",
            "details",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "sms.db")
            self._create_legacy_message_logs_db(db_path)
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{db_path}"
            env["SECRET_KEY"] = "test-secret"
            env["FLASK_DEBUG"] = "1"

            result = subprocess.run(
                [sys.executable, "-m", "app.dbdoctor", "--apply"],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(message_logs)")
                }

            self.assertTrue(expected_columns.issubset(columns))

            with self._temporary_env(env):
                import app.config

                importlib.reload(app.config)
                from app import create_app
                from app.models import MessageLog

                app = create_app(run_startup_tasks=False, start_scheduler=False)
                with app.app_context():
                    try:
                        MessageLog.query.order_by(MessageLog.created_at.desc()).limit(5).all()
                    except OperationalError as exc:
                        self.fail(f"MessageLog query raised OperationalError after migration: {exc}")


if __name__ == "__main__":
    unittest.main()

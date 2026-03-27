import importlib
import logging
import os
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class TestSaasSchemaMigrations(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "saas.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{self.db_path}",
                "FLASK_DEBUG": "1",
                "SECRET_KEY": "test-secret-key",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
                "ADMIN_USERNAME": "admin",
                "ADMIN_EMAIL": "admin@example.com",
                "ADMIN_PASSWORD": "bootstrap-secret",
            }
        )

        import app.config

        importlib.reload(app.config)
        import app.saas_db

        self.saas_db = importlib.reload(app.saas_db)
        self.engine = create_engine(os.environ["DATABASE_URL"])
        self.logger = logging.getLogger("tests.saas_db")
        from app.models import AppUser

        self.AppUser = AppUser

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_saas_migrations_upgrade_from_prior_version(self) -> None:
        from app.saas_migrations.runner import inspect_saas_migrations, run_pending_saas_migrations

        run_pending_saas_migrations(self.engine, self.logger, target_version="001")
        partial_report = inspect_saas_migrations(self.engine)
        self.assertEqual(partial_report["applied"], ["001"])
        self.assertEqual(partial_report["pending"], ["002", "003", "004", "005"])
        self.assertIn("saas_import_runs", partial_report["missing_tables"])

        run_pending_saas_migrations(self.engine, self.logger)
        final_report = inspect_saas_migrations(self.engine)
        self.assertEqual(final_report["applied"], ["001", "002", "003", "004", "005"])
        self.assertEqual(final_report["pending"], [])
        self.assertEqual(final_report["missing_tables"], [])

    def test_ensure_platform_admin_creates_first_platform_admin(self) -> None:
        from app.saas_migrations.runner import run_pending_saas_migrations

        run_pending_saas_migrations(self.engine, self.logger)

        created = self.saas_db.ensure_platform_admin(self.engine, self.logger)

        session = sessionmaker(bind=self.engine)()
        try:
            user = session.query(self.AppUser).filter_by(is_platform_admin=True).first()
            self.assertTrue(created)
            self.assertIsNotNone(user)
            self.assertEqual(user.username, "admin")
            self.assertEqual(user.email, "admin@example.com")
            self.assertEqual(user.role, "admin")
        finally:
            session.close()

    def test_ensure_platform_admin_is_noop_when_platform_admin_exists(self) -> None:
        from app.saas_migrations.runner import run_pending_saas_migrations

        run_pending_saas_migrations(self.engine, self.logger)
        self.saas_db.ensure_platform_admin(self.engine, self.logger)

        os.environ.pop("ADMIN_PASSWORD", None)
        import app.config

        importlib.reload(app.config)
        import app.saas_db

        self.saas_db = importlib.reload(app.saas_db)
        created = self.saas_db.ensure_platform_admin(self.engine, self.logger)

        session = sessionmaker(bind=self.engine)()
        try:
            self.assertFalse(created)
            self.assertEqual(session.query(self.AppUser).filter_by(is_platform_admin=True).count(), 1)
        finally:
            session.close()

    def test_ensure_platform_admin_requires_password_when_no_platform_admin_exists(self) -> None:
        from app.saas_migrations.runner import run_pending_saas_migrations

        run_pending_saas_migrations(self.engine, self.logger)
        os.environ.pop("ADMIN_PASSWORD", None)
        import app.config

        importlib.reload(app.config)
        import app.saas_db

        self.saas_db = importlib.reload(app.saas_db)
        with self.assertRaises(RuntimeError):
            self.saas_db.ensure_platform_admin(self.engine, self.logger)

    def test_ensure_platform_admin_fails_on_username_conflict_with_non_platform_user(self) -> None:
        from app.saas_migrations.runner import run_pending_saas_migrations

        run_pending_saas_migrations(self.engine, self.logger)

        session = sessionmaker(bind=self.engine)()
        try:
            session.add(
                self.AppUser(
                    username="admin",
                    email="owner@example.com",
                    role="admin",
                    is_platform_admin=False,
                    password_hash="pbkdf2:sha256:test-hash",
                )
            )
            session.commit()
        finally:
            session.close()

        with self.assertRaises(RuntimeError):
            self.saas_db.ensure_platform_admin(self.engine, self.logger)

import importlib
import logging
import os
import tempfile
import unittest

from sqlalchemy import create_engine


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
            }
        )

        import app.config

        importlib.reload(app.config)
        self.engine = create_engine(os.environ["DATABASE_URL"])
        self.logger = logging.getLogger("tests.saas_db")

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
        self.assertEqual(partial_report["pending"], ["002"])
        self.assertIn("saas_import_runs", partial_report["missing_tables"])

        run_pending_saas_migrations(self.engine, self.logger)
        final_report = inspect_saas_migrations(self.engine)
        self.assertEqual(final_report["applied"], ["001", "002"])
        self.assertEqual(final_report["pending"], [])
        self.assertEqual(final_report["missing_tables"], [])

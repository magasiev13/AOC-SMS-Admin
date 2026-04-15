import importlib
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


class TestRuntimeBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "runtime-bootstrap.db")
        os.environ["FLASK_DEBUG"] = "1"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        os.environ["ADMIN_USERNAME"] = "magasiev13"
        os.environ["ADMIN_PASSWORD"] = "bootstrap-secret"
        os.environ.pop("SAAS_MODE", None)
        os.environ.pop("ADMIN_EMAIL", None)

        import app.config

        importlib.reload(app.config)
        from app import _ensure_bootstrap_admin_user, create_app, db
        from app.models import AppUser

        self._ensure_bootstrap_admin_user = _ensure_bootstrap_admin_user
        self.db = db
        self.AppUser = AppUser
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._app_context.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_bootstrap_admin_commit_race_is_treated_as_success(self) -> None:
        original_commit = self.db.session.commit
        session_factory = sessionmaker(bind=self.db.engine)
        admin_username = self.app.config["ADMIN_USERNAME"]
        admin_email = f"{admin_username}@example.com"
        commit_calls = 0

        def commit_with_race() -> None:
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                other_session = session_factory()
                try:
                    other_user = self.AppUser(
                        username=admin_username,
                        email=admin_email,
                        role="admin",
                        password_hash="pbkdf2:sha256:test-hash",
                    )
                    other_session.add(other_user)
                    other_session.commit()
                finally:
                    other_session.close()
                raise IntegrityError(
                    "INSERT INTO users ...",
                    {"username": admin_username},
                    Exception("UNIQUE constraint failed: users.username"),
                )
            original_commit()

        with patch.object(self.db.session, "commit", side_effect=commit_with_race):
            self._ensure_bootstrap_admin_user(self.app)

        user = self.AppUser.query.filter_by(username=admin_username).first()
        self.assertIsNotNone(user)
        self.assertEqual(self.AppUser.query.count(), 1)
        self.assertEqual(user.role, "admin")

    def test_bootstrap_admin_still_raises_on_unrelated_integrity_error(self) -> None:
        with patch.object(
            self.db.session,
            "commit",
            side_effect=IntegrityError(
                "INSERT INTO users ...",
                {"username": self.app.config["ADMIN_USERNAME"]},
                Exception("UNIQUE constraint failed: users.email"),
            ),
        ):
            with self.assertRaises(IntegrityError):
                self._ensure_bootstrap_admin_user(self.app)

    def test_template_context_uses_twinevia_brand_defaults(self) -> None:
        with self.app.test_request_context("/"):
            context = {}
            for processor in self.app.template_context_processors[None]:
                context.update(processor())

        self.assertEqual(context["product_name"], "Twinevia")
        self.assertEqual(context["product_descriptor"], "Messaging Workspace")


class TestSaasRuntimeBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "runtime-bootstrap-saas.db")
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "FLASK_DEBUG": "1",
                "SAAS_MODE": "1",
                "SCHEDULER_ENABLED": "0",
                "SECRET_KEY": "test-secret-key",
                "ADMIN_USERNAME": "platform-admin",
                "ADMIN_PASSWORD": "Platform-pass1!",
                "ADMIN_EMAIL": "platform@example.com",
            }
        )

        import app.config
        import app.saas_db

        importlib.reload(app.config)
        self.saas_db = importlib.reload(app.saas_db)
        from app import create_runtime_app, db
        from app.models import AppUser
        from app.saas_migrations.runner import run_pending_saas_migrations

        self.create_runtime_app = create_runtime_app
        self.db = db
        self.AppUser = AppUser
        self.run_pending_saas_migrations = run_pending_saas_migrations
        self.logger = logging.getLogger("tests.runtime_bootstrap.saas")
        self.engine = create_engine(os.environ["DATABASE_URL"])

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def test_runtime_boot_in_saas_mode_skips_legacy_admin_bootstrap(self) -> None:
        self.run_pending_saas_migrations(self.engine, self.logger)

        with patch("app._ensure_bootstrap_admin_user") as mock_bootstrap:
            self.create_runtime_app(start_scheduler=False)

        mock_bootstrap.assert_not_called()

    def test_runtime_boot_succeeds_without_admin_password_after_platform_admin_provisioning(self) -> None:
        self.run_pending_saas_migrations(self.engine, self.logger)
        self.saas_db.ensure_platform_admin(self.engine, self.logger)
        os.environ.pop("ADMIN_PASSWORD", None)

        import app.config

        importlib.reload(app.config)
        app = self.create_runtime_app(start_scheduler=False)
        with app.app_context():
            self.assertEqual(self.AppUser.query.filter_by(is_platform_admin=True).count(), 1)


if __name__ == "__main__":
    unittest.main()

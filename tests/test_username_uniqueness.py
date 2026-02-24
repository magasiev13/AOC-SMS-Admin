import importlib
import os
import tempfile
import unittest

from sqlalchemy.exc import IntegrityError


class TestUsernameUniqueness(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser

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
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug
        os.environ.pop("DATABASE_URL", None)

    def test_case_variant_usernames_conflict_at_database_level(self) -> None:
        first = self.AppUser(username="CaseUser", phone="+15550005001", role="admin", must_change_password=False)
        first.set_password("Case-pass1!")
        self.db.session.add(first)
        self.db.session.commit()

        second = self.AppUser(username="caseuser", phone="+15550005002", role="admin", must_change_password=False)
        second.set_password("Case-pass1!")
        self.db.session.add(second)

        with self.assertRaises(IntegrityError):
            self.db.session.commit()

        self.db.session.rollback()


if __name__ == "__main__":
    unittest.main()

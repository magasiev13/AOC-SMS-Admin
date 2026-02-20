import os
import tempfile
import unittest


class TestUserCreationMustChangePassword(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        import importlib
        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser

        self.db = db
        self.AppUser = AppUser
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(username="admin", role="admin", must_change_password=False)
        admin.set_password("admin-pass")
        self.db.session.add(admin)
        self.db.session.commit()

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

    def _login(self) -> None:
        return self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-pass"},
            follow_redirects=False,
        )

    def test_unchecked_must_change_password_creates_user_without_flag(self) -> None:
        self._login()
        response = self.client.post(
            "/users/add",
            data={
                "username": "new-user",
                "role": "social_manager",
                "password": "new-password-123",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        user = self.AppUser.query.filter_by(username="new-user").first()
        self.assertIsNotNone(user)
        self.assertFalse(user.must_change_password)

    def test_checked_must_change_password_creates_user_with_flag(self) -> None:
        self._login()
        response = self.client.post(
            "/users/add",
            data={
                "username": "new-user-2",
                "role": "social_manager",
                "password": "new-password-123",
                "must_change_password": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        user = self.AppUser.query.filter_by(username="new-user-2").first()
        self.assertIsNotNone(user)
        self.assertTrue(user.must_change_password)


if __name__ == "__main__":
    unittest.main()

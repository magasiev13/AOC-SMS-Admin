import os
import tempfile
import unittest

from app import create_app, db
from app.models import AppUser


class TestPasswordChangeFlow(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        user = AppUser(username="forced", role="admin", must_change_password=True)
        user.set_password("old-password")
        db.session.add(user)
        db.session.commit()

    def tearDown(self) -> None:
        db.session.remove()
        db.drop_all()
        self._app_context.pop()
        self._temp_dir.cleanup()
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug

    def _login(self, password: str) -> None:
        return self.client.post(
            "/login",
            data={"username": "forced", "password": password},
            follow_redirects=False,
        )

    def test_new_user_login_forces_password_change(self) -> None:
        response = self._login("old-password")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/account/password", response.headers.get("Location", ""))

    def test_password_change_clears_flag_and_allows_navigation(self) -> None:
        self._login("old-password")
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "old-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers.get("Location", ""))

        user = AppUser.query.filter_by(username="forced").first()
        self.assertIsNotNone(user)
        self.assertFalse(user.must_change_password)

        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)

    def test_incorrect_current_password_is_rejected(self) -> None:
        self._login("old-password")
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "wrong-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Current password is incorrect.", response.data)

        user = AppUser.query.filter_by(username="forced").first()
        self.assertIsNotNone(user)
        self.assertTrue(user.must_change_password)


if __name__ == "__main__":
    unittest.main()

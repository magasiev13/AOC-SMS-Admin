import importlib
import os
import tempfile
import unittest

from werkzeug.security import generate_password_hash


class TestPasswordPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, UserPasswordHistory

        self.db = db
        self.AppUser = AppUser
        self.UserPasswordHistory = UserPasswordHistory
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, PASSWORD_HISTORY_COUNT=3)
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        user = self.AppUser(
            username="policy-admin",
            phone="+15550002001",
            role="admin",
            must_change_password=False,
        )
        user.set_password("Current-pass1!")
        self.db.session.add(user)
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
            data={"username": "policy-admin", "password": "Current-pass1!"},
            follow_redirects=False,
        )

    def test_change_password_rejects_weak_password(self) -> None:
        self._login()
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "Current-pass1!",
                "new_password": "short",
                "confirm_password": "short",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Password must be at least 12 characters.", response.data)

    def test_change_password_rejects_username_in_password(self) -> None:
        self._login()
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "Current-pass1!",
                "new_password": "Policy-Admin-456!",
                "confirm_password": "Policy-Admin-456!",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Password cannot contain your username.", response.data)

    def test_change_password_rejects_current_password_reuse(self) -> None:
        self._login()
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "Current-pass1!",
                "new_password": "Current-pass1!",
                "confirm_password": "Current-pass1!",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"New password cannot match your current or recently used passwords.",
            response.data,
        )

    def test_change_password_rejects_recent_password_reuse(self) -> None:
        user = self.AppUser.query.filter_by(username="policy-admin").first()
        self.assertIsNotNone(user)
        self.db.session.add(
            self.UserPasswordHistory(
                user_id=user.id,
                password_hash=generate_password_hash("Older-pass1!"),
            )
        )
        self.db.session.commit()

        self._login()
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "Current-pass1!",
                "new_password": "Older-pass1!",
                "confirm_password": "Older-pass1!",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"New password cannot match your current or recently used passwords.",
            response.data,
        )


if __name__ == "__main__":
    unittest.main()

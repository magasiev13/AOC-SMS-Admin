import importlib
import os
import tempfile
import unittest


class TestAuthSessionInvalidation(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, AuthEvent

        self.db = db
        self.AppUser = AppUser
        self.AuthEvent = AuthEvent
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()

        self.client = self.app.test_client()

        admin = self.AppUser(
            username="admin",
            phone="+15550003001",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("Admin-pass1!")
        target = self.AppUser(
            username="target",
            phone="+15550003002",
            role="social_manager",
            must_change_password=False,
        )
        target.set_password("Target-pass1!")
        self.db.session.add_all([admin, target])
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

    def _login_admin(self, password: str = "Admin-pass1!"):
        return self.client.post(
            "/login",
            data={"username": "admin", "password": password},
            follow_redirects=False,
        )

    def _login_target(self, password: str):
        return self.client.post(
            "/login",
            data={"username": "target", "password": password},
            follow_redirects=False,
        )

    def test_password_change_invalidates_current_session(self) -> None:
        self._login_target("Target-pass1!")
        user = self.AppUser.query.filter_by(username="target").first()
        self.assertIsNotNone(user)
        old_nonce = user.session_nonce

        response = self.client.post(
            "/account/password",
            data={
                "current_password": "Target-pass1!",
                "new_password": "Fresh-Newpass1!",
                "confirm_password": "Fresh-Newpass1!",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

        self.db.session.refresh(user)
        self.assertNotEqual(old_nonce, user.session_nonce)

        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(dashboard.status_code, 302)
        self.assertIn("/login", dashboard.headers.get("Location", ""))

        old_login = self._login_target("Target-pass1!")
        self.assertEqual(old_login.status_code, 200)
        self.assertIn(b"Invalid username or password.", old_login.data)

        new_login = self._login_target("Fresh-Newpass1!")
        self.assertEqual(new_login.status_code, 302)
        self.assertIn("/dashboard", new_login.headers.get("Location", ""))

    def test_admin_reset_revokes_target_sessions_and_forces_password_change(self) -> None:
        from app.auth import load_user

        self._login_admin()

        target = self.AppUser.query.filter_by(username="target").first()
        self.assertIsNotNone(target)
        old_nonce = target.session_nonce
        stale_session_id = target.get_id()

        response = self.client.post(
            f"/users/{target.id}/edit",
            data={
                "username": "target",
                "role": "social_manager",
                "phone": "+15550003002",
                "password": "Admin-Reset1!",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/users", response.headers.get("Location", ""))

        self.db.session.refresh(target)
        self.assertTrue(target.must_change_password)
        self.assertNotEqual(old_nonce, target.session_nonce)

        stale_user = load_user(stale_session_id)
        self.assertIsNone(stale_user)

        self.client.post("/logout", follow_redirects=False)

        relogin = self._login_target("Admin-Reset1!")
        self.assertEqual(relogin.status_code, 302)
        self.assertIn("/account/password", relogin.headers.get("Location", ""))

        reset_event = (
            self.AuthEvent.query.filter_by(event_type="admin_password_reset", username="target")
            .order_by(self.AuthEvent.id.desc())
            .first()
        )
        self.assertIsNotNone(reset_event)


if __name__ == "__main__":
    unittest.main()

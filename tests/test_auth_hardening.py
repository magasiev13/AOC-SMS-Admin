import importlib
import os
import tempfile
import unittest


class TestAuthHardening(unittest.TestCase):
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
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            AUTH_LOCKOUT_MAX_ATTEMPTS=3,
            AUTH_LOCKOUT_WINDOW_SECONDS=300,
            AUTH_LOCKOUT_SECONDS=600,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(username="admin", phone="+15550001001", role="admin", must_change_password=False)
        admin.set_password("admin-pass")
        viewer = self.AppUser(username="viewer", phone="+15550001002", role="viewer", must_change_password=False)
        viewer.set_password("viewer-pass")
        no_phone = self.AppUser(username="no-phone", phone=None, role="admin", must_change_password=False)
        no_phone.set_password("no-phone-pass")

        self.db.session.add_all([admin, viewer, no_phone])
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

    def _login(self, username: str, password: str):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )

    def test_logout_requires_post(self) -> None:
        self._login("admin", "admin-pass")

        response = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(response.status_code, 405)

        response = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_missing_phone_is_redirected_to_security_contact(self) -> None:
        self._login("no-phone", "no-phone-pass")

        response = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/account/security-contact", response.headers.get("Location", ""))

        save_response = self.client.post(
            "/account/security-contact",
            data={"phone": "+15550001003"},
            follow_redirects=False,
        )
        self.assertEqual(save_response.status_code, 302)

        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(dashboard.status_code, 200)

    def test_password_change_rotates_session_and_requires_relogin(self) -> None:
        self._login("admin", "admin-pass")

        user = self.AppUser.query.filter_by(username="admin").first()
        self.assertIsNotNone(user)
        old_nonce = user.session_nonce

        response = self.client.post(
            "/account/password",
            data={
                "current_password": "admin-pass",
                "new_password": "New-secure1!",
                "confirm_password": "New-secure1!",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

        self.db.session.refresh(user)
        self.assertNotEqual(user.session_nonce, old_nonce)

        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(dashboard.status_code, 302)
        self.assertIn("/login", dashboard.headers.get("Location", ""))

        relogin = self._login("admin", "New-secure1!")
        self.assertEqual(relogin.status_code, 302)

    def test_security_events_admin_only(self) -> None:
        self._login("admin", "admin-pass")
        response = self.client.get("/security/events")
        self.assertEqual(response.status_code, 200)

        self.client.post("/logout", follow_redirects=False)
        self._login("viewer", "viewer-pass")
        forbidden = self.client.get("/security/events")
        self.assertEqual(forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()

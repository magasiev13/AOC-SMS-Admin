import importlib
import os
import tempfile
import unittest


class TestSecurityContactGate(unittest.TestCase):
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
        self.client = self.app.test_client()

        no_phone = self.AppUser(
            username="no-phone",
            phone=None,
            role="admin",
            must_change_password=False,
        )
        no_phone.set_password("No-phone1!")
        other_user = self.AppUser(
            username="other-user",
            phone="+15550005099",
            role="social_manager",
            must_change_password=False,
        )
        other_user.set_password("Other-user1!")
        self.db.session.add_all([no_phone, other_user])
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

    def _login_no_phone_user(self):
        return self.client.post(
            "/login",
            data={"username": "no-phone", "password": "No-phone1!"},
            follow_redirects=False,
        )

    def test_missing_phone_user_is_blocked_until_security_contact_is_saved(self) -> None:
        self._login_no_phone_user()

        blocked_route = self.client.get("/users", follow_redirects=False)
        self.assertEqual(blocked_route.status_code, 302)
        self.assertIn("/account/security-contact", blocked_route.headers.get("Location", ""))

        invalid = self.client.post(
            "/account/security-contact",
            data={"phone": "123"},
            follow_redirects=True,
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertIn(b"Phone number must be a valid E.164 number.", invalid.data)

        still_blocked = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(still_blocked.status_code, 302)
        self.assertIn("/account/security-contact", still_blocked.headers.get("Location", ""))

        saved = self.client.post(
            "/account/security-contact",
            data={"phone": "+15550005001"},
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 302)

        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(dashboard.status_code, 200)

    def test_security_contact_rejects_duplicate_phone(self) -> None:
        self._login_no_phone_user()

        duplicate = self.client.post(
            "/account/security-contact",
            data={"phone": "+15550005099"},
            follow_redirects=True,
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn(b"That phone number is already assigned to another user.", duplicate.data)

        blocked = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/account/security-contact", blocked.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()

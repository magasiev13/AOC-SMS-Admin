import os
import tempfile
import unittest


class TestLoginHardeningConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "login-hardening.db")

        os.environ["FLASK_DEBUG"] = "1"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        os.environ["AUTH_ATTEMPT_WINDOW_SECONDS"] = "300"
        os.environ["AUTH_LOCKOUT_SECONDS"] = "900"
        os.environ["AUTH_MAX_ATTEMPTS_IP_ACCOUNT"] = "5"
        os.environ["AUTH_MAX_ATTEMPTS_ACCOUNT"] = "8"
        os.environ["AUTH_MAX_ATTEMPTS_IP"] = "30"

        import importlib
        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser

        self.db = db
        self.AppUser = AppUser
        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

        self._ctx = self.app.app_context()
        self._ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        user = self.AppUser(username="admin", role="admin", must_change_password=False)
        user.set_password("correct-password-123")
        self.db.session.add(user)
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._ctx.pop()
        self._temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._original_env)

    def _post_login(self, username: str, password: str):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def test_ip_limit_from_config_is_applied(self) -> None:
        os.environ["AUTH_MAX_ATTEMPTS_IP"] = "2"
        import importlib
        import app.config

        importlib.reload(app.config)
        self.app.config["AUTH_MAX_ATTEMPTS_IP"] = 2

        self._post_login("admin", "wrong-pass")
        self._post_login("admin", "wrong-pass")
        response = self._post_login("admin", "wrong-pass")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Too many failed attempts", response.data)

    def test_account_limit_from_config_is_applied(self) -> None:
        os.environ["AUTH_MAX_ATTEMPTS_ACCOUNT"] = "1"
        os.environ["AUTH_MAX_ATTEMPTS_IP"] = "30"
        import importlib
        import app.config

        importlib.reload(app.config)
        self.app.config["AUTH_MAX_ATTEMPTS_ACCOUNT"] = 1
        self.app.config["AUTH_MAX_ATTEMPTS_IP"] = 30

        self._post_login("admin", "wrong-pass")
        response = self._post_login("admin", "wrong-pass")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Too many failed attempts", response.data)

    def test_successful_login_marks_session_permanent(self) -> None:
        response = self._post_login("admin", "correct-password-123")

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertTrue(session.permanent)


if __name__ == "__main__":
    unittest.main()

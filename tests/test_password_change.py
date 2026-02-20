import os
import tempfile
import unittest


class TestPasswordChangeFlow(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        self._original_flask_env = os.environ.get("FLASK_ENV")
        self._original_sms_admin_env_file = os.environ.get("SMS_ADMIN_ENV_FILE")
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

        user = self.AppUser(username="forced", role="admin", must_change_password=True)
        user.set_password("old-password")
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
        if self._original_flask_env is None:
            os.environ.pop("FLASK_ENV", None)
        else:
            os.environ["FLASK_ENV"] = self._original_flask_env
        if self._original_sms_admin_env_file is None:
            os.environ.pop("SMS_ADMIN_ENV_FILE", None)
        else:
            os.environ["SMS_ADMIN_ENV_FILE"] = self._original_sms_admin_env_file
        os.environ.pop("DATABASE_URL", None)

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

        user = self.AppUser.query.filter_by(username="forced").first()
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

        user = self.AppUser.query.filter_by(username="forced").first()
        self.assertIsNotNone(user)
        self.assertTrue(user.must_change_password)

    def test_production_change_removes_bootstrap_admin_password_from_env_file(self) -> None:
        env_path = os.path.join(self._temp_dir.name, "prod.env")
        with open(env_path, "w", encoding="utf-8") as env_file:
            env_file.write("FLASK_ENV=production\n")
            env_file.write("ADMIN_USERNAME=forced\n")
            env_file.write("ADMIN_PASSWORD=bootstrap-secret\n")
            env_file.write("OTHER_KEY=keep\n")

        os.environ["FLASK_ENV"] = "production"
        os.environ["SMS_ADMIN_ENV_FILE"] = env_path
        os.environ["ADMIN_PASSWORD"] = "bootstrap-secret"
        self.app.config["DEBUG"] = False
        self.app.config["ADMIN_USERNAME"] = "forced"
        self.app.config["ADMIN_PASSWORD"] = "bootstrap-secret"

        self._login("old-password")
        response = self.client.post(
            "/account/password",
            data={
                "current_password": "old-password",
                "new_password": "new-password-123",
                "confirm_password": "new-password-123",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with open(env_path, "r", encoding="utf-8") as env_file:
            env_contents = env_file.read()
        self.assertNotIn("ADMIN_PASSWORD=", env_contents)
        self.assertIn("OTHER_KEY=keep", env_contents)
        self.assertIsNone(os.environ.get("ADMIN_PASSWORD"))
        self.assertIsNone(self.app.config.get("ADMIN_PASSWORD"))


if __name__ == "__main__":
    unittest.main()

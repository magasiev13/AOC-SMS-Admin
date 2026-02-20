import importlib
import os
import tempfile
import unittest


class TestConfigSecurityHardening(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.copy()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "config-security.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
        os.environ.pop("FLASK_DEBUG", None)
        os.environ.pop("FLASK_ENV", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._original_env)
        self._temp_dir.cleanup()

    def _reload_config_module(self):
        import app.config

        importlib.reload(app.config)
        return app.config

    def test_recommended_hardening_defaults(self) -> None:
        config_module = self._reload_config_module()
        Config = config_module.Config

        self.assertEqual(Config.AUTH_ATTEMPT_WINDOW_SECONDS, 300)
        self.assertEqual(Config.AUTH_LOCKOUT_SECONDS, 900)
        self.assertEqual(Config.AUTH_MAX_ATTEMPTS_IP_ACCOUNT, 5)
        self.assertEqual(Config.AUTH_MAX_ATTEMPTS_ACCOUNT, 8)
        self.assertEqual(Config.AUTH_MAX_ATTEMPTS_IP, 30)
        self.assertEqual(Config.SESSION_IDLE_TIMEOUT_MINUTES, 30)
        self.assertEqual(Config.REMEMBER_COOKIE_DURATION_DAYS, 7)
        self.assertEqual(Config.AUTH_PASSWORD_MIN_LENGTH, 12)
        self.assertTrue(Config.AUTH_PASSWORD_POLICY_ENFORCE)

    def test_invalid_integer_config_raises_clear_error(self) -> None:
        os.environ["AUTH_LOCKOUT_SECONDS"] = "not-a-number"

        with self.assertRaises(RuntimeError) as ctx:
            self._reload_config_module()

        self.assertIn("AUTH_LOCKOUT_SECONDS must be an integer", str(ctx.exception))

    def test_production_requires_trusted_hosts(self) -> None:
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-production-secret-key"
        os.environ.pop("TRUSTED_HOSTS", None)

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("TRUSTED_HOSTS must include your production hostnames", str(ctx.exception))

    def test_security_variables_have_non_technical_comments(self) -> None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "app", "config.py")
        with open(config_path, "r", encoding="utf-8") as config_file:
            lines = config_file.read().splitlines()

        security_variable_names = [
            "SECRET_KEY =",
            "TRUST_PROXY =",
            "SESSION_COOKIE_SAMESITE =",
            "SESSION_COOKIE_SECURE =",
            "SESSION_IDLE_TIMEOUT_MINUTES =",
            "REMEMBER_COOKIE_DURATION_DAYS =",
            "AUTH_ATTEMPT_WINDOW_SECONDS =",
            "AUTH_LOCKOUT_SECONDS =",
            "AUTH_MAX_ATTEMPTS_IP_ACCOUNT =",
            "AUTH_MAX_ATTEMPTS_ACCOUNT =",
            "AUTH_MAX_ATTEMPTS_IP =",
            "AUTH_PASSWORD_MIN_LENGTH =",
            "AUTH_PASSWORD_POLICY_ENFORCE =",
            "TRUSTED_HOSTS =",
        ]

        for index, line in enumerate(lines):
            if any(name in line for name in security_variable_names):
                previous_line = lines[index - 1].strip() if index > 0 else ""
                self.assertTrue(
                    previous_line.startswith("#"),
                    f"Expected a plain-language comment directly above: {line.strip()}",
                )


if __name__ == "__main__":
    unittest.main()

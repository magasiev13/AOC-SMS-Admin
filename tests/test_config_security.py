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

    def _configure_minimal_saas_production_env(self) -> None:
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-production-secret-key"
        os.environ["SAAS_MODE"] = "1"
        os.environ["TRUSTED_HOSTS"] = "app.example.com"
        os.environ["SAAS_BASE_URL"] = "https://app.example.com"
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_123"
        os.environ["STRIPE_PRICE_ID"] = "price_test_123"
        os.environ["STRIPE_ANNUAL_PRICE_ID"] = "price_annual_123"
        os.environ["STRIPE_ACTIVATION_PRICE_ID"] = "price_activation_123"
        os.environ["TWILIO_CREDENTIAL_ENCRYPTION_KEY"] = "4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o="

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
        self.assertTrue(Config.TWILIO_VALIDATE_INBOUND_SIGNATURE)
        self.assertTrue(Config.SECURITY_HEADERS_ENABLED)
        self.assertFalse(Config.SECURITY_HSTS_ENABLED)
        self.assertEqual(Config.SECURITY_HSTS_MAX_AGE, 31536000)
        self.assertIn("frame-ancestors 'none'", Config.SECURITY_CONTENT_SECURITY_POLICY)

    def test_invalid_integer_config_raises_clear_error(self) -> None:
        os.environ["AUTH_LOCKOUT_SECONDS"] = "not-a-number"

        with self.assertRaises(RuntimeError) as ctx:
            self._reload_config_module()

        self.assertIn("AUTH_LOCKOUT_SECONDS must be an integer", str(ctx.exception))

    def test_bool_config_accepts_common_true_false_strings(self) -> None:
        os.environ["TRUST_PROXY"] = "true"
        os.environ["TWILIO_VALIDATE_INBOUND_SIGNATURE"] = "no"

        config_module = self._reload_config_module()
        Config = config_module.Config

        self.assertTrue(Config.TRUST_PROXY)
        self.assertFalse(Config.TWILIO_VALIDATE_INBOUND_SIGNATURE)

    def test_invalid_boolean_config_raises_clear_error(self) -> None:
        os.environ["INBOUND_AUTO_REPLY_ENABLED"] = "sometimes"

        with self.assertRaises(RuntimeError) as ctx:
            self._reload_config_module()

        self.assertIn("INBOUND_AUTO_REPLY_ENABLED must be a boolean value", str(ctx.exception))

    def test_cookie_samesite_is_normalized_case_insensitively(self) -> None:
        os.environ["SESSION_COOKIE_SAMESITE"] = "strict"

        config_module = self._reload_config_module()
        Config = config_module.Config

        self.assertEqual(Config.SESSION_COOKIE_SAMESITE, "Strict")
        self.assertEqual(Config.REMEMBER_COOKIE_SAMESITE, "Strict")

    def test_invalid_cookie_samesite_raises_clear_error(self) -> None:
        os.environ["SESSION_COOKIE_SAMESITE"] = "sideways"

        with self.assertRaises(RuntimeError) as ctx:
            self._reload_config_module()

        self.assertIn("SESSION_COOKIE_SAMESITE must be one of Lax, Strict, or None", str(ctx.exception))

    def test_production_requires_trusted_hosts(self) -> None:
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-production-secret-key"
        os.environ.pop("TRUSTED_HOSTS", None)

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("TRUSTED_HOSTS must include your production hostnames", str(ctx.exception))

    def test_production_accepts_lowercase_cookie_samesite(self) -> None:
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-production-secret-key"
        os.environ["TRUSTED_HOSTS"] = "sms.example.org"
        os.environ["SESSION_COOKIE_SAMESITE"] = "lax"

        self._reload_config_module()

        from app import create_app

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertEqual(app.config["REMEMBER_COOKIE_SAMESITE"], "Lax")

    def test_explicit_production_rejects_debug_flag(self) -> None:
        self._configure_minimal_saas_production_env()
        os.environ["FLASK_DEBUG"] = "1"

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("FLASK_DEBUG must be unset or 0", str(ctx.exception))

    def test_explicit_production_rejects_sqlite_for_saas(self) -> None:
        self._configure_minimal_saas_production_env()

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("Production SaaS requires PostgreSQL DATABASE_URL", str(ctx.exception))

    def test_explicit_production_rejects_fake_provider_flags(self) -> None:
        self._configure_minimal_saas_production_env()
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:pass@127.0.0.1:5432/twinevia"
        os.environ["STRIPE_FAKE_CHECKOUT_ENABLED"] = "1"
        os.environ["TWILIO_BROWSER_FAKE_SENDS"] = "1"
        os.environ["TWILIO_A2P_FAKE_QUEUE"] = "1"

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("STRIPE_FAKE_CHECKOUT_ENABLED must be disabled", str(ctx.exception))
        self.assertIn("TWILIO_BROWSER_FAKE_SENDS must be disabled", str(ctx.exception))
        self.assertIn("TWILIO_A2P_FAKE_QUEUE must be disabled", str(ctx.exception))

    def test_explicit_production_rejects_disabled_twilio_signature_validation(self) -> None:
        self._configure_minimal_saas_production_env()
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:pass@127.0.0.1:5432/twinevia"
        os.environ["TWILIO_VALIDATE_INBOUND_SIGNATURE"] = "0"

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("TWILIO_VALIDATE_INBOUND_SIGNATURE must be enabled", str(ctx.exception))

    def test_explicit_production_rejects_disabled_security_headers(self) -> None:
        self._configure_minimal_saas_production_env()
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:pass@127.0.0.1:5432/twinevia"
        os.environ["SECURITY_HEADERS_ENABLED"] = "0"

        self._reload_config_module()

        from app import create_app

        with self.assertRaises(RuntimeError) as ctx:
            create_app(run_startup_tasks=False, start_scheduler=False)

        self.assertIn("SECURITY_HEADERS_ENABLED must be enabled", str(ctx.exception))

    def test_security_headers_are_applied_to_responses(self) -> None:
        os.environ["SECRET_KEY"] = "test-secret-key"
        os.environ["SECURITY_HSTS_ENABLED"] = "1"

        self._reload_config_module()

        from app import create_app

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        app.testing = True
        response = app.test_client().get("/health", base_url="https://app.example.com")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertIn("camera=()", response.headers["Permissions-Policy"])
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000")

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
            "SECURITY_HEADERS_ENABLED =",
            "SECURITY_HSTS_ENABLED =",
            "SECURITY_HSTS_MAX_AGE =",
            "SECURITY_REFERRER_POLICY =",
            "SECURITY_PERMISSIONS_POLICY =",
            "SECURITY_CONTENT_SECURITY_POLICY =",
        ]

        for index, line in enumerate(lines):
            if any(name in line for name in security_variable_names):
                previous_line = lines[index - 1].strip() if index > 0 else ""
                self.assertTrue(
                    previous_line.startswith("#"),
                    f"Expected a plain-language comment directly above: {line.strip()}",
                )

    def test_trusted_hosts_enforced_without_localhost_bypass(self) -> None:
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-production-secret-key"
        os.environ["TRUSTED_HOSTS"] = "sms.example.org"

        self._reload_config_module()

        from app import create_app

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        app.testing = True
        client = app.test_client()

        localhost_response = client.get("/", headers={"Host": "localhost"})
        self.assertEqual(localhost_response.status_code, 400)

        trusted_response = client.get("/", headers={"Host": "sms.example.org"})
        self.assertNotEqual(trusted_response.status_code, 400)

    def test_trusted_hosts_redirect_to_saas_base_url_host(self) -> None:
        os.environ["FLASK_ENV"] = "production"
        os.environ["SECRET_KEY"] = "test-production-secret-key"
        os.environ["TRUSTED_HOSTS"] = "twinevia.com,www.twinevia.com"
        os.environ["SAAS_BASE_URL"] = "https://www.twinevia.com"

        self._reload_config_module()

        from app import create_app

        app = create_app(run_startup_tasks=False, start_scheduler=False)
        app.testing = True
        client = app.test_client()

        response = client.get(
            "/login?next=%2Fplatform",
            headers={"Host": "twinevia.com"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 308)
        self.assertEqual(
            response.headers["Location"],
            "https://www.twinevia.com/login?next=%2Fplatform",
        )

        canonical_response = client.get(
            "/login",
            headers={"Host": "www.twinevia.com"},
            follow_redirects=False,
        )
        self.assertNotEqual(canonical_response.status_code, 308)

    def test_env_example_is_marked_as_local_bootstrap_only(self) -> None:
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as env_file:
            content = env_file.read()

        self.assertIn("# Twinevia local SaaS bootstrap defaults", content)
        self.assertIn("Do not copy it directly to a", content)
        self.assertIn("FLASK_ENV=development", content)

    def test_saas_mode_defaults_to_twinevia_local_db_and_queue(self) -> None:
        os.environ.pop("DATABASE_URL", None)
        os.environ["SAAS_MODE"] = "1"

        config_module = self._reload_config_module()
        Config = config_module.Config

        self.assertTrue(Config.SQLALCHEMY_DATABASE_URI.endswith("/instance/twinevia.db"))
        self.assertEqual(Config.RQ_QUEUE_NAME, "twinevia-saas")

    def test_legacy_mode_defaults_to_legacy_local_db_and_queue(self) -> None:
        os.environ.pop("DATABASE_URL", None)
        os.environ["SAAS_MODE"] = "0"

        config_module = self._reload_config_module()
        Config = config_module.Config

        self.assertTrue(Config.SQLALCHEMY_DATABASE_URI.endswith("/instance/sms.db"))
        self.assertEqual(Config.RQ_QUEUE_NAME, "sms")

    def test_env_example_uses_twinevia_saas_defaults(self) -> None:
        env_example_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        with open(env_example_path, "r", encoding="utf-8") as env_file:
            content = env_file.read()

        self.assertIn("# Twinevia local SaaS bootstrap defaults", content)
        self.assertIn("DATABASE_URL=sqlite:///instance/twinevia.db", content)
        self.assertIn("RQ_QUEUE_NAME=twinevia-saas", content)
        self.assertIn("# TWILIO_PLATFORM_FRIENDLY_NAME=Twinevia", content)
        self.assertIn(
            "# PLATFORM_SERVICE_RESTART_SCRIPT=/usr/local/bin/restart-twinevia-saas-services",
            content,
        )


if __name__ == "__main__":
    unittest.main()

import importlib
import os
import tempfile
import unittest


class TestLoginLockout(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, AuthEvent, LoginAttempt

        self.db = db
        self.AppUser = AppUser
        self.AuthEvent = AuthEvent
        self.LoginAttempt = LoginAttempt
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

        user = self.AppUser(
            username="lockuser",
            phone="+15550004001",
            role="admin",
            must_change_password=False,
        )
        user.set_password("Lock-user1!")
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

    def _post_login(self, username: str, password: str, ip_address: str):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            environ_overrides={"REMOTE_ADDR": ip_address},
            follow_redirects=True,
        )

    def test_lockout_applies_to_username_scope_and_records_audit_events(self) -> None:
        for _ in range(3):
            failed = self._post_login("lockuser", "wrong-password", "10.1.1.1")
            self.assertEqual(failed.status_code, 200)
            self.assertIn(b"Invalid username or password.", failed.data)

        blocked = self._post_login("lockuser", "Lock-user1!", "10.1.1.2")
        self.assertEqual(blocked.status_code, 200)
        self.assertIn(b"Too many failed attempts.", blocked.data)

        ip_scope_record = self.LoginAttempt.query.filter_by(client_ip="10.1.1.1", username="").first()
        account_scope_record = self.LoginAttempt.query.filter_by(
            client_ip="__account__",
            username="lockuser",
        ).first()
        self.assertIsNotNone(ip_scope_record)
        self.assertIsNotNone(account_scope_record)
        self.assertIsNotNone(account_scope_record.locked_until)

        blocked_event = (
            self.AuthEvent.query.filter_by(event_type="login_blocked", username="lockuser")
            .order_by(self.AuthEvent.id.desc())
            .first()
        )
        self.assertIsNotNone(blocked_event)
        self.assertEqual(blocked_event.metadata_payload.get("scope"), "account")

        alert_failure_event = (
            self.AuthEvent.query.filter_by(event_type="alert_sms_failed", username="lockuser")
            .order_by(self.AuthEvent.id.desc())
            .first()
        )
        self.assertIsNotNone(alert_failure_event)
        self.assertEqual(alert_failure_event.metadata_payload.get("context"), "account_lockout")


if __name__ == "__main__":
    unittest.main()

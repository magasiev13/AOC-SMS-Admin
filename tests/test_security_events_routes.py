import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone


class TestSecurityEventsRoutes(unittest.TestCase):
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

        admin = self.AppUser(username="admin", phone="+15550006001", role="admin", must_change_password=False)
        admin.set_password("Admin-pass1!")
        viewer = self.AppUser(username="viewer", phone="+15550006002", role="viewer", must_change_password=False)
        viewer.set_password("Viewer-pass1!")
        self.db.session.add_all([admin, viewer])
        self.db.session.flush()

        match_event = self.AuthEvent(
            event_type="login_failure",
            outcome="failed",
            username="admin",
            user_id=admin.id,
            client_ip="10.0.0.10",
            created_at=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        )
        match_event.set_metadata({"marker": "match-row"})

        old_event = self.AuthEvent(
            event_type="password_changed",
            outcome="success",
            username="admin",
            user_id=admin.id,
            client_ip="10.0.0.11",
            created_at=datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc),
        )
        old_event.set_metadata({"marker": "old-row"})

        viewer_event = self.AuthEvent(
            event_type="login_success",
            outcome="success",
            username="viewer",
            user_id=viewer.id,
            client_ip="10.0.0.12",
            created_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        )
        viewer_event.set_metadata({"marker": "viewer-row"})

        self.db.session.add_all([match_event, old_event, viewer_event])
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

    def test_security_events_route_is_admin_only(self) -> None:
        self._login("admin", "Admin-pass1!")
        admin_response = self.client.get("/security/events", follow_redirects=False)
        self.assertEqual(admin_response.status_code, 200)
        self.assertIn(b"Security Events", admin_response.data)

        self.client.post("/logout", follow_redirects=False)
        self._login("viewer", "Viewer-pass1!")
        viewer_response = self.client.get("/security/events", follow_redirects=False)
        self.assertEqual(viewer_response.status_code, 403)

    def test_security_events_filters_by_username_event_outcome(self) -> None:
        self._login("admin", "Admin-pass1!")
        filtered = self.client.get(
            "/security/events?username=admin&event_type=login_failure&outcome=failed",
            follow_redirects=False,
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"match-row", filtered.data)
        self.assertNotIn(b"old-row", filtered.data)
        self.assertNotIn(b"viewer-row", filtered.data)

    def test_security_events_filters_by_date_range(self) -> None:
        self._login("admin", "Admin-pass1!")
        filtered = self.client.get(
            "/security/events?date_from=2026-01-10&date_to=2026-01-20",
            follow_redirects=False,
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"match-row", filtered.data)
        self.assertIn(b"viewer-row", filtered.data)
        self.assertNotIn(b"old-row", filtered.data)


if __name__ == "__main__":
    unittest.main()

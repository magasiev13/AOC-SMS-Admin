import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


class TestScheduledStatusFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, Event, ScheduledMessage

        self.db = db
        self.AppUser = AppUser
        self.Event = Event
        self.ScheduledMessage = ScheduledMessage

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(
            username="admin",
            phone="+15550000008",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("admin-pass")
        self.db.session.add(admin)
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
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def _create_scheduled(
        self,
        *,
        message_body: str,
        status: str = "pending",
        target: str = "community",
        event_id: int | None = None,
    ):
        msg = self.ScheduledMessage(
            message_body=message_body,
            status=status,
            target=target,
            event_id=event_id,
            scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            test_mode=False,
        )
        self.db.session.add(msg)
        self.db.session.commit()
        return msg

    def test_scheduled_status_returns_all_pending_without_search(self) -> None:
        self._login()
        keep_one = self._create_scheduled(message_body="Alpha pending")
        keep_two = self._create_scheduled(message_body="Beta pending")
        self._create_scheduled(message_body="Past message", status="sent")

        response = self.client.get("/scheduled/status")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(set(payload["pending_ids"]), {keep_one.id, keep_two.id})
        self.assertEqual(payload["pending_count"], 2)

    def test_scheduled_status_filters_pending_ids_by_message_search(self) -> None:
        self._login()
        match = self._create_scheduled(message_body="Spring Gala invite")
        self._create_scheduled(message_body="Board reminder")

        response = self.client.get("/scheduled/status?search=gala")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["pending_ids"], [match.id])
        self.assertEqual(payload["pending_count"], 1)

    def test_scheduled_status_filters_pending_ids_by_event_title(self) -> None:
        self._login()
        event = self.Event(title="Neighborhood Cleanup")
        self.db.session.add(event)
        self.db.session.commit()

        match = self._create_scheduled(
            message_body="Reminder",
            target="event",
            event_id=event.id,
        )
        self._create_scheduled(message_body="Community hello", target="community")

        response = self.client.get("/scheduled/status?search=cleanup")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["pending_ids"], [match.id])
        self.assertEqual(payload["pending_count"], 1)

    def test_scheduled_bulk_cancel_only_updates_pending_or_processing(self) -> None:
        self._login()
        pending = self._create_scheduled(message_body="Pending message", status="pending")
        sent = self._create_scheduled(message_body="Sent message", status="sent")

        response = self.client.post(
            "/scheduled/bulk-cancel",
            data={"scheduled_ids": f"{pending.id},{sent.id}"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(pending)
        self.db.session.refresh(sent)
        self.assertEqual(pending.status, "cancelled")
        self.assertEqual(sent.status, "sent")


if __name__ == "__main__":
    unittest.main()

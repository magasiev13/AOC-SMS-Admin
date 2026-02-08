import importlib
import os
import tempfile
import unittest


class TestInboxRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, CommunityMember, InboxMessage, InboxThread

        self.db = db
        self.AppUser = AppUser
        self.CommunityMember = CommunityMember
        self.InboxMessage = InboxMessage
        self.InboxThread = InboxThread

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

        admin = self.AppUser(username="admin", role="admin", must_change_password=False)
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

    def test_inbox_status_requires_login(self) -> None:
        response = self.client.get("/inbox/status", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_inbox_status_returns_zero_when_no_messages(self) -> None:
        self._login()
        response = self.client.get("/inbox/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("latest_message_id"), 0)

    def test_inbox_status_returns_latest_message_id(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17202808358")
        self.db.session.add(thread)
        self.db.session.flush()

        message = self.InboxMessage(
            thread_id=thread.id,
            phone=thread.phone,
            direction="inbound",
            body="Hello from inbound",
        )
        self.db.session.add(message)
        self.db.session.commit()

        response = self.client.get("/inbox/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("latest_message_id"), message.id)

    def test_inbox_renders_community_name_with_phone_secondary(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17202808358")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Alex Rivera", phone=thread.phone))
        self.db.session.commit()

        response = self.client.get(f"/inbox?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<div class="fw-semibold">Alex Rivera</div>', html)
        self.assertIn(f'<div class="small text-muted">{thread.phone}</div>', html)

    def test_inbox_community_name_overrides_existing_thread_contact_name(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17202345027", contact_name="Twilio Name")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Community Name", phone=thread.phone))
        self.db.session.commit()

        response = self.client.get(f"/inbox?thread={thread.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<div class="fw-semibold">Community Name</div>', html)
        self.assertNotIn("Twilio Name", html)

    def test_inbox_search_matches_community_name_when_thread_name_missing(self) -> None:
        self._login()

        thread = self.InboxThread(phone="+17209990000")
        self.db.session.add(thread)
        self.db.session.add(self.CommunityMember(name="Jordan Blake", phone=thread.phone))
        self.db.session.commit()

        response = self.client.get("/inbox?search=Jordan")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Jordan Blake", html)
        self.assertIn(thread.phone, html)


if __name__ == "__main__":
    unittest.main()

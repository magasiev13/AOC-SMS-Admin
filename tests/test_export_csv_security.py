import csv
import importlib
import io
import os
import tempfile
import unittest


class TestCsvExportSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, CommunityMember, Event, EventRegistration, UnsubscribedContact

        self.db = db
        self.AppUser = AppUser
        self.CommunityMember = CommunityMember
        self.Event = Event
        self.EventRegistration = EventRegistration
        self.UnsubscribedContact = UnsubscribedContact

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
            phone="+15550000006",
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

    def test_community_export_escapes_formula_cells(self) -> None:
        self._login()
        member = self.CommunityMember(name="=2+2", phone="+17205550101")
        self.db.session.add(member)
        self.db.session.commit()

        response = self.client.get("/community/export")
        self.assertEqual(response.status_code, 200)

        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "'=2+2")
        self.assertEqual(rows[0]["phone"], "'+17205550101")

    def test_event_export_escapes_formula_cells(self) -> None:
        self._login()
        event = self.Event(title="Safe Export Event")
        self.db.session.add(event)
        self.db.session.flush()
        reg = self.EventRegistration(event_id=event.id, name="@evil", phone="+17205550102")
        self.db.session.add(reg)
        self.db.session.commit()

        response = self.client.get(f"/events/{event.id}/export")
        self.assertEqual(response.status_code, 200)

        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "'@evil")
        self.assertEqual(rows[0]["phone"], "'+17205550102")

    def test_unsubscribed_export_escapes_formula_cells(self) -> None:
        self._login()
        entry = self.UnsubscribedContact(
            name="-name",
            phone="+17205550103",
            reason="=SUM(1,1)",
            source="@manual",
        )
        self.db.session.add(entry)
        self.db.session.commit()

        response = self.client.get("/unsubscribed/export")
        self.assertEqual(response.status_code, 200)

        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "'-name")
        self.assertEqual(rows[0]["phone"], "'+17205550103")
        self.assertEqual(rows[0]["reason"], "'=SUM(1,1)")
        self.assertEqual(rows[0]["source"], "'@manual")


if __name__ == "__main__":
    unittest.main()

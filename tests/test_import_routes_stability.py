import importlib
import io
import os
import tempfile
import unittest


class TestImportRoutesStability(unittest.TestCase):
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
            phone="+15550000031",
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

    def test_community_import_skips_duplicate_rows_inside_same_file(self) -> None:
        self._login()
        csv_content = "\n".join(
            [
                "name,phone",
                "Alex,720-555-0201",
                "Alex Duplicate,(720) 555-0201",
                "Blair,720-555-0202",
            ]
        )
        response = self.client.post(
            "/community/import",
            data={"file": (io.BytesIO(csv_content.encode("utf-8")), "community.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Error processing CSV", response.data)
        self.assertIn(b"Imported 2 members. 1 duplicates skipped.", response.data)

        members = self.CommunityMember.query.order_by(self.CommunityMember.phone.asc()).all()
        self.assertEqual([member.phone for member in members], ["+17205550201", "+17205550202"])

    def test_event_import_skips_duplicate_rows_inside_same_file(self) -> None:
        self._login()
        event = self.Event(title="Import Event")
        self.db.session.add(event)
        self.db.session.commit()

        csv_content = "\n".join(
            [
                "name,phone",
                "Pat,720-555-0211",
                "Pat Duplicate,(720) 555-0211",
                "Rene,720-555-0212",
            ]
        )
        response = self.client.post(
            f"/events/{event.id}/import",
            data={"file": (io.BytesIO(csv_content.encode("utf-8")), "event.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Error processing CSV", response.data)
        self.assertIn(b"Added 2 registrations. 1 already registered.", response.data)

        registrations = self.EventRegistration.query.filter_by(event_id=event.id).order_by(
            self.EventRegistration.phone.asc()
        ).all()
        self.assertEqual([registration.phone for registration in registrations], ["+17205550211", "+17205550212"])

    def test_unsubscribed_import_skips_duplicate_rows_inside_same_file(self) -> None:
        self._login()
        csv_content = "\n".join(
            [
                "name,phone",
                "Jordan,720-555-0221",
                "Jordan Duplicate,(720) 555-0221",
                "Sky,720-555-0222",
            ]
        )
        response = self.client.post(
            "/unsubscribed/import",
            data={"file": (io.BytesIO(csv_content.encode("utf-8")), "unsubscribed.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Error processing CSV", response.data)
        self.assertIn(b"Imported 2 unsubscribed contact(s). 1 duplicates skipped.", response.data)

        entries = self.UnsubscribedContact.query.order_by(self.UnsubscribedContact.phone.asc()).all()
        self.assertEqual([entry.phone for entry in entries], ["+17205550221", "+17205550222"])


if __name__ == "__main__":
    unittest.main()

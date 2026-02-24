import importlib
import io
import os
import tempfile
import unittest


class TestEventsRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"

        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, Event, EventRegistration, SurveyFlow, UnsubscribedContact

        self.db = db
        self.AppUser = AppUser
        self.Event = Event
        self.EventRegistration = EventRegistration
        self.SurveyFlow = SurveyFlow
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
            phone="+15550000002",
            role="admin",
            must_change_password=False,
        )
        admin.set_password("admin-pass")
        manager = self.AppUser(
            username="manager",
            phone="+15550000003",
            role="social_manager",
            must_change_password=False,
        )
        manager.set_password("manager-pass")
        viewer = self.AppUser(
            username="viewer",
            phone="+15550000004",
            role="viewer",
            must_change_password=False,
        )
        viewer.set_password("viewer-pass")
        self.db.session.add_all([admin, manager, viewer])
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
        self._login_as("admin", "admin-pass")

    def _login_as(self, username: str, password: str) -> None:
        response = self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_events_list_mobile_cards_include_delete_action(self) -> None:
        self._login()

        event = self.Event(title="Community Picnic")
        self.db.session.add(event)
        self.db.session.commit()

        response = self.client.get("/events")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"delete-event-mobile-{event.id}".encode(), response.data)
        self.assertIn(b'bi bi-trash"></i> Delete', response.data)

    def test_events_list_mobile_delete_action_present_for_survey_linked_event(self) -> None:
        self._login()

        event = self.Event(title="AOC Gala")
        self.db.session.add(event)
        self.db.session.flush()

        survey = self.SurveyFlow(
            name="RSVP Flow",
            trigger_keyword="AOC RSVP",
            intro_message="Welcome!",
            completion_message="Thanks!",
            linked_event_id=event.id,
            is_active=True,
        )
        survey.set_questions(["Name?"])
        self.db.session.add(survey)
        self.db.session.commit()

        response = self.client.get("/events")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'data-event-title="{event.title}"'.encode(), response.data)
        self.assertIn(f"delete-event-mobile-{event.id}".encode(), response.data)

    def test_events_list_bulk_delete_controls_visible_for_admin(self) -> None:
        self._login()

        event = self.Event(title="Board Meeting")
        self.db.session.add(event)
        self.db.session.commit()

        response = self.client.get("/events")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="bulkDeleteBtn"', response.data)
        self.assertIn(b"Delete selected", response.data)
        self.assertIn(b'/events/bulk-delete', response.data)

    def test_events_list_hides_bulk_delete_controls_for_social_manager(self) -> None:
        self.db.session.add(self.Event(title="Neighborhood Cleanup"))
        self.db.session.commit()

        self._login_as("manager", "manager-pass")
        response = self.client.get("/events")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="bulkDeleteBtn"', response.data)
        self.assertNotIn(b'form="bulkDeleteForm"', response.data)

    def test_events_bulk_delete_deletes_selected_rows(self) -> None:
        self._login()

        keep = self.Event(title="Keep Event")
        drop_one = self.Event(title="Drop Event One")
        drop_two = self.Event(title="Drop Event Two")
        self.db.session.add_all([keep, drop_one, drop_two])
        self.db.session.commit()

        response = self.client.post(
            "/events/bulk-delete",
            data={"event_ids": [str(drop_one.id), str(drop_two.id)]},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

        remaining_titles = {event.title for event in self.Event.query.order_by(self.Event.id).all()}
        self.assertEqual(remaining_titles, {"Keep Event"})

    def test_event_registration_mutation_routes_forbidden_for_viewer(self) -> None:
        event = self.Event(title="Viewer Cannot Mutate")
        self.db.session.add(event)
        self.db.session.flush()
        registration = self.EventRegistration(event_id=event.id, name="Pat", phone="+15550002001")
        self.db.session.add(registration)
        self.db.session.commit()

        self._login_as("viewer", "viewer-pass")

        add_response = self.client.post(
            f"/events/{event.id}/register",
            data={"name": "New", "phone": "+15550002002"},
            follow_redirects=False,
        )
        self.assertEqual(add_response.status_code, 403)

        remove_response = self.client.post(
            f"/events/{event.id}/unregister/{registration.id}",
            follow_redirects=False,
        )
        self.assertEqual(remove_response.status_code, 403)

        unsubscribe_response = self.client.post(
            f"/events/{event.id}/registrations/{registration.id}/unsubscribe",
            follow_redirects=False,
        )
        self.assertEqual(unsubscribe_response.status_code, 403)

        import_response = self.client.post(
            f"/events/{event.id}/import",
            data={"file": (io.BytesIO(b"name,phone\nImport User,720-555-0203"), "event.csv")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(import_response.status_code, 403)

        self.assertEqual(self.EventRegistration.query.filter_by(event_id=event.id).count(), 1)
        self.assertEqual(self.UnsubscribedContact.query.count(), 0)

    def test_event_import_allows_social_manager(self) -> None:
        event = self.Event(title="Manager Import")
        self.db.session.add(event)
        self.db.session.commit()

        self._login_as("manager", "manager-pass")
        response = self.client.post(
            f"/events/{event.id}/import",
            data={"file": (io.BytesIO(b"name,phone\nImport User,720-555-0204"), "event.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Added 1 registrations.", response.data)
        self.assertEqual(self.EventRegistration.query.filter_by(event_id=event.id).count(), 1)


if __name__ == "__main__":
    unittest.main()

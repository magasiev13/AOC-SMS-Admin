import importlib
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
        from app.models import AppUser, Event, SurveyFlow

        self.db = db
        self.AppUser = AppUser
        self.Event = Event
        self.SurveyFlow = SurveyFlow

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


if __name__ == "__main__":
    unittest.main()

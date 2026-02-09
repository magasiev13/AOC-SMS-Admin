import importlib
import os
import tempfile
import unittest


class TestInboxAutomationRouteValidation(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import app.config

        importlib.reload(app.config)
        from app import create_app, db
        from app.models import AppUser, Event, KeywordAutomationRule, SurveyFlow

        self.db = db
        self.AppUser = AppUser
        self.Event = Event
        self.KeywordAutomationRule = KeywordAutomationRule
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

    def _create_survey(self, *, name: str, trigger_keyword: str):
        survey = self.SurveyFlow(
            name=name,
            trigger_keyword=trigger_keyword,
            intro_message="Welcome",
            completion_message="Done",
            is_active=True,
        )
        survey.set_questions(["Question 1?"])
        self.db.session.add(survey)
        self.db.session.commit()
        return survey

    def _create_rule(self, *, keyword: str):
        rule = self.KeywordAutomationRule(
            keyword=keyword,
            response_body="Auto-reply",
            is_active=True,
        )
        self.db.session.add(rule)
        self.db.session.commit()
        return rule

    def _create_event(self, *, title: str):
        event = self.Event(title=title)
        self.db.session.add(event)
        self.db.session.commit()
        return event

    def test_keyword_rule_add_rejects_existing_survey_keyword(self) -> None:
        self._login()
        self._create_survey(name="RSVP Flow", trigger_keyword="RSVP")

        response = self.client.post(
            "/inbox/keywords/add",
            data={
                "keyword": "rsvp",
                "response_body": "Hi there",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used as a survey trigger", response.data)
        self.assertIsNone(self.KeywordAutomationRule.query.filter_by(keyword="RSVP").first())

    def test_keyword_rule_edit_rejects_existing_survey_keyword(self) -> None:
        self._login()
        self._create_survey(name="Info Flow", trigger_keyword="INFO")
        rule = self._create_rule(keyword="HELP")

        response = self.client.post(
            f"/inbox/keywords/{rule.id}/edit",
            data={
                "keyword": "info",
                "response_body": "Updated",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used as a survey trigger", response.data)

        refreshed = self.db.session.get(self.KeywordAutomationRule, rule.id)
        self.assertEqual(refreshed.keyword, "HELP")

    def test_survey_add_rejects_existing_keyword_rule(self) -> None:
        self._login()
        self._create_rule(keyword="HELP")

        response = self.client.post(
            "/inbox/surveys/add",
            data={
                "name": "Help Survey",
                "trigger_keyword": "help",
                "intro_message": "Welcome",
                "questions": "Question 1?",
                "completion_message": "Done",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a keyword automation", response.data)
        self.assertIsNone(self.SurveyFlow.query.filter_by(trigger_keyword="HELP").first())

    def test_survey_edit_rejects_existing_keyword_rule(self) -> None:
        self._login()
        self._create_rule(keyword="HELP")
        survey = self._create_survey(name="RSVP Flow", trigger_keyword="RSVP")

        response = self.client.post(
            f"/inbox/surveys/{survey.id}/edit",
            data={
                "name": "RSVP Flow",
                "trigger_keyword": "help",
                "intro_message": "Welcome",
                "questions": "Question 1?",
                "completion_message": "Done",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a keyword automation", response.data)

        refreshed = self.db.session.get(self.SurveyFlow, survey.id)
        self.assertEqual(refreshed.trigger_keyword, "RSVP")

    def test_keywords_list_uses_data_confirm_attribute(self) -> None:
        self._login()
        self._create_rule(keyword="HELP")

        response = self.client.get("/inbox/keywords")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data.count(b'data-confirm="Delete this keyword rule?"'), 2)
        self.assertNotIn(b"onclick=\"return confirm('Delete this keyword rule?');\"", response.data)

    def test_surveys_list_uses_data_confirm_attribute(self) -> None:
        self._login()
        survey = self._create_survey(name="RSVP Flow", trigger_keyword="RSVP")

        response = self.client.get("/inbox/surveys")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data.count(b'data-confirm="Deactivate this survey flow?"'), 2)
        self.assertNotIn(b"onclick=\"return confirm('Deactivate this survey flow?');\"", response.data)
        submissions_href = f'/inbox/surveys/{survey.id}/submissions'.encode()
        self.assertGreaterEqual(response.data.count(submissions_href), 2)

    def test_survey_add_links_existing_event(self) -> None:
        self._login()
        event = self._create_event(title="Volunteer Day")

        response = self.client.post(
            "/inbox/surveys/add",
            data={
                "name": "Volunteer RSVP",
                "trigger_keyword": "VOL RSVP",
                "intro_message": "Welcome",
                "questions": "What is your name?\nHow many guests?",
                "completion_message": "Done",
                "event_link_mode": "existing",
                "existing_event_id": str(event.id),
                "is_active": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        created = self.SurveyFlow.query.filter_by(name="Volunteer RSVP").first()
        self.assertIsNotNone(created)
        self.assertEqual(created.linked_event_id, event.id)

    def test_survey_add_creates_and_links_new_event(self) -> None:
        self._login()

        response = self.client.post(
            "/inbox/surveys/add",
            data={
                "name": "Town Hall RSVP",
                "trigger_keyword": "TOWN RSVP",
                "intro_message": "Welcome",
                "questions": "What is your name?",
                "completion_message": "Done",
                "event_link_mode": "new",
                "new_event_title": "Town Hall",
                "new_event_date": "2026-05-01",
                "is_active": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        created = self.SurveyFlow.query.filter_by(name="Town Hall RSVP").first()
        self.assertIsNotNone(created)
        self.assertIsNotNone(created.linked_event_id)
        linked_event = self.db.session.get(self.Event, created.linked_event_id)
        self.assertIsNotNone(linked_event)
        self.assertEqual(linked_event.title, "Town Hall")
        self.assertEqual(str(linked_event.date), "2026-05-01")

    def test_survey_add_existing_mode_requires_valid_event(self) -> None:
        self._login()

        response = self.client.post(
            "/inbox/surveys/add",
            data={
                "name": "Invalid Link Survey",
                "trigger_keyword": "INVALID LINK",
                "intro_message": "",
                "questions": "What is your name?",
                "completion_message": "",
                "event_link_mode": "existing",
                "existing_event_id": "999999",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Selected event was not found", response.data)
        self.assertIsNone(self.SurveyFlow.query.filter_by(name="Invalid Link Survey").first())

    def test_survey_edit_can_switch_to_new_linked_event(self) -> None:
        self._login()
        survey = self._create_survey(name="Switch Link Survey", trigger_keyword="SWITCH LINK")

        response = self.client.post(
            f"/inbox/surveys/{survey.id}/edit",
            data={
                "name": "Switch Link Survey",
                "trigger_keyword": "SWITCH LINK",
                "intro_message": "Welcome",
                "questions": "What is your name?",
                "completion_message": "Done",
                "event_link_mode": "new",
                "new_event_title": "Switched Event",
                "new_event_date": "",
                "is_active": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        refreshed = self.db.session.get(self.SurveyFlow, survey.id)
        self.assertIsNotNone(refreshed.linked_event_id)
        linked_event = self.db.session.get(self.Event, refreshed.linked_event_id)
        self.assertIsNotNone(linked_event)
        self.assertEqual(linked_event.title, "Switched Event")

    def test_base_confirm_handler_does_not_stop_immediate_propagation(self) -> None:
        self._login()

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/static/js/app.js", response.data)
        self.assertNotIn(b"const confirmTrigger = event.target.closest('[data-confirm]')", response.data)
        self.assertNotIn(b"stopImmediatePropagation", response.data)


if __name__ == "__main__":
    unittest.main()

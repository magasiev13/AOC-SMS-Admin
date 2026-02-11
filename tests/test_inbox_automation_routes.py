import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone


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
        from app.models import (
            AppUser,
            Event,
            InboxMessage,
            InboxThread,
            KeywordAutomationRule,
            SurveyFlow,
            SurveyResponse,
            SurveySession,
        )

        self.db = db
        self.AppUser = AppUser
        self.Event = Event
        self.InboxMessage = InboxMessage
        self.InboxThread = InboxThread
        self.KeywordAutomationRule = KeywordAutomationRule
        self.SurveyFlow = SurveyFlow
        self.SurveyResponse = SurveyResponse
        self.SurveySession = SurveySession

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
        viewer = self.AppUser(username="viewer", role="viewer", must_change_password=False)
        viewer.set_password("viewer-pass")
        self.db.session.add_all([admin, viewer])
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

    def _create_survey(self, *, name: str, trigger_keyword: str, linked_event_id: int | None = None):
        survey = self.SurveyFlow(
            name=name,
            trigger_keyword=trigger_keyword,
            intro_message="Welcome",
            completion_message="Done",
            linked_event_id=linked_event_id,
            is_active=True,
        )
        survey.set_questions(["Question 1?"])
        self.db.session.add(survey)
        self.db.session.commit()
        return survey

    def _create_survey_submission(self, *, survey, phone: str = "+17205550100"):
        thread = self.InboxThread(phone=phone, contact_name="Survey Contact")
        self.db.session.add(thread)
        self.db.session.flush()

        session = self.SurveySession(
            survey_id=survey.id,
            thread_id=thread.id,
            phone=phone,
            status="completed",
            current_question_index=1,
        )
        self.db.session.add(session)
        self.db.session.flush()

        response = self.SurveyResponse(
            session_id=session.id,
            survey_id=survey.id,
            phone=phone,
            question_index=0,
            question_prompt=survey.questions[0],
            answer="Answer 1",
        )
        self.db.session.add(response)
        self.db.session.commit()
        return thread, session, response

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

    def _create_inbound_keyword_match(self, *, keyword: str, phone: str):
        thread = self.InboxThread(phone=phone, contact_name="Keyword Contact")
        self.db.session.add(thread)
        self.db.session.flush()

        message = self.InboxMessage(
            thread_id=thread.id,
            phone=phone,
            direction="inbound",
            body=f"Trigger {keyword}",
            matched_keyword=keyword,
            created_at=datetime.now(timezone.utc),
        )
        self.db.session.add(message)
        self.db.session.commit()
        return thread, message

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
        self.assertGreaterEqual(
            response.data.count(
                b'data-confirm="Delete this survey flow and all submissions? This cannot be undone."'
            ),
            2,
        )
        self.assertNotIn(b"onclick=\"return confirm('Deactivate this survey flow?');\"", response.data)
        submissions_href = f'/inbox/surveys/{survey.id}/submissions'.encode()
        self.assertGreaterEqual(response.data.count(submissions_href), 2)

    def test_survey_delete_removes_related_data(self) -> None:
        self._login()
        survey = self._create_survey(name="Delete Survey", trigger_keyword="DEL SURVEY")
        thread, session, response = self._create_survey_submission(survey=survey)

        delete_response = self.client.post(
            f"/inbox/surveys/{survey.id}/delete",
            follow_redirects=True,
        )

        self.assertEqual(delete_response.status_code, 200)
        self.assertIn(b"Survey flow deleted (1 session(s), 1 response(s)).", delete_response.data)
        self.assertIsNone(self.db.session.get(self.SurveyFlow, survey.id))
        self.assertIsNone(self.db.session.get(self.SurveySession, session.id))
        self.assertIsNone(self.db.session.get(self.SurveyResponse, response.id))
        self.assertIsNotNone(self.db.session.get(self.InboxThread, thread.id))

    def test_survey_delete_blocks_when_linked_to_event(self) -> None:
        self._login()
        event = self._create_event(title="Delete Guard Event")
        survey = self._create_survey(
            name="Linked Survey",
            trigger_keyword="LINKED SURVEY",
            linked_event_id=event.id,
        )

        delete_response = self.client.post(
            f"/inbox/surveys/{survey.id}/delete",
            follow_redirects=True,
        )

        self.assertEqual(delete_response.status_code, 200)
        self.assertIn(b"This survey is linked to an event.", delete_response.data)
        self.assertIn(b'before deleting.', delete_response.data)
        self.assertIsNotNone(self.db.session.get(self.SurveyFlow, survey.id))
        self.assertIsNotNone(self.db.session.get(self.Event, event.id))

    def test_survey_delete_requires_privileged_role(self) -> None:
        survey = self._create_survey(name="Protected Survey", trigger_keyword="PROTECTED")
        self._login_as("viewer", "viewer-pass")

        response = self.client.post(
            f"/inbox/surveys/{survey.id}/delete",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.db.session.get(self.SurveyFlow, survey.id))

    def test_dashboard_top_keywords_hides_deleted_keyword_rule(self) -> None:
        self._login()
        rule = self._create_rule(keyword="HELP")
        self._create_inbound_keyword_match(keyword="HELP", phone="+17205550101")

        before_delete = self.client.get("/dashboard")
        self.assertEqual(before_delete.status_code, 200)
        self.assertIn(b"<code>HELP</code>", before_delete.data)

        delete_response = self.client.post(
            f"/inbox/keywords/{rule.id}/delete",
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)

        after_delete = self.client.get("/dashboard")
        self.assertEqual(after_delete.status_code, 200)
        self.assertNotIn(b"<code>HELP</code>", after_delete.data)

    def test_dashboard_top_keywords_hides_deleted_survey_flow(self) -> None:
        self._login()
        survey = self._create_survey(name="RSVP Survey", trigger_keyword="RSVP")
        self._create_inbound_keyword_match(keyword="RSVP", phone="+17205550102")

        before_delete = self.client.get("/dashboard")
        self.assertEqual(before_delete.status_code, 200)
        self.assertIn(b"<code>RSVP</code>", before_delete.data)

        delete_response = self.client.post(
            f"/inbox/surveys/{survey.id}/delete",
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)

        after_delete = self.client.get("/dashboard")
        self.assertEqual(after_delete.status_code, 200)
        self.assertNotIn(b"<code>RSVP</code>", after_delete.data)

    def test_dashboard_top_keywords_hides_deactivated_triggers(self) -> None:
        self._login()
        rule = self._create_rule(keyword="HELP")
        survey = self._create_survey(name="RSVP Survey", trigger_keyword="RSVP")
        self._create_inbound_keyword_match(keyword="HELP", phone="+17205550103")
        self._create_inbound_keyword_match(keyword="RSVP", phone="+17205550104")

        before_deactivate = self.client.get("/dashboard")
        self.assertEqual(before_deactivate.status_code, 200)
        self.assertIn(b"<code>HELP</code>", before_deactivate.data)
        self.assertIn(b"<code>RSVP</code>", before_deactivate.data)

        rule_deactivate = self.client.post(
            f"/inbox/keywords/{rule.id}/edit",
            data={
                "keyword": "HELP",
                "response_body": "Auto-reply",
            },
            follow_redirects=False,
        )
        self.assertEqual(rule_deactivate.status_code, 302)

        survey_deactivate = self.client.post(
            f"/inbox/surveys/{survey.id}/deactivate",
            follow_redirects=False,
        )
        self.assertEqual(survey_deactivate.status_code, 302)

        after_deactivate = self.client.get("/dashboard")
        self.assertEqual(after_deactivate.status_code, 200)
        self.assertNotIn(b"<code>HELP</code>", after_deactivate.data)
        self.assertNotIn(b"<code>RSVP</code>", after_deactivate.data)

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

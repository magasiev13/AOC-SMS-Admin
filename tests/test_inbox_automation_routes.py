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
        from app.models import AppUser, KeywordAutomationRule, SurveyFlow

        self.db = db
        self.AppUser = AppUser
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

        refreshed = self.KeywordAutomationRule.query.get(rule.id)
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

        refreshed = self.SurveyFlow.query.get(survey.id)
        self.assertEqual(refreshed.trigger_keyword, "RSVP")

    def test_keywords_list_uses_data_confirm_attribute(self) -> None:
        self._login()
        self._create_rule(keyword="HELP")

        response = self.client.get("/inbox/keywords")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-confirm="Delete this keyword rule?"', response.data)
        self.assertNotIn(b"onclick=\"return confirm('Delete this keyword rule?');\"", response.data)

    def test_surveys_list_uses_data_confirm_attribute(self) -> None:
        self._login()
        self._create_survey(name="RSVP Flow", trigger_keyword="RSVP")

        response = self.client.get("/inbox/surveys")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-confirm="Deactivate this survey flow?"', response.data)
        self.assertNotIn(b"onclick=\"return confirm('Deactivate this survey flow?');\"", response.data)

    def test_base_confirm_handler_does_not_stop_immediate_propagation(self) -> None:
        self._login()

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"document.addEventListener('click'", response.data)
        self.assertNotIn(b"stopImmediatePropagation", response.data)


if __name__ == "__main__":
    unittest.main()

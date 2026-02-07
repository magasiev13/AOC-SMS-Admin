import importlib
import os
import tempfile
import unittest


class TestInboxKeywordConflicts(unittest.TestCase):
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
        survey = self.SurveyFlow(name=name, trigger_keyword=trigger_keyword, is_active=True)
        survey.set_questions(["What is your answer?"])
        self.db.session.add(survey)
        self.db.session.commit()
        return survey

    def test_keyword_add_rejects_existing_survey_trigger(self) -> None:
        self._create_survey(name="RSVP Survey", trigger_keyword="  rsvp   now ")
        self._login()

        response = self.client.post(
            "/inbox/keywords/add",
            data={
                "keyword": "RSVP now",
                "response_body": "Thanks for checking in.",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a survey trigger", response.data)
        self.assertEqual(self.KeywordAutomationRule.query.count(), 0)

    def test_survey_add_rejects_existing_keyword_rule(self) -> None:
        rule = self.KeywordAutomationRule(
            keyword="  join   us ",
            response_body="Thanks for reaching out.",
            is_active=True,
        )
        self.db.session.add(rule)
        self.db.session.commit()
        self._login()

        response = self.client.post(
            "/inbox/surveys/add",
            data={
                "name": "Join Survey",
                "trigger_keyword": "join us",
                "intro_message": "",
                "completion_message": "",
                "questions": "What is your name?",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a keyword automation", response.data)
        self.assertIsNone(self.SurveyFlow.query.filter_by(name="Join Survey").first())

    def test_keyword_edit_rejects_existing_survey_trigger(self) -> None:
        self._create_survey(name="Checkin Survey", trigger_keyword="check in")
        rule = self.KeywordAutomationRule(
            keyword="HELP",
            response_body="How can we help?",
            is_active=True,
        )
        self.db.session.add(rule)
        self.db.session.commit()
        self._login()

        response = self.client.post(
            f"/inbox/keywords/{rule.id}/edit",
            data={
                "keyword": "  check   in ",
                "response_body": "How can we help?",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a survey trigger", response.data)
        refreshed_rule = self.db.session.get(self.KeywordAutomationRule, rule.id)
        self.assertEqual(refreshed_rule.keyword, "HELP")

    def test_survey_edit_rejects_existing_keyword_rule(self) -> None:
        self.db.session.add(
            self.KeywordAutomationRule(
                keyword="ATTEND",
                response_body="Thanks!",
                is_active=True,
            )
        )
        survey = self._create_survey(name="RSVP Survey", trigger_keyword="RSVP")
        self._login()

        response = self.client.post(
            f"/inbox/surveys/{survey.id}/edit",
            data={
                "name": "RSVP Survey",
                "trigger_keyword": "  attend ",
                "intro_message": "",
                "completion_message": "",
                "questions": "Will you be there?",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a keyword automation", response.data)
        refreshed_survey = self.db.session.get(self.SurveyFlow, survey.id)
        self.assertEqual(refreshed_survey.trigger_keyword, "RSVP")


if __name__ == "__main__":
    unittest.main()

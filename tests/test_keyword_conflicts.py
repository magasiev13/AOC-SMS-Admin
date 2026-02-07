import os
import tempfile
import unittest


class TestKeywordCrossTableConflicts(unittest.TestCase):
    def setUp(self) -> None:
        self._original_flask_debug = os.environ.get("FLASK_DEBUG")
        os.environ["FLASK_DEBUG"] = "1"
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

        import importlib
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

        user = self.AppUser(username="admin", role="admin", must_change_password=False)
        user.set_password("admin-password")
        self.db.session.add(user)
        self.db.session.commit()

    def tearDown(self) -> None:
        self.db.session.remove()
        self.db.drop_all()
        self.db.engine.dispose()
        self._app_context.pop()
        self._temp_dir.cleanup()
        os.environ.pop("DATABASE_URL", None)
        if self._original_flask_debug is None:
            os.environ.pop("FLASK_DEBUG", None)
        else:
            os.environ["FLASK_DEBUG"] = self._original_flask_debug

    def _login(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin-password"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_keyword_rule_add_rejects_keyword_used_by_survey(self) -> None:
        survey = self.SurveyFlow(
            name="Legacy Survey",
            trigger_keyword="JOIN   NOW",
            intro_message=None,
            completion_message=None,
            is_active=True,
        )
        survey.set_questions(["How are you?"])
        self.db.session.add(survey)
        self.db.session.commit()

        self._login()
        response = self.client.post(
            "/inbox/keywords/add",
            data={
                "keyword": "join now",
                "response_body": "Thanks for texting.",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used as a survey trigger", response.data)
        self.assertEqual(self.KeywordAutomationRule.query.count(), 0)

    def test_survey_add_rejects_keyword_used_by_automation_rule(self) -> None:
        rule = self.KeywordAutomationRule(
            keyword="JOIN   NOW",
            response_body="Thanks for your message.",
            is_active=True,
        )
        self.db.session.add(rule)
        self.db.session.commit()

        self._login()
        response = self.client.post(
            "/inbox/surveys/add",
            data={
                "name": "Weekly Survey",
                "trigger_keyword": "join now",
                "intro_message": "",
                "completion_message": "",
                "questions": "How was your week?",
                "is_active": "on",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already used by a keyword automation", response.data)
        self.assertIsNone(self.SurveyFlow.query.filter_by(name="Weekly Survey").first())


if __name__ == "__main__":
    unittest.main()

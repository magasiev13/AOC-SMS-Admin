import importlib
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestInboxService(unittest.TestCase):
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
            InboxMessage,
            InboxThread,
            KeywordAutomationRule,
            SurveyFlow,
            SurveyResponse,
            SurveySession,
            UnsubscribedContact,
        )
        from app.services.inbox_service import process_inbound_sms

        self.db = db
        self.InboxMessage = InboxMessage
        self.InboxThread = InboxThread
        self.KeywordAutomationRule = KeywordAutomationRule
        self.SurveyFlow = SurveyFlow
        self.SurveySession = SurveySession
        self.SurveyResponse = SurveyResponse
        self.UnsubscribedContact = UnsubscribedContact
        self.process_inbound_sms = process_inbound_sms

        self.app = create_app(run_startup_tasks=False, start_scheduler=False)
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            TWILIO_VALIDATE_INBOUND_SIGNATURE=False,
            INBOUND_AUTO_REPLY_ENABLED=True,
        )
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.db.create_all()
        self.client = self.app.test_client()

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

    @patch("app.services.inbox_service.get_twilio_service")
    def test_keyword_rule_matches_and_replies(self, mock_get_twilio) -> None:
        rule = self.KeywordAutomationRule(keyword="HELP", response_body="Support is on the way.", is_active=True)
        self.db.session.add(rule)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM111",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        result = self.process_inbound_sms(
            {"From": "+15551234567", "Body": "help", "MessageSid": "SM-IN-1"}
        )
        self.assertEqual(result["status"], "keyword_reply")

        thread = self.InboxThread.query.filter_by(phone="+15551234567").first()
        self.assertIsNotNone(thread)

        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.created_at.asc())
            .all()
        )
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].direction, "inbound")
        self.assertEqual(messages[0].matched_keyword, "HELP")
        self.assertEqual(messages[1].direction, "outbound")
        self.assertEqual(messages[1].body, "Support is on the way.")

        refreshed = self.KeywordAutomationRule.query.get(rule.id)
        self.assertEqual(refreshed.match_count, 1)
        self.assertIsNotNone(refreshed.last_matched_at)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_survey_flow_starts_and_completes(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="RSVP Flow",
            trigger_keyword="RSVP",
            intro_message="Thanks for joining.",
            completion_message="All set. Thank you!",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM222",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "RSVP", "MessageSid": "SM-IN-2"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        session = self.SurveySession.query.filter_by(phone="+15550001111", status="active").first()
        self.assertIsNotNone(session)
        self.assertEqual(session.current_question_index, 0)

        first_answer = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "Alex", "MessageSid": "SM-IN-3"}
        )
        self.assertEqual(first_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15550001111").first()
        self.assertEqual(session.current_question_index, 1)
        self.assertEqual(session.status, "active")

        second_answer = self.process_inbound_sms(
            {"From": "+15550001111", "Body": "3", "MessageSid": "SM-IN-4"}
        )
        self.assertEqual(second_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15550001111").first()
        self.assertEqual(session.status, "completed")
        self.assertIsNotNone(session.completed_at)

        responses = self.SurveyResponse.query.filter_by(phone="+15550001111").all()
        self.assertEqual(len(responses), 2)

        refreshed_survey = self.SurveyFlow.query.get(survey.id)
        self.assertEqual(refreshed_survey.start_count, 1)
        self.assertEqual(refreshed_survey.completion_count, 1)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_active_survey_yes_is_recorded_as_answer(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="Attendance Flow",
            trigger_keyword="ATTEND",
            intro_message="Welcome.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "Are you attending?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM555",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15559990000", "Body": "ATTEND", "MessageSid": "SM-IN-YES-1"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        first_answer = self.process_inbound_sms(
            {"From": "+15559990000", "Body": "Taylor", "MessageSid": "SM-IN-YES-2"}
        )
        self.assertEqual(first_answer["status"], "survey_response")

        second_answer = self.process_inbound_sms(
            {"From": "+15559990000", "Body": "YES", "MessageSid": "SM-IN-YES-3"}
        )
        self.assertEqual(second_answer["status"], "survey_response")

        session = self.SurveySession.query.filter_by(phone="+15559990000").first()
        self.assertEqual(session.status, "completed")
        self.assertIsNotNone(session.completed_at)

        responses = (
            self.SurveyResponse.query.filter_by(phone="+15559990000")
            .order_by(self.SurveyResponse.question_index.asc())
            .all()
        )
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1].answer, "YES")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15559990000").first()
        self.assertIsNone(unsubscribed)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_cancel_during_active_survey_opts_out(self, mock_get_twilio) -> None:
        survey = self.SurveyFlow(
            name="Cancel Flow",
            trigger_keyword="CHECKIN",
            intro_message="Starting flow.",
            completion_message="Done.",
            is_active=True,
        )
        survey.set_questions(["What is your name?", "How many guests?"])
        self.db.session.add(survey)
        self.db.session.commit()

        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM666",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        start_result = self.process_inbound_sms(
            {"From": "+15551112222", "Body": "CHECKIN", "MessageSid": "SM-IN-CANCEL-1"}
        )
        self.assertEqual(start_result["status"], "survey_started")

        cancel_result = self.process_inbound_sms(
            {"From": "+15551112222", "Body": "CANCEL", "MessageSid": "SM-IN-CANCEL-2"}
        )
        self.assertEqual(cancel_result["status"], "opt_out")

        session = self.SurveySession.query.filter_by(phone="+15551112222").first()
        self.assertEqual(session.status, "cancelled")
        self.assertIsNotNone(session.completed_at)

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15551112222").first()
        self.assertIsNotNone(unsubscribed)

        thread = self.InboxThread.query.filter_by(phone="+15551112222").first()
        messages = (
            self.InboxMessage.query.filter_by(thread_id=thread.id)
            .order_by(self.InboxMessage.created_at.asc())
            .all()
        )
        self.assertTrue(
            any(
                msg.direction == "outbound"
                and "You are unsubscribed and will no longer receive SMS alerts." in msg.body
                for msg in messages
            )
        )

    @patch("app.services.inbox_service.get_twilio_service")
    def test_cancel_without_active_survey_opts_out(self, mock_get_twilio) -> None:
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM777",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        cancel_result = self.process_inbound_sms(
            {"From": "+15556667777", "Body": "CANCEL", "MessageSid": "SM-IN-CANCEL-3"}
        )
        self.assertEqual(cancel_result["status"], "opt_out")

        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15556667777").first()
        self.assertIsNotNone(unsubscribed)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_stop_then_start_updates_unsubscribe_state(self, mock_get_twilio) -> None:
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM333",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        stop_result = self.process_inbound_sms(
            {"From": "+15554443333", "Body": "STOP", "MessageSid": "SM-IN-5"}
        )
        self.assertEqual(stop_result["status"], "opt_out")
        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15554443333").first()
        self.assertIsNotNone(unsubscribed)

        start_result = self.process_inbound_sms(
            {"From": "+15554443333", "Body": "START", "MessageSid": "SM-IN-6"}
        )
        self.assertEqual(start_result["status"], "opt_in")
        unsubscribed = self.UnsubscribedContact.query.filter_by(phone="+15554443333").first()
        self.assertIsNone(unsubscribed)

    def test_webhook_rejects_when_signature_required(self) -> None:
        self.app.config["TWILIO_VALIDATE_INBOUND_SIGNATURE"] = True
        response = self.client.post(
            "/webhooks/twilio/inbound",
            data={"From": "+15557778888", "Body": "Hello", "MessageSid": "SM-IN-7"},
        )
        self.assertEqual(response.status_code, 403)

    @patch("app.services.inbox_service.get_twilio_service")
    def test_webhook_accepts_when_signature_validation_disabled(self, mock_get_twilio) -> None:
        self.app.config["TWILIO_VALIDATE_INBOUND_SIGNATURE"] = False
        mock_service = MagicMock()
        mock_service.send_message.return_value = {
            "success": True,
            "sid": "SM444",
            "status": "sent",
            "error": None,
        }
        mock_get_twilio.return_value = mock_service

        response = self.client.post(
            "/webhooks/twilio/inbound",
            data={"From": "+15558889999", "Body": "hello", "MessageSid": "SM-IN-8"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<Response></Response>", response.data)


if __name__ == "__main__":
    unittest.main()
